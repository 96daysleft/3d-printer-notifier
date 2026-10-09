"""Printer type registry.

``type:`` in each printer's config selects the PrinterClient subclass
registered under that name. See base.py for how to add a new type.
"""

from typing import Any, Dict, Type

from ..config import NotifyConfig, PrinterConfig
from .base import PrinterClient

PRINTER_TYPES: Dict[str, Type[PrinterClient]] = {}


def register_printer_type(cls: Type[PrinterClient]) -> Type[PrinterClient]:
    """Class decorator that makes a PrinterClient available by its type_name."""
    if cls.type_name in PRINTER_TYPES:
        raise ValueError(f"printer type '{cls.type_name}' is already registered")
    PRINTER_TYPES[cls.type_name] = cls
    return cls


def parse_printer_config(raw: Dict[str, Any]) -> PrinterConfig:
    """Validate a raw printer dict with the config model of its declared type."""
    printer_type = raw.get("type")
    if printer_type not in PRINTER_TYPES:
        known = ", ".join(sorted(PRINTER_TYPES)) or "none"
        label = raw.get("name", "<unnamed>")
        raise ValueError(f"printer '{label}': unknown type '{printer_type}' (known types: {known})")
    return PRINTER_TYPES[printer_type].config_model.model_validate(raw)


def create_printer_client(config: PrinterConfig, notify: NotifyConfig) -> PrinterClient:
    return PRINTER_TYPES[config.type](config, notify)


# Built-in printer types register themselves on import
from . import cc1, u1  # noqa: E402,F401
