import io
import math
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from rigol_mcp.scope import Scope, decode_waveform
from rigol_mcp.simulator import Simulator
from rigol_mcp.transport import read_block


@pytest.fixture
def rig():
    sim = Simulator()
    return sim, Scope(sim.connect, allow_control=True, simulated=True)


def test_conversion_with_nonzero_origins():
    result = decode_waveform("0,0,3,1,0.25,-1,2,0.02,10,127", bytes([127, 137, 147]))
    assert result["time_seconds"] == [-1.5, -1.25, -1.0]
    assert result["amplitude"] == pytest.approx([-0.2, 0, 0.2])


@pytest.mark.parametrize("preamble", [
    "0,0,2,1,.1,0,0,.1,0,127", "1,0,3,1,.1,0,0,.1,0,127",
    "0,2,3,1,.1,0,0,.1,0,127", "0,0,3,1,nan,0,0,.1,0,127",
    "0,0,3,1,0,0,0,.1,0,127", "0,0,3,1,.1,0,0,-.1,0,127", "0,0,3",
])
def test_reject_invalid_preambles(preamble):
    with pytest.raises(ValueError):
        decode_waveform(preamble, b"abc")


def test_controls_require_opt_in():
    sim = Simulator()
    scope = Scope(sim.connect)
    with pytest.raises(PermissionError):
        scope.acquisition("run")
    with pytest.raises(PermissionError):
        scope.set_timebase(.001)
    assert not sim.commands


def test_wrong_model_only_allows_identification():
    sim = Simulator()
    query = sim.query
    sim.query = lambda c: "RIGOL,DS1202CA,SERIAL,1.0" if c == "*IDN?" else query(c)
    scope = Scope(sim.connect, allow_control=True)
    assert scope.identify()["model"] == "DS1202CA"
    with pytest.raises(ValueError, match="Unsupported instrument"):
        scope.acquisition("run")
    assert ":RUN" not in sim.commands


@pytest.mark.parametrize("raw", ["9.9e37", "-9.9e37", "nan", "inf", "measure error!"])
def test_invalid_measurements_are_not_numbers(rig, raw):
    sim, scope = rig
    query = sim.query
    sim.query = lambda c: raw if c.startswith(":MEAS") else query(c)
    result = scope.measure(1, "frequency")
    assert result["value"] is None and not result["valid"]


@pytest.mark.parametrize("channel", [0, 3, True, "1;:RUN"])
def test_invalid_channels_never_touch_hardware(rig, channel):
    sim, scope = rig
    with pytest.raises(ValueError):
        scope.measure(channel, "frequency")
    assert not sim.commands


def test_running_capture_rejected_without_writes(rig):
    sim, scope = rig
    sim.trigger = "RUN"
    with pytest.raises(ValueError, match="Stop acquisition"):
        scope.waveform(1)
    assert all(c.endswith("?") for c in sim.commands)


def test_waveform_and_measurement_agree(rig):
    _, scope = rig
    wave = scope.waveform(1)
    assert wave["point_count"] == 1200
    assert max(wave["amplitude"]) - min(wave["amplitude"]) == pytest.approx(
        scope.measure(1, "peak_to_peak")["value"])
    assert wave["simulated"]
    assert wave["amplitude_unit"] == "V"


def test_disabled_channel_rejected(rig):
    sim, scope = rig
    query = sim.query
    sim.query = lambda c: "0" if c == ":CHAN2:DISP?" else query(c)
    with pytest.raises(ValueError, match="Enable channel 2"):
        scope.waveform(2)


def test_preexisting_error_prevents_control(rig):
    sim, scope = rig
    query = sim.query
    sim.query = lambda c: '-100,"Command error"' if c == ":SYST:ERR?" else query(c)
    with pytest.raises(RuntimeError, match="before command"):
        scope.acquisition("run")
    assert ":RUN" not in sim.commands


def test_single_only_arms_and_timebase_reads_back(rig):
    _, scope = rig
    assert scope.acquisition("single")["trigger_status"] == "WAIT"
    assert scope.set_timebase(.002)["actual_seconds_per_division"] == .002


@pytest.mark.parametrize("value", [0, -1, 51, math.nan, math.inf])
def test_bad_timebase_never_touches_hardware(rig, value):
    sim, scope = rig
    with pytest.raises(ValueError):
        scope.set_timebase(value)
    assert not sim.commands


class BinaryResource:
    def __init__(self, data):
        self.stream = io.BytesIO(data)
        self.read_termination = "\n"

    def read_bytes(self, count, **kwargs):
        return self.stream.read(count)


def test_block_contains_newline_and_hash():
    resource = BinaryResource(b"#13\n#\x00\n")
    assert read_block(resource, 1200) == b"\n#\x00"
    assert resource.read_termination == "\n"


@pytest.mark.parametrize("wire", [b"#0", b"xx", b"#1x", b"#199\n", b"#14abc", b"#13abcX", b"#41201"])
def test_malformed_or_oversized_blocks(wire):
    resource = BinaryResource(wire)
    with pytest.raises(ValueError):
        read_block(resource, 1200)
    assert resource.read_termination == "\n"


def test_sessions_are_serialized(rig):
    sim, _ = rig
    active = 0
    @contextmanager
    def connect():
        nonlocal active
        active += 1
        assert active == 1
        try:
            yield sim
        finally:
            active -= 1
    scope = Scope(connect)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: scope.waveform(1), range(12)))
    assert len(results) == 12 and active == 0


def test_inspect_signal_needs_no_preflight_and_has_compact_readings(rig):
    sim, scope = rig
    result = scope.inspect_signal()
    assert result["ok"] and result["simulated"]
    assert len(result["channels"]) == 2
    assert result["channels"][0]["measurements"]["frequency"] == {"value": 1000, "unit": "Hz"}
    assert "stopped" in result["acquisition"]
    assert sim.commands.count("*IDN?") == 1
    assert all("?" in c for c in sim.commands)


def test_inspect_signal_explains_unavailable_measurements(rig):
    sim, scope = rig
    original = sim.query
    sim.query = lambda c: "9.9e37" if c.startswith(":MEAS") else original(c)
    result = scope.inspect_signal(1)
    assert result["channels"][0]["measurements"]["frequency"]["value"] is None
    assert result["next_actions"]
    assert "does not by itself prove" in result["channels"][0]["notes"][0]


def test_burst_frequency_error_preserves_other_signal_readings(rig):
    sim, scope = rig
    original = sim.query
    sim.query = lambda c: "measure error!" if c == ":MEAS:ITEM? FREQ,CHAN1" else original(c)
    result = scope.inspect_signal(1)
    measurements = result["channels"][0]["measurements"]
    assert result["ok"]
    assert measurements["frequency"]["value"] is None
    assert measurements["peak_to_peak"]["value"] > 0
    assert "Unavailable: frequency" in result["channels"][0]["notes"][0]


def test_unknown_measurement_text_is_still_an_error(rig):
    sim, scope = rig
    original = sim.query
    sim.query = lambda c: "unexpected reply" if c.startswith(":MEAS") else original(c)
    with pytest.raises(ValueError):
        scope.measure(1, "frequency")


def test_inspect_no_channels_is_actionable(rig):
    sim, scope = rig
    original = sim.query
    sim.query = lambda c: "0" if c.endswith(":DISP?") else original(c)
    result = scope.inspect_signal()
    assert result["channels"] == []
    assert result["next_actions"]


def test_instrument_lock_contends_and_releases(tmp_path, monkeypatch):
    from rigol_mcp.transport import instrument_lock
    monkeypatch.setattr("rigol_mcp.transport.Path.home", lambda: tmp_path)
    with instrument_lock("test-resource", 0.1):
        with pytest.raises(TimeoutError, match="Scope busy"):
            with instrument_lock("test-resource", 0.01):
                pytest.fail("Concurrent lock granted")
    with instrument_lock("test-resource", 0.01):
        pass


@pytest.mark.parametrize("action,states", [("stop", ["TD", "RUN", "STOP"]),
                                           ("run", ["STOP", "STOP", "WAIT"])])
def test_acquisition_waits_for_real_firmware_state(rig, monkeypatch, action, states):
    sim, scope = rig
    original = sim.query
    replies = iter(states)
    sim.query = lambda c: next(replies) if c == ":TRIG:STAT?" else original(c)
    monkeypatch.setattr("rigol_mcp.scope.time.sleep", lambda _: None)
    assert scope.acquisition(action)["trigger_status"] == states[-1]
    assert list(replies) == []


def test_acquisition_state_confirmation_is_bounded(rig, monkeypatch):
    sim, scope = rig
    original = sim.query
    sim.query = lambda c: "RUN" if c == ":TRIG:STAT?" else original(c)
    clock = iter([0, 4])
    monkeypatch.setattr("rigol_mcp.scope.time.monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="did not confirm"):
        scope.acquisition("stop")

