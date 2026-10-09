"""Pydantic models for the Moonraker JSON-RPC API used by the Snapmaker U1.

The U1 runs Snapmaker's fork of Klipper + Moonraker + Fluidd. The notifier
talks to Moonraker's JSON-RPC 2.0 WebSocket (ws://{ip}/websocket) and tracks
a handful of printer objects:

- print_stats    — job state machine (standby/printing/paused/complete/...)
- virtual_sdcard — file progress (0.0-1.0) and active flag
- display_status — human-readable progress/message shown on the LCD
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict


class PrintState(str, Enum):
    """Values reported in print_stats.state."""
    STANDBY = "standby"
    PRINTING = "printing"
    PAUSED = "paused"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    ERROR = "error"

    @classmethod
    def label(cls, value: Optional[str]) -> str:
        if not value:
            return "Unknown"
        try:
            return cls(value).name.replace("_", " ").title()
        except ValueError:
            return value.title()


class PrintStatsInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_layer: Optional[int] = None
    current_layer: Optional[int] = None


class PrintStats(BaseModel):
    model_config = ConfigDict(extra="ignore")

    filename: Optional[str] = None
    total_duration: Optional[float] = None
    print_duration: Optional[float] = None
    filament_used: Optional[float] = None
    state: Optional[str] = None
    message: Optional[str] = None
    info: Optional[PrintStatsInfo] = None


class VirtualSdcard(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file_path: Optional[str] = None
    progress: Optional[float] = None   # 0.0 - 1.0
    is_active: Optional[bool] = None
    file_position: Optional[int] = None

    @property
    def progress_pct(self) -> Optional[int]:
        if self.progress is None:
            return None
        return round(self.progress * 100)


class DisplayStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    progress: Optional[float] = None
    message: Optional[str] = None


class PrinterObjectsStatus(BaseModel):
    """The 'status' object inside a subscribe response or notify_status_update."""
    model_config = ConfigDict(extra="ignore")

    print_stats: Optional[PrintStats] = None
    virtual_sdcard: Optional[VirtualSdcard] = None
    display_status: Optional[DisplayStatus] = None
