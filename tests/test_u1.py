import json

from conftest import run

U1 = {"name": "U1", "type": "u1", "host": "10.0.0.1"}


def push(client, status):
    run(client.handle_message(json.dumps({
        "jsonrpc": "2.0", "method": "notify_status_update", "params": [status, 123.4],
    })))


def titles(client):
    return [a.title for a in run(client.drain_alerts())]


def test_print_lifecycle(make_client):
    c = make_client(U1)
    # Subscribe result carries the initial snapshot
    run(c.handle_message(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"status": {
        "print_stats": {"state": "standby", "filename": ""},
        "virtual_sdcard": {"progress": 0.0},
    }}})))
    assert titles(c) == ["Current Printer Status"]

    push(c, {"print_stats": {"state": "printing", "filename": "benchy.gcode"}})
    # Progress-only partial pushes (no print_stats)
    for p in (0.1, 0.3, 0.55, 0.8, 1.0):
        push(c, {"virtual_sdcard": {"progress": p}})
    push(c, {"print_stats": {"state": "complete"}})

    assert titles(c) == [
        "Print Started", "Print 25% Complete", "Print 50% Complete",
        "Print 75% Complete", "Print Finished",
    ]


def test_pause_resume_is_not_a_new_print(make_client):
    c = make_client(U1)
    push(c, {"print_stats": {"state": "printing", "filename": "a.gcode"}})
    run(c.drain_alerts())
    push(c, {"print_stats": {"state": "paused"}})
    push(c, {"print_stats": {"state": "printing"}})
    assert titles(c) == ["Printer State Changed", "Printer State Changed"]


def test_errors_and_notify_toggles(make_client):
    c = make_client(U1, on_progress=False, on_print_start=False)
    push(c, {"print_stats": {"state": "standby"}})
    push(c, {"print_stats": {"state": "printing", "filename": "a.gcode"}})
    push(c, {"virtual_sdcard": {"progress": 0.9}})
    push(c, {"virtual_sdcard": {"progress": 0.95}})
    push(c, {"print_stats": {"state": "error", "message": "Heater extruder not heating"}})
    run(c.handle_message(json.dumps({"method": "notify_gcode_response", "params": ["!! Move out of range"]})))
    run(c.handle_message(json.dumps({"method": "notify_gcode_response", "params": ["ok"]})))
    run(c.handle_message(json.dumps({"method": "notify_klippy_shutdown", "params": []})))

    alerts = run(c.drain_alerts())
    assert [a.title for a in alerts] == [
        "Current Printer Status", "Print Error", "Printer Error", "Klipper Shutdown",
    ]
    assert "Heater extruder not heating" in alerts[1].description
    assert alerts[1].severity == "high"


def test_klippy_ready_resubscribes(make_client):
    c = make_client(U1)
    sent = []

    async def fake_send(payload):
        sent.append(json.loads(payload))

    c.send = fake_send
    run(c.handle_message(json.dumps({"method": "notify_klippy_ready"})))
    assert sent[0]["method"] == "printer.objects.subscribe"


def test_connection_details(make_client):
    c = make_client({**U1, "apiKey": "k", "port": 7125})
    assert c.url == "ws://10.0.0.1:7125/websocket"
    assert c.connect_kwargs()["additional_headers"] == {"X-Api-Key": "k"}
    assert c.snapshot_url == "http://10.0.0.1:7125/server/files/camera/monitor.jpg"
    assert make_client(U1).snapshot_url == "http://10.0.0.1/server/files/camera/monitor.jpg"
    custom = make_client({**U1, "camera": {"snapshotUrl": "http://cam/snap.jpg"}})
    assert custom.snapshot_url == "http://cam/snap.jpg"
