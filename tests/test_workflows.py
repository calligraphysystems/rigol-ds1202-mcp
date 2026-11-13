import json
import time

import pytest

from rigol_mcp.advanced import Advanced
from rigol_mcp.artifacts import Artifacts
from rigol_mcp.scope import Scope
from rigol_mcp.simulator import Simulator


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sim = Simulator()
    sim.memory_points = 100
    scope = Scope(sim.connect, allow_control=True, simulated=True)
    return sim, scope, Advanced(scope, Artifacts(tmp_path / "screenshots"))


def test_setup_restore_and_backup_survive_new_instance(rig):
    sim, scope, advanced = rig
    sim.trigger = "RUN"
    saved = advanced.read("setup.snapshot", {})
    sim.timebase = .002
    sim.settings[":CHAN1:COUP"] = "AC"
    other = Advanced(scope, advanced.artifacts)
    result = other.control("setup.restore", {"artifact_id": saved["artifact_id"], "acquisition_after": "saved"})
    assert result["ok"] and not result["readback_differences"] and sim.trigger == "RUN"
    assert sim.timebase == .001 and sim.settings[":CHAN1:COUP"] == "DC"
    result = other.control("setup.restore", {"artifact_id": result["rollback_artifact_id"]})
    assert result["ok"] and sim.trigger == "STOP" and sim.timebase == .002
    assert sim.settings[":CHAN1:COUP"] == "AC"


@pytest.mark.parametrize("damage", ["identity", "payload", "path"])
def test_bad_setup_never_writes(rig, damage):
    sim, scope, advanced = rig
    saved = advanced.read("setup.snapshot", {})
    identifier = saved["artifact_id"]
    path = advanced.setups.root / (identifier + ".json")
    record = json.loads(path.read_text())
    if damage == "identity":
        record["instrument"]["serial"] = "OTHER"
    elif damage == "payload":
        record["sha256"] = "0" * 64
    else:
        identifier = "../secret"
    path.write_text(json.dumps(record))
    sim.commands.clear()
    with pytest.raises(ValueError):
        advanced.control("setup.restore", {"artifact_id": identifier})
    assert all(c.endswith("?") for c in sim.commands)


def test_restore_readback_failure_keeps_backup_and_does_not_resume(rig):
    sim, _, advanced = rig
    sim.trigger = "RUN"
    saved = advanced.read("setup.snapshot", {})
    sim.timebase = .005
    original_write = sim.write
    sim.write = lambda command: None if command.startswith(":TIM:MAIN:SCAL ") else original_write(command)
    result = advanced.control("setup.restore", {"artifact_id": saved["artifact_id"], "acquisition_after": "saved"})
    assert not result["ok"] and result["rollback_artifact_id"]
    assert result["readback_differences"] and sim.trigger == "STOP"


@pytest.mark.parametrize("state,sweep,recorder", [("WAIT", "NORM", "0"), ("RUN", "SING", "0"), ("STOP", "AUTO", "1")])
def test_armed_or_recording_refused_before_writes(rig, state, sweep, recorder):
    sim, _, advanced = rig
    saved = advanced.read("setup.snapshot", {})
    sim.trigger = state
    sim.settings[":TRIG:SWE"] = sweep
    sim.settings[":FUNC:WREC:ENAB"] = recorder
    sim.commands.clear()
    for op, args in [("setup.restore", {"artifact_id": saved["artifact_id"]}), ("acquisition.capture_single", {"channel": 1})]:
        with pytest.raises(ValueError):
            advanced.control(op, args)
    assert all(c.endswith("?") for c in sim.commands)


def test_success_wait_and_full_memory_share_session(rig):
    sim, _, advanced = rig
    sim.single_polls_remaining = 1
    result = advanced.control("acquisition.capture_single", {"channel": 1, "timeout_seconds": 1})
    assert result["ok"] and result["trigger_completed"] and not result["timed_out"]
    assert result["point_count"] == 100 and sim.trigger == "STOP"
    assert sim.commands.count("*IDN?") == 1
    assert advanced.read("setup.info", {"artifact_id": result["setup_artifact_id"]})["ok"]
    assert advanced.read("waveform.info", {"artifact_id": result["artifact_id"]})["ok"]


def test_timeout_stops_and_publishes_no_waveform(rig):
    sim, _, advanced = rig
    result = advanced.control("acquisition.capture_single", {"channel": 1, "timeout_seconds": .2})
    assert not result["ok"] and result["timed_out"] and not result["trigger_completed"]
    assert sim.trigger == "STOP" and not advanced.waveforms.root.exists()
    assert ":WAV:DATA?" not in sim.commands and ":TFOR" not in sim.commands


def test_external_settings_change_refuses_success(rig):
    sim, _, advanced = rig
    sim.single_polls_remaining = 0
    write = sim.write
    def changed(command):
        write(command)
        if command == ":SING":
            sim.timebase = .01
    sim.write = changed
    result = advanced.control("acquisition.capture_single", {"channel": 1})
    assert not result["ok"] and "Settings changed" in result["problem"]
    assert not advanced.waveforms.root.exists()


def test_disabled_controls_and_bad_input_before_mutation(rig):
    sim, scope, advanced = rig
    saved = advanced.read("setup.snapshot", {})
    scope.allow_control = False
    sim.commands.clear()
    for op, args in [("setup.restore", {"artifact_id": saved["artifact_id"]}), ("acquisition.capture_single", {"channel": 1})]:
        with pytest.raises(PermissionError):
            advanced.control(op, args)
    assert not sim.commands
    with pytest.raises(ValueError):
        advanced.control("acquisition.capture_single", {"channel": 1, "timeout_seconds": 61})


def test_restore_centers_delayed_window_before_short_timebase(rig):
    sim, _, advanced = rig
    saved = advanced.read('setup.snapshot', {})
    sim.timebase=.01
    sim.settings[':TIM:MAIN:OFFS']='0.048'
    sim.settings[':CHAN1:TCAL']='0.00000001'
    original=sim.write
    def write(command):
        if command==':TIM:MAIN:SCAL 0.000001' and float(sim.settings[':TIM:MAIN:OFFS'])!=0:
            raise RuntimeError('Firmware cannot shrink delayed window')
        original(command)
    sim.write=write
    r=advanced.control('setup.restore',{'artifact_id':saved['artifact_id']})
    assert r['ok'] and not r['readback_differences']
