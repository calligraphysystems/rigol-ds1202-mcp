import os
import time

import pytest

from rigol_mcp.advanced import Advanced
from rigol_mcp.artifacts import Artifacts
from rigol_mcp.operations import discover
from rigol_mcp.scope import Scope
from rigol_mcp.simulator import Simulator


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sim = Simulator()
    scope = Scope(sim.connect, allow_control=True, simulated=True)
    return sim, scope, Advanced(scope, Artifacts(tmp_path / "images"))


@pytest.mark.parametrize("op,args", [
    ("channel.set", {"channel": True, "enabled": False}),
    ("channel.set", {"channel": 3, "enabled": False}),
    ("channel.set", {"channel": 1, "enabled": 1}),
    ("channel.set", {"channel": 1, "coupling": "DC;:RUN"}),
    ("channel.set", {"channel": 1, "probe_ratio": 3}),
    ("channel.set", {"channel": 1, "scale_per_division": float("nan")}),
    ("channel.set", {"channel": 1, "offset": float("inf")}),
    ("channel.set", {"channel": 1}),
    ("channel.set", {"channel": 1, "typo": 1}),
    ("trigger.edge.set", {}),
    ("trigger.edge.set", {"source": "CHAN3"}),
    ("trigger.edge.set", {"holdoff_seconds": 11}),
    ("trigger.edge.set", {"holdoff_seconds": 0}),
    ("system.maintenance", {}),
    ("*RST", {}),
    ("channel.get", {"channel": 1}),
])
def test_invalid_request_rejected_before_instrument_access(rig, op, args):
    sim, _, advanced = rig
    with pytest.raises(ValueError):
        advanced.control(op, args)
    assert sim.commands == []


@pytest.mark.parametrize("op,args", [
    ("channel.set", {"channel": 1, "scale_per_division": 101}),
    ("channel.set", {"channel": 1, "scale_per_division": -.1}),
    ("channel.set", {"channel": 1, "offset": 21}),
    ("channel.set", {"channel": 1, "deskew_seconds": 1e-8}),
    ("channel.set", {"channel": 1, "range": 8, "scale_per_division": 1}),
    ("trigger.edge.set", {"level": 6}),
    ("trigger.edge.set", {"source": "EXT", "level": 0}),
    ("trigger.edge.set", {"source": "AC", "coupling": "DC"}),
])
def test_dependent_limits_fail_before_any_writes(rig, op, args):
    sim, _, advanced = rig
    with pytest.raises(ValueError):
        advanced.control(op, args)
    assert all(c.endswith("?") for c in sim.commands)


def test_read_dispatch_cannot_write_and_control_gate_precedes_io(rig):
    sim, scope, advanced = rig
    with pytest.raises(ValueError):
        advanced.read("channel.set", {"channel": 1, "enabled": False})
    scope.allow_control = False
    with pytest.raises(PermissionError):
        advanced.control("channel.set", {"channel": 1, "enabled": False})
    assert sim.commands == []


def test_channel_coupled_settings_and_alias(rig):
    sim, _, advanced = rig
    result = advanced.control("channel.set", {"channel": 1, "probe_ratio": 1, "range": 4,
        "offset": 99, "coupling": "AC", "inverted": True, "bandwidth_limit": "20M"})
    assert result["ok"]
    assert result["actual"]["scale_per_division"] == .5
    assert result["actual"]["offset"] == 99
    assert sim.commands.index(":CHAN1:PROB 1") < sim.commands.index(":CHAN1:RANG 4") < sim.commands.index(":CHAN1:OFFS 99")
    assert advanced.read("channel.get", {"channel": 1})["coupling"] == "AC"


def test_deskew_small_timebase_and_unchanged_locked_value(rig):
    sim, _, advanced = rig
    sim.timebase = 1e-6
    assert advanced.control("channel.set", {"channel": 2, "deskew_seconds": 2e-8})["ok"]
    sim.timebase = .001
    sim.commands.clear()
    assert advanced.control("channel.set", {"channel": 2, "deskew_seconds": 2e-8})["ok"]
    assert ":CHAN2:TCAL 2e-08" not in sim.commands


def test_trigger_level_depends_on_selected_source_offset(rig):
    sim, _, advanced = rig
    sim.settings.update({":TRIG:MODE": "PULS", ":CHAN2:SCAL": ".5", ":CHAN2:OFFS": "-1"})
    result = advanced.control("trigger.edge.set", {"source": "CHAN2", "level": 3.5, "slope": "NEG"})
    assert result["actual"]["mode"] == "EDGE"
    assert result["actual"]["level"] == 3.5
    assert sim.commands.index(":TRIG:MODE EDGE") < sim.commands.index(":TRIG:EDGE:SOUR CHAN2") < sim.commands.index(":TRIG:EDGE:LEV 3.5")


def test_readback_quantization_is_visible(rig):
    sim, _, advanced = rig
    write = sim.write
    def quantized(command):
        write(":CHAN1:SCAL 2" if command == ":CHAN1:SCAL 1.9" else command)
    sim.write = quantized
    result = advanced.control("channel.set", {"channel": 1, "scale_per_division": 1.9})
    assert result["ok"]
    assert result["readback_differences"] == [{"field": "scale_per_division", "requested": 1.9, "actual": 2.0}]


def test_partial_failure_reports_changed_state_without_rollback(rig):
    sim, _, advanced = rig
    write = sim.write
    def fail(command):
        if command == ":CHAN1:COUP AC":
            raise TimeoutError("USB disconnected")
        write(command)
    sim.write = fail
    result = advanced.control("channel.set", {"channel": 1, "enabled": False, "coupling": "AC"})
    assert not result["ok"] and result["state_may_have_changed"]
    assert result["completed_fields"] == ["enabled"]
    assert result["attempted_field"] == "coupling"
    assert result["actual"]["enabled"] is False
    assert ":CHAN1:DISP 1" not in sim.commands


def test_preexisting_scpi_error_prevents_writes(rig):
    sim, _, advanced = rig
    query = sim.query
    sim.query = lambda c: '-100,"old error"' if c == ":SYST:ERR?" else query(c)
    with pytest.raises(RuntimeError, match="before changes"):
        advanced.control("trigger.edge.set", {"level": 1})
    assert all(c.endswith("?") for c in sim.commands)


def test_catalog_small_and_detailed_only_on_demand():
    listing = discover("trigger")
    assert len(listing["operations"]) <= 10
    assert all("parameters" not in item for item in listing["operations"])
    assert discover(operation="channel.set")["parameters"]["additionalProperties"] is False
    assert discover(operation="math.configure")["executable"] is False
    with pytest.raises(ValueError):
        discover(operation="unknown")


def test_unconfirmed_edge_mode_is_not_success(rig):
    sim, _, advanced = rig
    sim.settings[":TRIG:MODE"] = "PULS"
    write = sim.write
    sim.write = lambda c: None if c == ":TRIG:MODE EDGE" else write(c)
    result = advanced.control("trigger.edge.set", {"slope": "NEG"})
    assert not result["ok"] and "did not confirm" in result["problem"]


def test_bitmap_conversion_preserves_pixels(rig):
    import io
    from PIL import Image
    sim, _, advanced = rig
    raw = sim.binary(":DISP:DATA? ON,OFF,BMP24", 2 * 1024 * 1024)
    png = advanced.artifacts.normalize_bitmap(raw)
    with Image.open(io.BytesIO(raw)) as before, Image.open(io.BytesIO(png)) as after:
        assert before.tobytes() == after.tobytes()
    with pytest.raises(OSError):
        advanced.artifacts.normalize_bitmap(raw[:100])


def test_screenshot_receipt_integrity_and_persistence(rig):
    sim, scope, advanced = rig
    receipt = advanced.read("screenshot.capture", {})
    assert receipt["simulated"] and "data" not in receipt
    assert sim.trigger == "STOP"
    assert not any(c in sim.commands for c in (":STOP", ":RUN"))
    other = Advanced(scope, Artifacts(advanced.artifacts.root))
    metadata, data = other.read("artifact.image", {"artifact_id": receipt["artifact_id"]})
    assert metadata["artifact_id"] == receipt["artifact_id"] and data.startswith(b"\x89PNG")
    assert metadata["next_action"] != receipt["next_action"]  # Never instruct a fetch loop.
    path = advanced.artifacts.root / (receipt["artifact_id"] + ".png")
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        other.read("artifact.image", {"artifact_id": receipt["artifact_id"]})


@pytest.mark.parametrize("artifact_id", ["../../secret", "/etc/passwd", "a" * 31, "g" * 32])
def test_artifact_ids_cannot_access_arbitrary_files(rig, artifact_id):
    with pytest.raises(ValueError, match="Invalid artifact"):
        rig[2].read("artifact.image", {"artifact_id": artifact_id})


def test_invalid_screenshot_never_saved(rig):
    sim, _, advanced = rig
    sim.binary = lambda *args: b"not an image"
    with pytest.raises(ValueError, match="BMP"):
        advanced.read("screenshot.capture", {})
    assert not advanced.artifacts.root.exists()


def test_artifact_expiry_and_count_limit(rig):
    _, _, advanced = rig
    first = advanced.read("screenshot.capture", {})
    path = advanced.artifacts.root / (first["artifact_id"] + ".json")
    os.utime(path, (time.time() - 8 * 86400,) * 2)
    with pytest.raises(ValueError, match="expired"):
        advanced.read("artifact.image", {"artifact_id": first["artifact_id"]})
    assert not path.with_suffix(".png").exists()
    for _ in range(65):
        advanced.read("screenshot.capture", {})
    assert len(list(advanced.artifacts.root.glob("*.png"))) == 64
    assert len(list(advanced.artifacts.root.glob("*.json"))) == 64
