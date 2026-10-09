"""3D Printer Notifier: printer WebSocket events to Discord."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("3d-printer-notifier")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
