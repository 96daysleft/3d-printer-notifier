"""Snapmaker U1 (Klipper + Moonraker JSON-RPC)."""

import itertools
import json
from typing import Any, Dict, Literal, Optional, Union

from ...alerts import Alert
from ...config import PrinterConfig
from .. import register_printer_type
from ..base import PrinterClient
from .models import PrinterObjectsStatus, PrintState, PrintStats, VirtualSdcard

# Printer objects tracked via printer.objects.subscribe
SUBSCRIBE_OBJECTS = {
    "print_stats": None,
    "virtual_sdcard": None,
    "display_status": None,
}

SOURCE = "moonraker"


class U1PrinterConfig(PrinterConfig):
    type: Literal["u1"]
    port: int = 80   # Moonraker behind the U1's nginx
    # Only needed if the printer doesn't trust the notifier's network
    api_key: Optional[str] = None


@register_printer_type
class U1PrinterClient(PrinterClient):
    """
    Moonraker JSON-RPC 2.0 WebSocket client, as run on the Snapmaker U1.

    - URL:      ws://{host}:{port}/websocket
    - Requests: {"jsonrpc": "2.0", "method": ..., "params": {...}, "id": N}
    - Pushes:   {"jsonrpc": "2.0", "method": "notify_status_update", "params": [status, eventtime]}
    - Keepalive is standard WebSocket ping/pong frames.
    """

    type_name = "u1"
    display_name = "Snapmaker U1"
    config_model = U1PrinterConfig

    config: U1PrinterConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ids = itertools.count(1)
        # Retained between messages to detect changes
        self._last_state: Optional[str] = None
        self._last_progress: Optional[int] = None
        self._last_filename: Optional[str] = None

    # --- Connection -------------------------------------------------------

    @property
    def url(self) -> str:
        return f"ws://{self.config.host}:{self.config.port}/websocket"

    @property
    def default_snapshot_url(self) -> str:
        port = "" if self.config.port == 80 else f":{self.config.port}"
        return f"http://{self.config.host}{port}/server/files/camera/monitor.jpg"

    def connect_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"ping_interval": 20, "ping_timeout": 20, "max_size": 2**22}
        if self.config.api_key:
            kwargs["additional_headers"] = {"X-Api-Key": self.config.api_key}
        return kwargs

    async def on_open(self) -> None:
        # Fresh connection: report current state again rather than diffing
        # against whatever we saw before the disconnect.
        self._last_state = None
        await self._subscribe()

    async def _subscribe(self) -> None:
        await self._request("printer.objects.subscribe", {"objects": SUBSCRIBE_OBJECTS})
        self.log.debug("Subscribed to printer objects", objects=list(SUBSCRIBE_OBJECTS))

    async def _request(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        await self.send(json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": next(self._ids),
        }))

    # --- Message dispatch -------------------------------------------------

    async def handle_message(self, raw: Union[str, bytes]) -> None:
        msg = json.loads(raw)
        method = msg.get("method")
        params = msg.get("params") or []

        if method == "notify_status_update":
            if params and isinstance(params[0], dict):
                await self.process_status(params[0])
        elif method == "notify_klippy_ready":
            self.log.info("Klippy ready")
            # Klipper restarted: subscriptions don't survive that
            await self._subscribe()
        elif method in ("notify_klippy_disconnected", "notify_klippy_shutdown"):
            await self._klippy_down(method.removeprefix("notify_klippy_"))
        elif method == "notify_gcode_response":
            if params:
                await self._gcode_response(str(params[0]))
        elif "result" in msg:
            result = msg["result"]
            if isinstance(result, dict) and isinstance(result.get("status"), dict):
                await self.process_status(result["status"])
        elif "error" in msg:
            await self._rpc_error(msg.get("error") or {})

    # --- Alert rules ------------------------------------------------------

    async def process_status(self, status: Dict[str, Any]) -> None:
        """Handle a printer.objects status payload (subscribe result or push)."""
        parsed = PrinterObjectsStatus.model_validate(status)
        ps, vsd = parsed.print_stats, parsed.virtual_sdcard

        if ps is not None and ps.state is not None:
            self.log.info(
                "Printer status",
                state=ps.state,
                file=ps.filename,
                progress_pct=vsd.progress_pct if vsd else None,
            )
            await self._check_state_change(ps)

        # Moonraker pushes partial updates: a progress-only push has no
        # print_stats, so milestones rely on the state/filename tracked from
        # the last push that did include it.
        if vsd is not None and vsd.progress is not None:
            await self._check_progress(vsd)

    async def _check_state_change(self, ps: PrintStats) -> None:
        state, filename = ps.state, ps.filename
        # A partial push may omit filename: that does not mean it was cleared
        if filename is not None and filename != self._last_filename:
            self._last_progress = None
            self._last_filename = filename
        file_line = f"File: {self._last_filename}" if self._last_filename else None

        if self._last_state is None:
            parts = [f"State: {PrintState.label(state)}", file_line]
            await self.emit(Alert(
                title="Current Printer Status",
                description="\n".join(p for p in parts if p),
                source=f"{SOURCE}/print_stats",
            ))

        elif state != self._last_state:
            prev_label, curr_label = PrintState.label(self._last_state), PrintState.label(state)
            is_resume = state == "printing" and self._last_state == "paused"
            is_start = state == "printing" and not is_resume
            is_finish = state in ("complete", "standby") and self._last_state in ("printing", "paused")
            is_error = state in ("error", "cancelled")

            if is_start:
                if self.notify.on_print_start:
                    parts = ["Printer has started a new job.", file_line]
                    await self.emit(Alert(
                        title="Print Started",
                        description="\n".join(p for p in parts if p),
                        source=f"{SOURCE}/print_stats",
                    ))
            elif is_error:
                if self.notify.on_error:
                    parts = [f"Print job {curr_label.lower()}.", file_line,
                             f"Message: {ps.message}" if ps.message else None]
                    await self.emit(Alert(
                        title=f"Print {curr_label}",
                        description="\n".join(p for p in parts if p),
                        severity="high",
                        source=f"{SOURCE}/print_stats",
                    ))
            elif is_finish:
                if self.notify.on_print_finish:
                    parts = [f"Print job finished ({curr_label.lower()}).", file_line]
                    await self.emit(Alert(
                        title="Print Finished",
                        description="\n".join(p for p in parts if p),
                        source=f"{SOURCE}/print_stats",
                    ))
            else:
                await self.emit(Alert(
                    title="Printer State Changed",
                    description=f"State changed from {prev_label} to {curr_label}",
                    source=f"{SOURCE}/print_stats",
                ))

        self._last_state = state

    async def _check_progress(self, vsd: VirtualSdcard) -> None:
        progress = vsd.progress_pct
        if progress is None or self._last_state != "printing":
            return
        if self.notify.on_progress and self._last_progress is not None:
            for milestone in self.notify.progress_milestones:
                if self._last_progress < milestone <= progress:
                    await self.emit(Alert(
                        title=f"Print {milestone}% Complete",
                        description=f"{self._last_filename or 'Print'} is {milestone}% complete.",
                        source=f"{SOURCE}/virtual_sdcard",
                    ))
        self._last_progress = progress

    async def _klippy_down(self, state: str) -> None:
        self.log.warning("Klippy state changed", state=state)
        if not self.notify.on_error:
            return
        disconnected = state == "disconnected"
        await self.emit(Alert(
            title="Klipper Disconnected" if disconnected else "Klipper Shutdown",
            description=(
                "The printer's Klipper firmware connection was lost."
                if disconnected
                else "Klipper has entered an emergency shutdown state."
            ),
            severity="high",
            source=f"{SOURCE}/klippy",
        ))

    async def _gcode_response(self, line: str) -> None:
        if not self.notify.on_error:
            return
        if not (line.startswith("!!") or line.lower().startswith("error")):
            return
        await self.emit(Alert(
            title="Printer Error",
            description=line.lstrip("!").strip(),
            severity="high",
            source=f"{SOURCE}/gcode_response",
        ))

    async def _rpc_error(self, error: Dict[str, Any]) -> None:
        self.log.error("Moonraker JSON-RPC error", error=error)
        if not self.notify.on_error:
            return
        await self.emit(Alert(
            title="Moonraker Error",
            description=error.get("message", "A Moonraker error was reported."),
            severity="high",
            source=f"{SOURCE}/error",
            details=error or None,
        ))
