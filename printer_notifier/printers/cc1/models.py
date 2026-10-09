"""Pydantic models for the Elegoo Centauri Carbon SDCP WebSocket messages."""

from enum import IntEnum
from typing import List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class MachineStatus(IntEnum):
    """Machine status codes reported in CurrentStatus / PreviousStatus."""
    IDLE = 0
    PRINTING = 1
    FILE_TRANSFERRING = 2
    CALIBRATING = 3
    DEVICE_TESTING = 4

    @classmethod
    def label(cls, code: int) -> str:
        try:
            return cls(code).name.replace("_", " ").title()
        except ValueError:
            return f"Unknown ({code})"

    @classmethod
    def labels(cls, codes: List[int]) -> str:
        return ", ".join(cls.label(c) for c in codes) or "Unknown"


class PrintErrorCode(IntEnum):
    """Error codes reported in PrintInfo.ErrorNumber."""
    NORMAL = 0
    FILE_MD5_CHECK_FAILED = 1
    FILE_READ_FAILED = 2
    RESOLUTION_MISMATCH = 3
    FORMAT_MISMATCH = 4
    MACHINE_MODEL_MISMATCH = 5

    @classmethod
    def label(cls, code: int) -> str:
        try:
            return cls(code).name.replace("_", " ").title().replace("Md5", "MD5")
        except ValueError:
            return f"Unknown Error ({code})"


class PrintInfo(BaseModel):
    # extra='ignore' swallows the hex-string keys the firmware emits
    model_config = ConfigDict(extra="ignore")

    Status: Optional[int] = None
    CurrentLayer: Optional[int] = None
    TotalLayer: Optional[int] = None
    CurrentTicks: Optional[float] = None   # elapsed seconds
    TotalTicks: Optional[float] = None     # estimated total seconds
    Filename: Optional[str] = None
    ErrorNumber: Optional[int] = None
    TaskId: Optional[str] = None
    PrintSpeedPct: Optional[int] = None
    Progress: Optional[int] = None

    @property
    def progress_pct(self) -> Optional[int]:
        """Firmware Progress, falling back to layer count when it is stuck at 0."""
        if self.Progress:
            return self.Progress
        if self.TotalLayer and self.CurrentLayer is not None:
            return int(self.CurrentLayer / self.TotalLayer * 100)
        return self.Progress

    @property
    def layers(self) -> Optional[str]:
        if self.TotalLayer:
            return f"{self.CurrentLayer or 0}/{self.TotalLayer}"
        return None


class StatusPayload(BaseModel):
    # extra='ignore' handles firmware-specific fields (fans, lights, coords...)
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    CurrentStatus: Optional[List[int]] = None
    # PreviousStatus can be int or list depending on firmware version
    PreviousStatus: Optional[Union[int, List[int]]] = None
    TempOfNozzle: Optional[float] = None
    TempTargetNozzle: Optional[float] = None
    TempOfHotbed: Optional[float] = None
    TempTargetHotbed: Optional[float] = None
    TempOfBox: Optional[float] = None
    print_info: Optional[PrintInfo] = Field(None, alias="PrintInfo")


class StatusMessage(BaseModel):
    """sdcp/status/{MainboardID} envelope."""
    model_config = ConfigDict(extra="ignore")

    Status: StatusPayload
    MainboardID: Optional[str] = None
    TimeStamp: Optional[int] = None
    Topic: str


class AttributesPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    Name: Optional[str] = None
    MachineName: Optional[str] = None
    BrandName: Optional[str] = None
    ProtocolVersion: Optional[str] = None
    FirmwareVersion: Optional[str] = None
    MainboardIP: Optional[str] = None
    MainboardID: Optional[str] = None


class AttributesMessage(BaseModel):
    """sdcp/attributes/{MainboardID} envelope."""
    model_config = ConfigDict(extra="ignore")

    Attributes: AttributesPayload
    MainboardID: Optional[str] = None
    TimeStamp: Optional[int] = None
    Topic: str
