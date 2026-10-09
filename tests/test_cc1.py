import copy
import json

from conftest import fixture_json, run

CC1 = {"name": "Centauri", "type": "cc1", "host": "10.0.0.3", "mainboardId": "607315d30105041800009c0000000000"}


def status(current, filename="", layer=0, total=0, progress=0, error=0, task="t1"):
    msg = copy.deepcopy(fixture_json("cc1_status.json"))
    msg["Status"]["CurrentStatus"] = current
    msg["Status"]["PrintInfo"].update({
        "Filename": filename, "CurrentLayer": layer, "TotalLayer": total,
        "Progress": progress, "ErrorNumber": error, "TaskId": task,
    })
    return json.dumps(msg)


def titles(client):
    return [a.title for a in run(client.drain_alerts())]


def test_recorded_status_message(make_client):
    c = make_client(CC1)
    run(c.handle_message(json.dumps(fixture_json("cc1_status.json"))))
    alerts = run(c.drain_alerts())
    assert [a.title for a in alerts] == ["Current Machine Status"]
    assert "Status: Idle" in alerts[0].description
    # SDCP Unix timestamp carried through to the alert
    assert int(alerts[0].timestamp.timestamp()) == 1772302614


def test_recorded_attributes_message_is_not_an_alert(make_client):
    c = make_client(CC1)
    run(c.handle_message(json.dumps(fixture_json("cc1_attributes.json"))))
    assert titles(c) == []


def test_print_lifecycle_uses_layers_when_progress_stuck_at_zero(make_client):
    c = make_client(CC1)
    run(c.handle_message(status([0])))
    run(c.drain_alerts())

    run(c.handle_message(status([1], "cube.gcode", 0, 100)))
    for layer in (10, 30, 60, 90):
        run(c.handle_message(status([1], "cube.gcode", layer, 100)))
    run(c.handle_message(status([0], "cube.gcode", 100, 100)))

    assert titles(c) == [
        "Print Started", "Print 25% Complete", "Print 50% Complete",
        "Print 75% Complete", "Print Finished",
    ]


def test_new_job_resets_progress(make_client):
    c = make_client(CC1)
    run(c.handle_message(status([1], "a.gcode", progress=90, task="a")))
    run(c.handle_message(status([1], "b.gcode", progress=5, task="b")))
    run(c.handle_message(status([1], "b.gcode", progress=30, task="b")))
    assert titles(c)[-1] == "Print 25% Complete"


def test_print_error_and_notice(make_client):
    c = make_client(CC1)
    run(c.handle_message(status([1], "a.gcode", progress=10)))
    run(c.handle_message(status([1], "a.gcode", progress=10, error=2)))
    run(c.handle_message(status([1], "a.gcode", progress=10, error=2)))  # no repeat
    run(c.handle_message(json.dumps({
        "Topic": "sdcp/notice/x",
        "Data": {"Data": {"Title": "Filament runout", "Message": "Load filament", "Level": 2}},
    })))
    alerts = run(c.drain_alerts())
    assert [a.title for a in alerts] == ["Current Machine Status", "Print Error", "Filament runout"]
    assert alerts[1].description == "File Read Failed"
    assert alerts[2].severity == "high"


def test_failed_command_response(make_client):
    c = make_client(CC1)
    ok = {"Topic": "sdcp/response/x", "Data": {"Cmd": 0, "Data": {"Ack": 0}}}
    bad = {"Topic": "sdcp/response/x", "Data": {"Cmd": 0, "Data": {"Ack": 1}}}
    run(c.handle_message(json.dumps(ok)))
    run(c.handle_message(json.dumps(bad)))
    run(c.handle_message("pong"))
    assert titles(c) == ["SDCP Command Failed"]


def test_status_request_and_heartbeat(make_client):
    c = make_client(CC1)
    sent = []

    async def fake_send(payload):
        sent.append(payload)

    c.send = fake_send
    run(c.on_open())
    run(c.send_heartbeat())
    request = json.loads(sent[0])
    assert request["Topic"] == f"sdcp/request/{CC1['mainboardId']}"
    assert request["Data"]["Cmd"] == 0
    assert sent[1] == "ping"
    assert c.connect_kwargs()["ping_interval"] is None
    assert c.snapshot_url == "http://10.0.0.3:3031/video"
