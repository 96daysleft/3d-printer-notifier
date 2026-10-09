"""Elegoo Centauri Carbon (SDCP v3 over WebSocket)."""

import asyncio
import json
import time
import uuid
from typing import Any, Dict, Literal, Optional, Union

from ...alerts import Alert
from ...config import PrinterConfig
from .. import register_printer_type
from ..base import PrinterClient
from .models import AttributesMessage, MachineStatus, PrintErrorCode, StatusMessage, StatusPayload

SOURCE = "sdcp"
DISCOVERY_PORT = 3000
DISCOVERY_PROBE = b"M99999"
_NOTICE_SEVERITY = {0: "low", 1: "medium", 2: "high", 3: "critical"}


class CC1PrinterConfig(PrinterConfig):
    type: Literal["cc1"]
    port: int = 3030
    # Needed for SDCP topic routing. Discovered over UDP when left empty.
    mainboard_id: Optional[str] = None


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, future: "asyncio.Future[Dict[str, Any]]") -> None:
        self.future = future

    def datagram_received(self, data: bytes, addr: Any) -> None:
        if not self.future.done():
            try:
                self.future.set_result(json.loads(data))
            except ValueError as e:
                self.future.set_exception(e)

    def error_received(self, exc: Exception) -> None:
        if not self.future.done():
            self.future.set_exception(exc)


async def discover_mainboard_id(host: str, timeout: float = 3.0) -> str:
    """Ask the printer for its MainboardID via SDCP UDP discovery."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Dict[str, Any]] = loop.create_future()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _DiscoveryProtocol(future), remote_addr=(host, DISCOVERY_PORT)
    )
    try:
        transport.sendto(DISCOVERY_PROBE)
        reply = await asyncio.wait_for(future, timeout)
    finally:
        transport.close()
    mainboard_id = (reply.get("Data") or {}).get("MainboardID")
    if not mainboard_id:
        raise ValueError(f"discovery reply did not include a MainboardID: {reply}")
    return mainboard_id


@register_printer_type
class CC1PrinterClient(PrinterClient):
    """
    SDCP WebSocket client for the Elegoo Centauri Carbon.

    - URL:       ws://{host}:3030/websocket
    - Heartbeat: text "ping" -> "pong" (NOT WebSocket ping frames)
    - Messages:  JSON routed by Topic: sdcp/{status,notice,response,error,attributes}/{MainboardID}
    - Requests:  Topic sdcp/request/{MainboardID} with a Data.Cmd field
    """

    type_name = "cc1"
    display_name = "Elegoo Centauri Carbon"
    config_model = CC1PrinterConfig
    heartbeat_interval = 30

    config: CC1PrinterConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.mainboard_id: Optional[str] = self.config.mainboard_id
        # Retained between messages to detect changes
        self._last_status: Optional[list] = None
        self._last_progress: Optional[int] = None
        self._last_task: Optional[str] = None
        self._last_error: int = 0

    # --- Connection -------------------------------------------------------

    @property
    def url(self) -> str:
        return f"ws://{self.config.host}:{self.config.port}/websocket"

    @property
    def default_snapshot_url(self) -> str:
        return f"http://{self.config.host}:3031/video"

    def connect_kwargs(self) -> Dict[str, Any]:
        # SDCP uses text ping/pong; protocol-level pings are not answered
        return {"ping_interval": None, "max_size": 2**22}

    async def before_connect(self) -> None:
        if self.mainboard_id:
            return
        self.mainboard_id = await discover_mainboard_id(self.config.host)
        self.log.info(
            "Discovered mainboard ID (set mainboardId to skip discovery)",
            mainboard_id=self.mainboard_id,
        )

    async def on_open(self) -> None:
        self._last_status = None
        await self._request(cmd=0)   # current status

    async def send_heartbeat(self) -> None:
        await self.send("ping")

    async def _request(self, cmd: int, data: Optional[Dict[str, Any]] = None) -> None:
        await self.send(json.dumps({
            "Id": str(uuid.uuid4()),
            "Data": {
                "Cmd": cmd,
                "Data": data or {},
                "RequestID": str(uuid.uuid4()),
                "MainboardID": self.mainboard_id,
                "TimeStamp": int(time.time()),
                "From": 0,
            },
            "Topic": f"sdcp/request/{self.mainboard_id}",
        }))

    # --- Message dispatch -------------------------------------------------

    async def handle_message(self, raw: Union[str, bytes]) -> None:
        if raw == "pong":
            return
        msg = json.loads(raw)
        # sdcp/<kind>/<MainboardID>
        kind = msg.get("Topic", "").split("/")[1:2]
        kind = kind[0] if kind else ""

        if kind == "status":
            await self.process_status(msg)
        elif kind == "notice":
            await self._notice(msg)
        elif kind == "error":
            await self._sdcp_error(msg)
        elif kind == "response":
            await self._response(msg)
        elif kind == "attributes":
            a = AttributesMessage.model_validate(msg).Attributes
            self.log.info(
                "Machine attributes",
                machine=a.MachineName, brand=a.BrandName,
                firmware=a.FirmwareVersion, protocol=a.ProtocolVersion,
            )
        else:
            self.log.debug("Unhandled topic", topic=msg.get("Topic"))

    # --- Alert rules ------------------------------------------------------

    async def process_status(self, msg: Dict[str, Any]) -> None:
        status_msg = StatusMessage.model_validate(msg)
        s = status_msg.Status
        ts = status_msg.TimeStamp or int(time.time())
        pi = s.print_info
        self.log.info(
            "Machine status",
            status=MachineStatus.labels(s.CurrentStatus or []),
            nozzle=s.TempOfNozzle, bed=s.TempOfHotbed,
            file=pi.Filename if pi else None,
            layers=pi.layers if pi else None,
            progress=pi.progress_pct if pi else None,
        )
        await self._check_status_change(s, ts)
        await self._check_print_error(s, ts)
        await self._check_progress(s, ts)

    async def _check_status_change(self, s: StatusPayload, ts: int) -> None:
        current = s.CurrentStatus or []
        pi = s.print_info
        file_line = f"File: {pi.Filename}" if pi and pi.Filename else None

        if self._last_status is None:
            parts = [f"Status: {MachineStatus.labels(current)}"]
            if file_line and MachineStatus.PRINTING in current:
                parts += [file_line, f"Progress: {pi.progress_pct}%"]
            await self.emit(Alert(
                title="Current Machine Status",
                description="\n".join(parts),
                timestamp=ts,
                source=f"{SOURCE}/status",
            ))

        elif current != self._last_status:
            prev = self._last_status
            was_printing = MachineStatus.PRINTING in prev
            is_printing = MachineStatus.PRINTING in current

            if is_printing and not was_printing:
                if self.notify.on_print_start:
                    parts = ["Printer has started a new job.", file_line]
                    await self.emit(Alert(
                        title="Print Started",
                        description="\n".join(p for p in parts if p),
                        timestamp=ts,
                        source=f"{SOURCE}/status",
                    ))
            elif was_printing and not is_printing:
                if self.notify.on_print_finish:
                    parts = ["Print job finished.", file_line]
                    await self.emit(Alert(
                        title="Print Finished",
                        description="\n".join(p for p in parts if p),
                        timestamp=ts,
                        source=f"{SOURCE}/status",
                    ))
            else:
                await self.emit(Alert(
                    title="Machine Status Changed",
                    description=(
                        f"Status changed from {MachineStatus.labels(prev)} "
                        f"to {MachineStatus.labels(current)}"
                    ),
                    timestamp=ts,
                    source=f"{SOURCE}/status",
                ))

        self._last_status = current

    async def _check_print_error(self, s: StatusPayload, ts: int) -> None:
        error = (s.print_info.ErrorNumber if s.print_info else None) or 0
        if error and error != self._last_error and self.notify.on_error:
            await self.emit(Alert(
                title="Print Error",
                description=PrintErrorCode.label(error),
                severity="high",
                timestamp=ts,
                source=f"{SOURCE}/status",
                details={"ErrorNumber": error, "File": s.print_info.Filename},
            ))
        self._last_error = error

    async def _check_progress(self, s: StatusPayload, ts: int) -> None:
        pi = s.print_info
        if pi is None or MachineStatus.PRINTING not in (s.CurrentStatus or []):
            return
        progress = pi.progress_pct
        if progress is None:
            return
        # New job: forget the previous job's progress
        task = pi.TaskId or pi.Filename
        if task != self._last_task:
            self._last_task = task
            self._last_progress = None

        if self.notify.on_progress and self._last_progress is not None:
            for milestone in self.notify.progress_milestones:
                if self._last_progress < milestone <= progress:
                    layers = f" ({pi.layers} layers)" if pi.layers else ""
                    await self.emit(Alert(
                        title=f"Print {milestone}% Complete",
                        description=f"{pi.Filename or 'Print'} is {milestone}% complete{layers}.",
                        timestamp=ts,
                        source=f"{SOURCE}/status",
                    ))
        self._last_progress = progress

    async def _notice(self, msg: Dict[str, Any]) -> None:
        data = (msg.get("Data") or {}).get("Data") or {}
        await self.emit(Alert(
            title=data.get("Title", "Printer Notice"),
            description=data.get("Message", json.dumps(data)),
            severity=_NOTICE_SEVERITY.get(data.get("Level", 0), "low"),
            source=f"{SOURCE}/notice",
            details=data or None,
        ))

    async def _sdcp_error(self, msg: Dict[str, Any]) -> None:
        data = msg.get("Data") or {}
        self.log.error("SDCP error", data=data)
        if not self.notify.on_error:
            return
        await self.emit(Alert(
            title="SDCP Error",
            description=json.dumps(data) if data else "The printer reported an SDCP error.",
            severity="high",
            source=f"{SOURCE}/error",
            details=data or None,
        ))

    async def _response(self, msg: Dict[str, Any]) -> None:
        """Command acknowledgements: only alert when one failed."""
        outer = msg.get("Data") or {}
        data = outer.get("Data") or {}
        cmd, result = outer.get("Cmd"), data.get("Ack", data.get("Result", 0))
        self.log.debug("SDCP response", cmd=cmd, result=result)
        if result and self.notify.on_error:
            await self.emit(Alert(
                title="SDCP Command Failed",
                description=f"Command {cmd} failed with result code {result}.",
                severity="high",
                source=f"{SOURCE}/response",
                details=data,
            ))
