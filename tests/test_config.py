import textwrap

import pytest

from printer_notifier.config import ConfigError, load_config
from printer_notifier.printers.cc1 import CC1PrinterConfig
from printer_notifier.printers.u1 import U1PrinterConfig


def write(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body))
    return str(path)


def test_multiple_printers_of_each_type(tmp_path, monkeypatch):
    monkeypatch.setenv("HOOK", "https://discord.example/default")
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    cfg = load_config(write(tmp_path, """
        discord:
          webhookUrl: ${HOOK}
        notify:
          progressMilestones: [75, 25, 50, 25]
        printers:
          - {name: u1-a, type: u1, host: ws://10.0.0.1, apiKey: secret}
          - {name: u1-b, type: u1, host: 10.0.0.2, port: 7125}
          - name: cc1-a
            type: cc1
            host: 10.0.0.3
            discord_webhook_url: https://discord.example/cc1
            notify: {onProgress: false}
          - {name: cc1-b, type: cc1, host: 10.0.0.4, mainboardId: abc}
    """))

    u1a, u1b, cc1a, cc1b = cfg.printers
    assert isinstance(u1a, U1PrinterConfig) and isinstance(u1b, U1PrinterConfig)
    assert isinstance(cc1a, CC1PrinterConfig) and isinstance(cc1b, CC1PrinterConfig)
    assert (u1a.host, u1a.port, u1a.api_key) == ("10.0.0.1", 80, "secret")
    assert u1b.port == 7125
    assert cc1a.port == 3030 and cc1b.mainboard_id == "abc"

    assert cfg.notify.progress_milestones == [25, 50, 75]
    assert cfg.notify_for(cc1a).on_progress is False
    assert cfg.notify_for(cc1a).on_error is True
    assert cfg.notify_for(u1a).on_progress is True
    assert cfg.webhook_for(cc1a) == "https://discord.example/cc1"
    assert cfg.webhook_for(u1a) == "https://discord.example/default"


@pytest.mark.parametrize("body, message", [
    ("""
        discord: {webhookUrl: x}
        printers: [{name: p, type: bambu, host: h}]
     """, "unknown type 'bambu'"),
    ("""
        discord: {webhookUrl: x}
        printers:
          - {name: p, type: u1, host: a}
          - {name: p, type: cc1, host: b}
     """, "duplicated: p"),
    ("""
        printers: [{name: p, type: u1, host: h}]
     """, "has no Discord webhook"),
    ("""
        discord: {webhookUrl: x}
        printers: [{name: p, type: u1, host: h, mainboardId: nope}]
     """, "mainboardId"),
    ("""
        discord: {webhookUrl: ${NOT_SET_ANYWHERE}}
        printers: [{name: p, type: u1, host: h}]
     """, "NOT_SET_ANYWHERE"),
    ("""
        discord: {webhookUrl: x}
        printers: []
     """, "printers"),
])
def test_invalid_configs(tmp_path, body, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, body))


def test_env_default_and_log_level_override(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    cfg = load_config(write(tmp_path, """
        logLevel: ERROR
        discord: {webhookUrl: "${MISSING_HOOK:-https://discord.example/fallback}"}
        printers: [{name: p, type: u1, host: h}]
    """))
    assert cfg.log_level == "DEBUG"
    assert cfg.discord.webhook_url == "https://discord.example/fallback"


def test_example_config_is_valid(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/x")
    cfg = load_config("config.example.yaml")
    assert [p.type for p in cfg.printers] == ["u1", "u1", "cc1", "cc1"]
    assert cfg.printers[3].enabled is False
