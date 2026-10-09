"""Runs the real service against fake printers and a fake Discord webhook."""

import asyncio
import json

from aiohttp import web
from websockets.asyncio.server import serve

from printer_notifier import service as service_module
from printer_notifier.config import AppConfig
from printer_notifier.printers import cc1 as cc1_module
from printer_notifier.service import NotifierService

from conftest import fixture_json

JPEG = b"\xff\xd8fake-jpeg\xff\xd9"
MAINBOARD_ID = "607315d30105041800009c0000000000"


async def fake_moonraker(ws):
    async for raw in ws:
        req = json.loads(raw)
        if req["method"] == "printer.objects.subscribe":
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"status": {
                "print_stats": {"state": "printing", "filename": "benchy.gcode"},
            }}}))


async def fake_sdcp(ws):
    async for raw in ws:
        if raw == "ping":
            await ws.send("pong")
            continue
        req = json.loads(raw)
        assert req["Data"]["MainboardID"] == MAINBOARD_ID
        await ws.send(json.dumps(fixture_json("cc1_status.json")))


class DiscoveryResponder(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        assert data == b"M99999"
        reply = {"Id": "x", "Data": {"Name": "Centauri Carbon", "MainboardID": MAINBOARD_ID}}
        self.transport.sendto(json.dumps(reply).encode(), addr)


async def start_http(posts):
    async def webhook(request: web.Request):
        if request.content_type.startswith("multipart/"):
            form = await request.post()
            posts.append({
                "hook": request.match_info["hook"],
                "payload": json.loads(form["payload_json"]),
                "image": form["files[0]"].file.read(),
            })
            return web.json_response({}, status=200)
        posts.append({"hook": request.match_info["hook"], "payload": await request.json(), "image": None})
        return web.Response(status=204)

    async def snapshot(_):
        return web.Response(body=JPEG, content_type="image/jpeg")

    app = web.Application()
    app.router.add_post("/hooks/{hook}", webhook)
    app.router.add_get("/snapshot.jpg", snapshot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def test_two_printer_types_end_to_end(monkeypatch):
    monkeypatch.setattr(service_module, "INITIAL_FLUSH_DELAY_SECS", 0.2)

    async def scenario():
        posts = []
        http, http_port = await start_http(posts)
        loop = asyncio.get_running_loop()
        udp, _ = await loop.create_datagram_endpoint(DiscoveryResponder, local_addr=("127.0.0.1", 0))
        monkeypatch.setattr(cc1_module, "DISCOVERY_PORT", udp.get_extra_info("sockname")[1])

        async with serve(fake_moonraker, "127.0.0.1", 0) as u1_srv, serve(fake_sdcp, "127.0.0.1", 0) as cc1_srv:
            base = f"http://127.0.0.1:{http_port}"
            config = AppConfig.model_validate({
                "healthPort": 0,
                "discord": {"webhookUrl": f"{base}/hooks/default"},
                "printers": [
                    {"name": "U1 Left", "type": "u1", "host": "127.0.0.1",
                     "port": u1_srv.sockets[0].getsockname()[1],
                     "camera": {"snapshotUrl": f"{base}/snapshot.jpg"}},
                    {"name": "Centauri", "type": "cc1", "host": "127.0.0.1",
                     "port": cc1_srv.sockets[0].getsockname()[1],
                     "discordWebhookUrl": f"{base}/hooks/cc1",
                     "notify": {"withSnapshot": False}},
                ],
            })
            svc = NotifierService(config)
            run_task = asyncio.create_task(svc.run())

            # Wait for each printer's "connected" + initial status message
            for _ in range(100):
                if len(posts) >= 4:
                    break
                await asyncio.sleep(0.05)
            assert all(c.is_connected for c in svc.clients)
            assert svc.clients[1].mainboard_id == MAINBOARD_ID   # discovered over UDP

            svc._stop.set()
            await asyncio.wait_for(run_task, 10)

        udp.close()
        await http.cleanup()
        return posts

    posts = asyncio.run(scenario())

    def embeds_for(hook, username):
        return [
            (e["title"], p["image"])
            for p in posts if p["hook"] == hook and p["payload"].get("username") == username
            for e in p["payload"]["embeds"]
        ]

    assert embeds_for("default", "U1 Left") == [
        ("Printer Connected", None),
        ("🟢 Current Printer Status", JPEG),
    ]
    assert embeds_for("cc1", "Centauri") == [
        ("Printer Connected", None),
        ("🟢 Current Machine Status", None),
    ]
    # Shutdown notice goes once to each distinct webhook
    stopped = sorted(p["hook"] for p in posts if p["payload"]["embeds"][0]["title"] == "Service Stopped")
    assert stopped == ["cc1", "default"]
