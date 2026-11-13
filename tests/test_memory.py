import hashlib
import json
import os
import time

import pytest

from rigol_mcp.advanced import Advanced
from rigol_mcp.artifacts import Artifacts
from rigol_mcp.memory import MAX_POINTS, parse_preamble, sample_amplitude, sample_time
from rigol_mcp.scope import Scope
from rigol_mcp.simulator import Simulator


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sim = Simulator()
    scope = Scope(sim.connect, allow_control=True, simulated=True)
    return sim, scope, Advanced(scope, Artifacts(tmp_path / "screenshots"))


def capture(advanced, **args):
    return advanced.control("waveform.full_memory", {"channel": 1, **args})


def test_chunk_boundaries_hash_calibration_and_restoration(rig):
    sim, _, advanced = rig
    original = dict(sim.settings)
    result = capture(advanced)
    assert result["ok"] and result["point_count"] == 600001
    assert sim.settings == original and sim.trigger == "STOP"
    assert [c for c in sim.commands if c.startswith(":WAV:STOP ")][1:4] == [
        ":WAV:STOP 250000", ":WAV:STOP 500000", ":WAV:STOP 600001"]
    expected = bytes(i % 256 for i in range(600001))
    assert result["sha256"] == hashlib.sha256(expected).hexdigest()
    assert "amplitude" not in result and "time_seconds" not in result
    assert result["summary"]["minimum"] == pytest.approx(-5.08)
    assert result["summary"]["maximum"] == pytest.approx(5.12)
    for start in (249998, 499998, 599999):
        page = advanced.read("waveform.samples", {"artifact_id": result["artifact_id"], "start": start, "count": 5})
        assert page["amplitude"] == pytest.approx([(i % 256 - 127) * .04 for i in range(start, min(start + 5, 600001))])
        assert page["time_seconds"][0] == pytest.approx(start * 1e-6 - .003)
    assert page["next_start"] is None and page["count"] == 2
    # New object, same filesystem, no instrument access for saved data.
    sim.commands.clear()
    other = Advanced(rig[1], Artifacts(advanced.artifacts.root))
    info = other.read("waveform.info", {"artifact_id": result["artifact_id"]})
    assert all(info[k] == v for k, v in result.items())
    assert "scaling" in info and "scaling" not in result
    assert not sim.commands


@pytest.mark.parametrize("points", [1, 249999, 250000, 250001, 500000])
def test_exact_and_partial_chunk_sizes(rig, points):
    sim, _, advanced = rig
    sim.memory_points = points
    result = capture(advanced)
    assert result["ok"] and result["point_count"] == points
    assert sim.commands.count(":WAV:DATA?") == (points + 249999) // 250000


@pytest.mark.parametrize("state,sweep,policy", [
    ("RUN", "AUTO", "require_stopped"), ("WAIT", "NORM", "stop_and_resume"),
    ("TD", "SING", "stop_and_resume"), ("UNKNOWN", "AUTO", "stop_and_resume")])
def test_running_and_armed_preconditions_never_write(rig, state, sweep, policy):
    sim, _, advanced = rig
    sim.trigger, sim.settings[":TRIG:SWE"] = state, sweep
    with pytest.raises(ValueError):
        capture(advanced, acquisition_policy=policy)
    assert all(c.endswith("?") for c in sim.commands)


def test_explicit_pause_resumes_and_stopped_single_is_preserved(rig):
    sim, _, advanced = rig
    sim.trigger = "TD"
    result = capture(advanced, acquisition_policy="stop_and_resume")
    assert result["ok"] and sim.trigger == "RUN"
    assert sim.commands.count(":STOP") == sim.commands.count(":RUN") == 1
    sim.trigger, sim.settings[":TRIG:SWE"] = "STOP", "SING"
    sim.commands.clear()
    result = capture(advanced, acquisition_policy="stop_and_resume")
    assert result["ok"] and sim.trigger == "STOP" and ":RUN" not in sim.commands


@pytest.mark.parametrize("args", [{"channel": True}, {"timeout_seconds": float("nan")},
    {"timeout_seconds": 301}, {"acquisition_policy": "run"}, {"channel": "1;:RUN"}])
def test_invalid_input_before_io(rig, args):
    sim, _, advanced = rig
    with pytest.raises(ValueError):
        advanced.control("waveform.full_memory", {"channel": 1, **args})
    assert not sim.commands


def test_read_gateway_and_disabled_controls_reject_capture(rig):
    sim, scope, advanced = rig
    with pytest.raises(ValueError):
        advanced.read("waveform.full_memory", {"channel": 1})
    scope.allow_control = False
    with pytest.raises(PermissionError):
        capture(advanced)
    assert not sim.commands


@pytest.mark.parametrize("preamble", [
    "0,0,1200,1,1e-9,0,0,.1,0,127", "0,2,24000001,1,1e-9,0,0,.1,0,127",
    "0,2,1.1,1,1e-9,0,0,.1,0,127", "0,2,0,1,1e-9,0,0,.1,0,127",
    "0,2,3,1,0,0,0,.1,0,127", "0,2,3,1,1e-9,0,0,-.1,0,127",
    "0,2,3,1,nan,0,0,.1,0,127", "0,2,3", "0,2,3,0,1e-9,0,0,.1,0,127"])
def test_preamble_bounds(preamble):
    with pytest.raises(ValueError):
        parse_preamble(preamble)


def test_nonzero_calibration_origins():
    scaling = parse_preamble("0,2,3,1,.25,-1,2,.02,10,127")
    assert sample_time(0, scaling) == -1.5
    assert sample_amplitude(137, scaling) == 0


@pytest.mark.parametrize("failure", ["short", "timeout"])
def test_failed_chunk_discards_partial_data_and_restores(rig, failure):
    sim, _, advanced = rig
    original = dict(sim.settings)
    sim.trigger = "RUN"
    binary = sim.binary
    def broken(command, maximum):
        if sim.settings[":WAV:STAR"] == "250001":
            if failure == "timeout":
                raise TimeoutError("USB timed out")
            return b"short"
        return binary(command, maximum)
    sim.binary = broken
    result = capture(advanced, acquisition_policy="stop_and_resume")
    assert not result["ok"] and result["transferred_points"] == 250000
    assert "artifact_id" not in result and not advanced.waveforms.root.exists()
    assert sim.settings == original and sim.trigger == "RUN"
    assert not result["cleanup_errors"]


def test_external_restart_discards_and_does_not_override_operator(rig):
    sim, _, advanced = rig
    binary = sim.binary
    def restart(command, maximum):
        data = binary(command, maximum)
        sim.trigger = "RUN"
        return data
    sim.binary = restart
    result = capture(advanced)
    assert not result["ok"] and result["cleanup_errors"]
    assert sim.trigger == "RUN" and "artifact_id" not in result
    assert ":STOP" not in sim.commands


def test_changed_settings_discard_at_end(rig):
    sim, _, advanced = rig
    sim.memory_points = 100
    binary = sim.binary
    def changed(command, maximum):
        data = binary(command, maximum)
        sim.settings[":CHAN1:SCAL"] = "2"
        return data
    sim.binary = changed
    result = capture(advanced)
    assert not result["ok"] and "artifact_id" not in result
    assert "changed" in result["problem"]


def test_restoration_failure_keeps_complete_capture_but_is_error(rig):
    sim, _, advanced = rig
    sim.trigger = "RUN"
    write = sim.write
    def fail(command):
        if command == ":RUN":
            raise TimeoutError("Resume failed")
        write(command)
    sim.write = fail
    result = capture(advanced, acquisition_policy="stop_and_resume")
    assert not result["ok"] and result["capture_complete"]
    assert result["cleanup_errors"] and result["acquisition_after"] == "STOP"
    assert advanced.read("waveform.samples", {"artifact_id": result["artifact_id"], "count": 1})["ok"]


def test_transfer_deadline_discards_without_retry(rig, monkeypatch):
    sim, _, advanced = rig
    ticks = iter([0, 6, 7])
    monkeypatch.setattr("rigol_mcp.memory.time.monotonic", lambda: next(ticks))
    result = capture(advanced, timeout_seconds=5)
    assert not result["ok"] and result["transferred_points"] == 0
    assert ":WAV:DATA?" not in sim.commands


def test_artifact_validation_retention_and_corruption(rig):
    sim, _, advanced = rig
    sim.memory_points = 10
    result = capture(advanced)
    for args in ({"artifact_id": "../../secret"}, {"artifact_id": result["artifact_id"], "start": 10},
                 {"artifact_id": result["artifact_id"], "count": 2001}, {"artifact_id": result["artifact_id"], "start": True}):
        with pytest.raises(ValueError):
            advanced.read("waveform.samples", args)
    path = advanced.waveforms.root / (result["artifact_id"] + ".bin")
    path.write_bytes(b"x" * 10)
    with pytest.raises(ValueError, match="integrity"):
        advanced.read("waveform.info", {"artifact_id": result["artifact_id"]})
    meta = path.with_suffix(".json")
    os.utime(meta, (time.time() - 8 * 86400,) * 2)
    with pytest.raises(ValueError, match="expired"):
        advanced.read("waveform.info", {"artifact_id": result["artifact_id"]})
    assert not path.exists()
    for _ in range(9):
        capture(advanced)
    assert len(list(advanced.waveforms.root.glob("*.bin"))) == 8


def test_unsupported_acquisition_and_bad_transfer_replies_do_not_write(rig):
    sim, _, advanced = rig
    sim.settings[":ACQ:TYPE"] = "PEAK"
    with pytest.raises(ValueError, match="NORMAL"):
        capture(advanced)
    assert all(c.endswith("?") for c in sim.commands)
    sim.settings[":ACQ:TYPE"] = "NORM"
    sim.settings[":WAV:SOUR"] = "CHAN1;:RUN"
    with pytest.raises(ValueError, match="Unexpected"):
        capture(advanced)
    assert all(c.endswith("?") for c in sim.commands)


@pytest.mark.parametrize("second_enabled,maximum", [(False, 24000000), (True, 12000000)])
def test_memory_depth_maximum_and_auto(rig, second_enabled, maximum):
    sim, _, advanced = rig
    sim.trigger = "RUN"
    sim.settings[":CHAN2:DISP"] = str(int(second_enabled))
    info = advanced.read("acquisition.memory.get", {})
    assert info["available_depths"][-1] == maximum
    result = advanced.control("acquisition.memory.set", {"depth": "MAXIMUM"})
    assert result["ok"] and result["actual"]["selected_depth"] == maximum
    result = advanced.control("acquisition.memory.set", {"depth": "AUTO"})
    assert result["ok"] and result["actual"]["selected_depth"] == "AUTO"


@pytest.mark.parametrize("depth", ["24000000", "120000", "12M;:RUN", 12000000])
def test_memory_depth_rejects_incompatible_or_invalid_values(rig, depth):
    sim, _, advanced = rig
    sim.trigger = "RUN"
    with pytest.raises(ValueError):
        advanced.control("acquisition.memory.set", {"depth": depth})
    assert all(c.endswith("?") for c in sim.commands)


def test_memory_depth_rejects_waiting_and_reports_readback_mismatch(rig):
    sim, _, advanced = rig
    sim.trigger = "WAIT"
    with pytest.raises(ValueError, match="running"):
        advanced.control("acquisition.memory.set", {"depth": "MAXIMUM"})
    assert all(c.endswith("?") for c in sim.commands)
    sim.trigger = "RUN"
    write = sim.write
    sim.write = lambda c: None if c.startswith(":ACQ:MDEP ") else write(c)
    result = advanced.control("acquisition.memory.set", {"depth": "MAXIMUM"})
    assert not result["ok"] and result["actual"]["selected_depth"] == "AUTO"


def test_stopped_memory_depth_write_is_rejected_before_io_mutation(rig):
    sim, _, advanced = rig
    with pytest.raises(ValueError, match="STOP/WAIT"):
        advanced.control("acquisition.memory.set", {"depth": "AUTO"})
    assert sim.commands == ["*IDN?", ":TRIG:STAT?"]


def test_inconsistent_sample_rate_rejects_capture(rig):
    sim, _, advanced = rig
    sim.settings[":ACQ:SRAT"] = "2000000"
    result = capture(advanced)
    assert not result["ok"] and "sample rate" in result["problem"]
    assert "artifact_id" not in result and ":WAV:DATA?" not in sim.commands
