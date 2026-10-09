"""Internal alert representation and the per-printer alert buffer."""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high", "critical"]


class Alert(BaseModel):
    """A printer event worth notifying about, independent of printer type."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    description: str
    severity: Severity = "low"
    # Accepts a datetime or Unix seconds (as sent by SDCP); stored as aware UTC
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


class AlertBuffer:
    """Collects alerts between Discord flushes."""

    def __init__(self) -> None:
        self._alerts: List[Alert] = []
        self._lock = asyncio.Lock()

    async def add(self, alert: Alert) -> None:
        async with self._lock:
            self._alerts.append(alert)

    async def drain(self) -> List[Alert]:
        """Return and clear all buffered alerts."""
        async with self._lock:
            alerts, self._alerts = self._alerts, []
            return alerts
