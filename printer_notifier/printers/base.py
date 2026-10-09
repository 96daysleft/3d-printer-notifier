"""The interface every printer type implements.

A printer type is a ``PrinterClient`` subclass plus a ``PrinterConfig``
subclass, registered with ``@register_printer_type``. The base class owns
the WebSocket connection lifecycle (connect, heartbeat, reconnect with
backoff) and the alert buffer; a subclass only has to describe how to reach
the printer and how to turn its messages into ``Alert`` objects.

Adding a new printer type:

1. Create ``printer_notifier/printers/<type>/`` with a config model::

       class FooPrinterConfig(PrinterConfig):
           type: Literal["foo"]
           port: int = 1234
           some_token: Optional[str] = None

2. Implement the client::

       @register_printer_type
       class FooPrinterClient(PrinterClient):
           type_name = "foo"
           display_name = "Foo Printer 3000"
           config_model = FooPrinterConfig

           @property
           def url(self) -> str: ...
           async def on_open(self) -> None: ...        # subscribe / request status
           async def handle_message(self, raw) -> None: # parse, then self.emit(Alert(...))

3. Import the module in ``printer_notifier/printers/__init__.py``.
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Callable, ClassVar, Dict, Optional, Type, Union

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from ..alerts import Alert, AlertBuffer
from ..config import NotifyConfig, PrinterConfig
from ..log import logger


class PrinterClient(ABC):
    """Connection + alert source for a single printer."""

    # --- Set by each printer type ----------------------------------------
    type_name: ClassVar[str]                  # value of `type:` in the config
    display_name: ClassVar[str]               # human-readable model name
    config_model: ClassVar[Type[PrinterConfig]]
    # Seconds between application-level heartbeats (send_heartbeat); None = off
    heartbeat_interval: ClassVar[Optional[float]] = None

    RECONNECT_MIN_SECS: ClassVar[float] = 10
    RECONNECT_MAX_SECS: ClassVar[float] = 120

    def __init__(self, config: PrinterConfig, notify: NotifyConfig) -> None:
        self.config = config
        self.notify = notify
        self.log = logger.bind(printer=config.name, type=self.type_name)
        self.ws: Optional[ClientConnection] = None
        self.is_connected = False
        self.reconnect_attempts = 0
        self._running = False
        self._alerts = AlertBuffer()
        # Called (synchronously) after each successful connect + on_open
        self.on_connect: Optional[Callable[["PrinterClient"], None]] = None

    # --- Printer-type hooks ---------------------------------------------

    @property
    @abstractmethod
    def url(self) -> str:
        """WebSocket URL to connect to."""

    @property
    def default_snapshot_url(self) -> Optional[str]:
        """Camera URL used when camera.snapshotUrl is not configured."""
        return None

    def connect_kwargs(self) -> Dict[str, Any]:
        """Extra keyword args for websockets.connect (headers, ping settings)."""
        return {}

    async def before_connect(self) -> None:
        """Runs before every connection attempt (e.g. discovery). Raise to retry later."""

    async def on_open(self) -> None:
        """Runs once the socket is open (subscribe, request initial status)."""

    @abstractmethod
    async def handle_message(self, raw: Union[str, bytes]) -> None:
        """Process one raw WebSocket message; call self.emit() for alerts."""

    async def send_heartbeat(self) -> None:
        """Send an application-level keepalive. Only called if heartbeat_interval is set."""

    # --- Shared behaviour -----------------------------------------------

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def snapshot_url(self) -> Optional[str]:
        return self.config.camera.snapshot_url or self.default_snapshot_url

    async def emit(self, alert: Alert) -> None:
        """Buffer an alert for the next Discord flush."""
        await self._alerts.add(alert)
        self.log.info("Alert buffered", title=alert.title, severity=alert.severity)

    async def drain_alerts(self) -> list[Alert]:
        return await self._alerts.drain()

    async def send(self, payload: str) -> None:
        if self.ws is not None:
            await self.ws.send(payload)

    async def run(self) -> None:
        """Connect and keep reconnecting until stop() is called."""
        self._running = True
        while self._running:
            try:
                await self.before_connect()
                self.log.info("Connecting", url=self.url, attempt=self.reconnect_attempts + 1)
                async with connect(self.url, open_timeout=10, **self.connect_kwargs()) as ws:
                    self.ws = ws
                    self.is_connected = True
                    self.reconnect_attempts = 0
                    self.log.info("Connected")
                    await self.on_open()
                    if self.on_connect:
                        self.on_connect(self)
                    await self._read_loop(ws)
                self.log.info("Connection closed by printer")
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as e:
                self.log.warning("Connection closed", code=e.rcvd.code if e.rcvd else None)
            except Exception as e:
                # A powered-off printer is the common case: keep this quiet-ish
                log = self.log.warning if self.reconnect_attempts == 0 else self.log.debug
                log("Connection failed", error=str(e) or type(e).__name__)
            finally:
                self.ws = None
                self.is_connected = False

            if not self._running:
                break
            self.reconnect_attempts += 1
            delay = min(
                self.RECONNECT_MIN_SECS * 2 ** (self.reconnect_attempts - 1),
                self.RECONNECT_MAX_SECS,
            )
            self.log.info("Reconnecting", delay_secs=delay, attempt=self.reconnect_attempts)
            await asyncio.sleep(delay)

    async def _read_loop(self, ws: ClientConnection) -> None:
        heartbeat = (
            asyncio.create_task(self._heartbeat_loop()) if self.heartbeat_interval else None
        )
        try:
            async for raw in ws:
                try:
                    await self.handle_message(raw)
                except Exception as e:
                    # One bad message must not drop the connection
                    self.log.error("Failed to handle message", error=str(e), raw=str(raw)[:200])
        finally:
            if heartbeat:
                heartbeat.cancel()

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await self.send_heartbeat()
            except Exception as e:
                self.log.debug("Heartbeat failed", error=str(e))

    async def stop(self) -> None:
        self._running = False
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
