"""Discord webhook notifier."""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp
from pydantic import BaseModel

from .alerts import Alert
from .log import logger
from .printers.base import PrinterClient

SEVERITY_COLORS = {
    "low": 0x00FF00,
    "medium": 0xFFFF00,
    "high": 0xFF8800,
    "critical": 0xFF0000,
}
SEVERITY_EMOJIS = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}
FOOTER = "3D Printer Notifier"
MAX_SNAPSHOT_BYTES = 2_000_000


class DiscordEmbed(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    color: Optional[int] = None
    timestamp: Optional[str] = None
    fields: Optional[List[Dict[str, Any]]] = None
    footer: Optional[Dict[str, str]] = None
    image: Optional[Dict[str, str]] = None


class DiscordMessage(BaseModel):
    username: Optional[str] = None
    avatar_url: Optional[str] = None
    content: Optional[str] = None
    embeds: Optional[List[DiscordEmbed]] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DiscordNotifier:
    """Sends alerts to Discord, labelled with the printer they came from."""

    def __init__(self, session: aiohttp.ClientSession, max_alerts_per_message: int = 5) -> None:
        self.session = session
        self.max_alerts_per_message = max_alerts_per_message

    # --- Embeds -----------------------------------------------------------

    @staticmethod
    def alert_embed(alert: Alert, printer: PrinterClient) -> DiscordEmbed:
        fields = [{"name": "Severity", "value": alert.severity.upper(), "inline": True}]
        if alert.source:
            fields.append({"name": "Source", "value": alert.source, "inline": True})
        if alert.details:
            details = "\n".join(f"**{k}**: {v}" for k, v in alert.details.items())
            if len(details) > 1000:
                details = details[:997] + "..."
            fields.append({"name": "Details", "value": details, "inline": False})

        return DiscordEmbed(
            title=f"{SEVERITY_EMOJIS[alert.severity]} {alert.title}",
            description=alert.description,
            color=SEVERITY_COLORS[alert.severity],
            timestamp=alert.timestamp.isoformat(),
            fields=fields,
            footer={"text": f"{printer.display_name} · {FOOTER}"},
        )

    @staticmethod
    def summary_embed(alerts: List[Alert], printer: PrinterClient) -> DiscordEmbed:
        counts: Dict[str, int] = {}
        for alert in alerts:
            counts[alert.severity] = counts.get(alert.severity, 0) + 1
        return DiscordEmbed(
            title=f"📊 {printer.name}: Alert Summary",
            color=0x3498DB,
            fields=[{"name": sev.upper(), "value": str(n), "inline": True} for sev, n in counts.items()],
            timestamp=_now(),
            footer={"text": f"Total: {len(alerts)} alert{'s' if len(alerts) != 1 else ''}"},
        )

    # --- Sending ----------------------------------------------------------

    async def send_alerts(self, alerts: List[Alert], printer: PrinterClient, webhook_url: str) -> bool:
        """Send a printer's buffered alerts as one message (with a camera frame)."""
        if not alerts:
            return True

        if len(alerts) == 1:
            embeds = [self.alert_embed(alerts[0], printer)]
            content = None
        else:
            shown = alerts[: self.max_alerts_per_message]
            embeds = [self.summary_embed(alerts, printer)] + [self.alert_embed(a, printer) for a in shown]
            content = f"🚨 **{len(alerts)} new alerts from {printer.name}**"
            if len(alerts) > len(shown):
                content += f" (showing {len(shown)})"

        snapshot = await self.fetch_snapshot(printer.snapshot_url) if printer.notify.with_snapshot else None
        if snapshot:
            embeds[0].image = {"url": "attachment://snapshot.jpg"}

        message = DiscordMessage(username=printer.name, content=content, embeds=embeds)
        return await self.send_message(webhook_url, message, image=snapshot)

    async def send_event(
        self,
        webhook_url: str,
        title: str,
        description: str,
        color: int,
        username: str,
        footer: str = FOOTER,
    ) -> bool:
        """Send a one-off status message (connected, service stopped...)."""
        message = DiscordMessage(
            username=username,
            embeds=[DiscordEmbed(
                title=title, description=description, color=color,
                timestamp=_now(), footer={"text": footer},
            )],
        )
        return await self.send_message(webhook_url, message)

    async def send_message(
        self, webhook_url: str, message: DiscordMessage, image: Optional[bytes] = None
    ) -> bool:
        payload = message.model_dump(exclude_none=True)
        for attempt in range(2):
            if image:
                form = aiohttp.FormData()
                form.add_field("payload_json", json.dumps(payload), content_type="application/json")
                form.add_field("files[0]", image, filename="snapshot.jpg", content_type="image/jpeg")
                kwargs: Dict[str, Any] = {"data": form}
            else:
                kwargs = {"json": payload}
            try:
                async with self.session.post(webhook_url, **kwargs) as response:
                    # 204 for JSON posts, 200 for multipart (file upload) posts
                    if response.status in (200, 204):
                        return True
                    if response.status == 429 and attempt == 0:
                        retry_after = float((await response.json()).get("retry_after", 1))
                        logger.warning("Discord rate limited", retry_after=retry_after)
                        await asyncio.sleep(min(retry_after, 30))
                        continue
                    logger.error(
                        "Discord webhook error",
                        status=response.status,
                        response=(await response.text())[:500],
                    )
                    return False
            except Exception as e:
                logger.error("Failed to send Discord message", error=str(e))
                return False
        return False

    async def fetch_snapshot(self, url: Optional[str]) -> Optional[bytes]:
        """Grab one JPEG frame from a snapshot endpoint or an MJPEG stream."""
        if not url:
            return None
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status != 200:
                    logger.warning("Camera snapshot request failed", url=url, status=response.status)
                    return None

                content_type = response.headers.get("Content-Type", "")
                if "multipart" not in content_type and "x-mixed-replace" not in content_type:
                    return await response.read()

                # MJPEG: take the first complete JPEG (SOI ... EOI) from the stream
                buf = b""
                async for chunk in response.content.iter_chunked(4096):
                    buf += chunk
                    start = buf.find(b"\xff\xd8")
                    end = buf.find(b"\xff\xd9", start + 2) if start != -1 else -1
                    if start != -1 and end != -1:
                        return buf[start:end + 2]
                    if len(buf) > MAX_SNAPSHOT_BYTES:
                        break
                logger.warning("No JPEG frame found in camera stream", url=url)
                return None
        except Exception as e:
            logger.warning("Failed to fetch camera snapshot", url=url, error=str(e) or type(e).__name__)
            return None
