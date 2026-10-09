"""The config.yaml the Helm chart renders must be accepted by the app."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from printer_notifier.config import load_config

ROOT = Path(__file__).parent.parent
CHART = ROOT / "helm" / "3d-printer-notifier"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def render(*args: str) -> list:
    out = subprocess.run(
        ["helm", "template", "test", str(CHART), *args],
        check=True, capture_output=True, text=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def test_rendered_config_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/default")
    monkeypatch.setenv("GARAGE_WEBHOOK_URL", "https://discord.example/garage")
    docs = render("-f", str(ROOT / "examples" / "values-multi-printer.yaml"))

    secret = next(d for d in docs if d["kind"] == "Secret")
    path = tmp_path / "config.yaml"
    path.write_text(secret["stringData"]["config.yaml"])
    config = load_config(str(path))

    assert [(p.name, p.type) for p in config.printers] == [
        ("U1 Left", "u1"), ("U1 Right", "u1"), ("Centauri", "cc1"), ("Centauri Garage", "cc1"),
    ]
    garage = config.printers[3]
    assert config.webhook_for(garage) == "https://discord.example/garage"
    assert config.notify_for(garage).on_progress is False


def test_existing_secret_skips_rendering():
    docs = render("--set", "existingSecret=my-config")
    assert [d["kind"] for d in docs] == ["Deployment"]
    volume = docs[0]["spec"]["template"]["spec"]["volumes"][0]
    assert volume["secret"]["secretName"] == "my-config"


def test_no_printers_fails():
    with pytest.raises(subprocess.CalledProcessError) as e:
        render()
    assert "at least one printer" in e.value.stderr
