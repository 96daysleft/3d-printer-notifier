"""Application configuration, loaded from a YAML file.

Keys may be written in camelCase (as rendered by the Helm chart) or
snake_case. Any string value may reference environment variables with
``${VAR}`` or ``${VAR:-default}`` so secrets can be injected at runtime
(e.g. from a Kubernetes Secret or OpenBao) instead of living in the file.
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

DEFAULT_CONFIG_PATH = "config.yaml"

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


class ConfigModel(BaseModel):
    """Base for all config models: camelCase or snake_case keys, unknown keys rejected."""
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class NotifyConfig(ConfigModel):
    """Which events produce Discord notifications."""
    on_connect: bool = True          # "Printer Connected" message on every (re)connect
    on_print_start: bool = True
    on_print_finish: bool = True
    on_error: bool = True            # firmware errors, cancelled/failed prints, disconnects
    on_progress: bool = True
    progress_milestones: List[int] = Field(default_factory=lambda: [25, 50, 75])
    with_snapshot: bool = True       # attach a camera frame to alerts

    @field_validator("progress_milestones")
    @classmethod
    def _milestones_in_range(cls, v: List[int]) -> List[int]:
        if any(m < 1 or m > 100 for m in v):
            raise ValueError("progress milestones must be between 1 and 100")
        return sorted(set(v))


class NotifyOverrides(ConfigModel):
    """Per-printer overrides; unset fields fall back to the global notify block."""
    on_connect: Optional[bool] = None
    on_print_start: Optional[bool] = None
    on_print_finish: Optional[bool] = None
    on_error: Optional[bool] = None
    on_progress: Optional[bool] = None
    progress_milestones: Optional[List[int]] = None
    with_snapshot: Optional[bool] = None


class CameraConfig(ConfigModel):
    # Plain JPEG endpoint or MJPEG stream. Empty = the printer type's default.
    snapshot_url: Optional[str] = None


class PrinterConfig(ConfigModel):
    """Settings common to every printer type.

    Each printer type subclasses this with its own ``type`` literal and any
    protocol-specific fields (see printer_notifier/printers/).
    """
    name: str = Field(min_length=1)
    type: str
    host: str = Field(min_length=1)
    port: Optional[int] = None       # None = the printer type's default port
    enabled: bool = True
    discord_webhook_url: Optional[str] = None   # overrides discord.webhookUrl
    camera: CameraConfig = Field(default_factory=CameraConfig)
    notify: NotifyOverrides = Field(default_factory=NotifyOverrides)

    @field_validator("host")
    @classmethod
    def _strip_scheme(cls, v: str) -> str:
        for prefix in ("ws://", "wss://", "http://", "https://"):
            if v.startswith(prefix):
                v = v[len(prefix):]
        return v.rstrip("/")


class DiscordConfig(ConfigModel):
    webhook_url: str = ""            # default webhook for printers without their own
    username: str = "3D Printer Notifier"


class AppConfig(ConfigModel):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"
    poll_interval_minutes: int = Field(default=5, ge=1)
    # Detailed alert embeds per Discord message (Discord allows 10 embeds,
    # one is used by the summary). Extra alerts are still counted in the summary.
    max_alerts_per_poll: int = Field(default=5, ge=1, le=9)
    health_port: int = Field(default=8080, ge=0, le=65535)   # 0 disables /healthz
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    printers: List[PrinterConfig] = Field(min_length=1)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v

    @field_validator("printers", mode="before")
    @classmethod
    def _parse_printers(cls, v: Any) -> Any:
        """Validate each printer entry against its own type's config model."""
        # Imported here: the printers package imports this module.
        from .printers import parse_printer_config

        if not isinstance(v, list):
            return v
        return [parse_printer_config(p) if isinstance(p, dict) else p for p in v]

    @model_validator(mode="after")
    def _check_printers(self) -> "AppConfig":
        names = [p.name for p in self.printers]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"printer names must be unique, duplicated: {', '.join(dupes)}")
        for p in self.printers:
            if not (p.discord_webhook_url or self.discord.webhook_url):
                raise ValueError(
                    f"printer '{p.name}' has no Discord webhook: set discord.webhookUrl "
                    "or the printer's discordWebhookUrl"
                )
        return self

    def notify_for(self, printer: PrinterConfig) -> NotifyConfig:
        """Global notify settings with the printer's overrides applied."""
        overrides = printer.notify.model_dump(exclude_none=True)
        return NotifyConfig.model_validate({**self.notify.model_dump(), **overrides})

    def webhook_for(self, printer: PrinterConfig) -> str:
        return printer.discord_webhook_url or self.discord.webhook_url


def _expand_env(value: Any) -> Any:
    """Recursively substitute ${VAR} / ${VAR:-default} in string values."""
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ConfigError(f"environment variable '{name}' is referenced in the config but not set")

    return _ENV_REF.sub(replace, value)


def load_config(path: Optional[str] = None) -> AppConfig:
    """Load the YAML config from ``path``, $CONFIG_FILE, or ./config.yaml."""
    config_path = Path(path or os.getenv("CONFIG_FILE") or DEFAULT_CONFIG_PATH)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        raw: Dict[str, Any] = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"could not parse {config_path}: {e}") from e

    raw = _expand_env(raw)

    # LOG_LEVEL env var wins over the file, handy for one-off debugging
    if os.getenv("LOG_LEVEL"):
        raw["logLevel"] = os.environ["LOG_LEVEL"]
        raw.pop("log_level", None)

    try:
        return AppConfig.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"invalid config in {config_path}:\n{e}") from e
