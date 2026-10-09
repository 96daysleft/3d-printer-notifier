"""Runs one PrinterClient per configured printer and forwards their alerts to Discord."""

import asyncio
import signal
from typing import Dict, List, Optional

import aiohttp
from aiohttp import web

from . import __version__
from .config import AppConfig
from .discord import DiscordNotifier
from .log import logger
from .printers import create_printer_client
from .printers.base import PrinterClient

INITIAL_FLUSH_DELAY_SECS = 3


class NotifierService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.clients: List[PrinterClient] = []
        self._webhooks: Dict[str, str] = {}
        for printer in config.printers:
            if not printer.enabled:
                logger.info("Printer disabled, skipping", printer=printer.name)
                continue
            client = create_printer_client(printer, config.notify_for(printer))
            client.on_connect = self._on_printer_connect
            self.clients.append(client)
            self._webhooks[printer.name] = config.webhook_for(printer)

        self.notifier: Optional[DiscordNotifier] = None
        self._stop = asyncio.Event()
        self._background: set[asyncio.Task] = set()

    # --- Lifecycle --------------------------------------------------------

    async def run(self) -> None:
        logger.info(
            "Starting 3D Printer Notifier",
            version=__version__,
            printers=[f"{c.name} ({c.type_name})" for c in self.clients],
        )
        if not self.clients:
            logger.warning("No enabled printers configured; nothing to do")
            return

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop.set)

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": f"3d-printer-notifier/{__version__}"},
        ) as session:
            self.notifier = DiscordNotifier(session, self.config.max_alerts_per_poll)
            health = await self._start_health_server()

            tasks = [asyncio.create_task(c.run(), name=f"printer-{c.name}") for c in self.clients]
            tasks.append(asyncio.create_task(self._poll_loop(), name="poll"))
            logger.info("Alert polling started", interval_minutes=self.config.poll_interval_minutes)

            await self._stop.wait()
            logger.info("Shutting down")

            for client in self.clients:
                await client.stop()
            for task in tasks + list(self._background):
                task.cancel()
            await asyncio.gather(*tasks, *self._background, return_exceptions=True)

            # Don't drop alerts that arrived since the last poll
            await self._flush_all()
            await self._notify_stopped()
            if health:
                await health.cleanup()
        logger.info("Stopped")

    # --- Alert flow -------------------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.poll_interval_minutes * 60)
            await self._flush_all()

    async def _flush_all(self) -> None:
        for client in self.clients:
            await self._flush(client)

    async def _flush(self, client: PrinterClient) -> None:
        try:
            alerts = await client.drain_alerts()
            if not alerts:
                return
            client.log.info("Sending alerts to Discord", count=len(alerts))
            ok = await self.notifier.send_alerts(alerts, client, self._webhooks[client.name])
            if not ok:
                client.log.error("Failed to send alerts to Discord", count=len(alerts))
        except Exception as e:
            client.log.error("Error flushing alerts", error=str(e))

    def _on_printer_connect(self, client: PrinterClient) -> None:
        self._spawn(self._after_connect(client))

    async def _after_connect(self, client: PrinterClient) -> None:
        if client.notify.on_connect:
            await self.notifier.send_event(
                self._webhooks[client.name],
                title="Printer Connected",
                description=f"Connected to {client.display_name} at `{client.config.host}`",
                color=0x00FF00,
                username=client.name,
                footer=f"{client.display_name} · 3D Printer Notifier",
            )
        # Send the initial status now rather than waiting a full poll interval
        await asyncio.sleep(INITIAL_FLUSH_DELAY_SECS)
        await self._flush(client)

    async def _notify_stopped(self) -> None:
        names = ", ".join(c.name for c in self.clients)
        for webhook in sorted(set(self._webhooks.values())):
            try:
                await self.notifier.send_event(
                    webhook,
                    title="Service Stopped",
                    description=f"3D Printer Notifier has shut down. Monitored: {names}",
                    color=0x808080,
                    username=self.config.discord.username,
                )
            except Exception as e:
                logger.warning("Could not send shutdown notification", error=str(e))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # --- Health -----------------------------------------------------------

    async def _start_health_server(self) -> Optional[web.AppRunner]:
        """Liveness endpoint. Always 200 while the process runs: a powered-off
        printer is normal and must not get the pod restarted."""
        if not self.config.health_port:
            return None

        async def healthz(_: web.Request) -> web.Response:
            return web.json_response({
                "status": "ok",
                "printers": {
                    c.name: {"type": c.type_name, "connected": c.is_connected}
                    for c in self.clients
                },
            })

        app = web.Application()
        app.router.add_get("/healthz", healthz)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.config.health_port).start()
        logger.info("Health endpoint listening", port=self.config.health_port, path="/healthz")
        return runner
