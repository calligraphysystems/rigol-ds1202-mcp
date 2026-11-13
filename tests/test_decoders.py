import pytest
from rigol_mcp.advanced import Advanced
from rigol_mcp.artifacts import Artifacts
from rigol_mcp.scope import Scope
from rigol_mcp.simulator import Simulator
from rigol_mcp.decoders import parse_table
from rigol_mcp.workflows import differences

@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("rigol_mcp.decoders.time.sleep", lambda _: None)
    sim = Simulator()
    return sim, Advanced(Scope(sim.connect, allow_control=True, simulated=True), Artifacts(tmp_path / "screenshots"))

@pytest.mark.parametrize("protocol,args", [
    ("uart", {"tx": "CHAN1", "rx": "OFF", "baud": 10000}),
    ("uart", {"tx": "OFF", "rx": "CHAN2", "baud": 115200, "parity": "ODD", "stop_bits": 1.5}),
    ("i2c", {"clock": "CHAN2", "data": "CHAN1", "address_format": "RW"}),
    ("spi", {"clock": "CHAN1", "data": "CHAN2", "sample_edge": "FALL", "frame_timeout_seconds": .001}),
])
def test_protocol_config_and_full_decoder_rollback(rig, protocol, args):
    sim, advanced = rig
    original = dict(sim.settings)
    result = advanced.control(f"decode.{protocol}.configure", {"decoder": 1, **args})
    assert result["ok"] and not result["readback_differences"]
    assert sim.trigger == "STOP"
    restored = advanced.control("decode.restore", {"artifact_id": result["rollback_artifact_id"]})
    assert restored["ok"] and not differences(original, sim.settings)
    assert not any(c in sim.commands for c in (":STOP", ":RUN", ":SING"))

@pytest.mark.parametrize("args", [
    {"tx": "OFF", "rx": "OFF", "baud": 9600},
    {"tx": "CHAN1", "rx": "CHAN1", "baud": 9600},
    {"tx": "CHAN1", "rx": "OFF", "baud": 0},
    {"tx": "CHAN1", "rx": "OFF", "baud": 9600, "threshold_ch1": 8},
    {"tx": "CHAN1", "rx": "OFF", "baud": 9600, "threshold_ch1": 0, "auto_threshold": True}])
def test_invalid_input_no_writes(rig, args):
    sim, advanced = rig
    with pytest.raises(ValueError):
        advanced.control("decode.uart.configure", {"decoder": 1, **args})
    assert all(c.endswith("?") for c in sim.commands)


def test_manual_threshold_and_page_results(rig):
    sim, advanced = rig
    result = advanced.control("decode.uart.configure", {"decoder": 1, "tx": "CHAN1", "rx": "OFF", "baud": 10000, "threshold_ch1": 1.5})
    assert result["ok"] and sim.settings[":DEC1:THRE:AUTO"] == "0"
    result = advanced.read("decode.events", {"decoder": 1, "start": 1, "count": 1})
    assert result["events"] == [{"time_text": "0.001000", "tx": 240}] and result["next_start"] is None
    sim.trigger = "RUN"
    with pytest.raises(ValueError):
        advanced.read("decode.events", {"decoder": 1})


def test_empty_table_not_traffic_claim_and_parser_preserves_cells(rig):
    sim, advanced = rig
    advanced.control("decode.uart.configure", {"decoder": 1, "tx": "CHAN1", "rx": "OFF", "baud": 10000})
    sim.settings[":ETAB1:ROW"] = "0"
    sim.commands.clear()
    result = advanced.read("decode.events", {"decoder": 1})
    assert result["events"] == [] and ":ETAB1:DATA?" not in sim.commands
    assert parse_table(b'Time,TX\r\n"0,1","a,b"\r\n') == [["Time", "TX"], ["0,1", "a,b"]]


def test_armed_and_disabled_channel_rejected(rig):
    sim, advanced = rig
    args = {"decoder": 1, "tx": "CHAN1", "rx": "OFF", "baud": 10000}
    sim.trigger = "WAIT"
    with pytest.raises(ValueError):
        advanced.control("decode.uart.configure", args)
    sim.trigger = "STOP"
    sim.settings[":CHAN1:DISP"] = "0"
    with pytest.raises(ValueError):
        advanced.control("decode.uart.configure", args)
    assert all(c.endswith("?") for c in sim.commands)


def test_observed_firmware_uart_table_is_normalized():
    from rigol_mcp.decoders import normalized_events
    rows = parse_table(b"Decoder1,RS232,\nTime,Tx,Info,\n-400.0us,F0,,\n600.0us,0xF0,Framing error,\n")
    events, columns = normalized_events(rows, "HEX")
    assert columns == ["Time", "Tx", "Info"]
    assert events[0] == {"time_seconds": pytest.approx(-.0004), "tx": 240}
    assert events[1]["time_seconds"] == pytest.approx(.0006)
    assert events[1]["info"] == "Framing error"
    assert normalized_events([["unknown", "table"]], "HEX")[0] == [{"firmware_cells": ["unknown", "table"]}]


def test_only_measured_one_step_rounding_is_accepted():
    from rigol_mcp.decoders import classify_differences
    items = [{"setting": "SPI:TIM", "expected": "1e-6", "actual": "9.99e-7"},
             {"setting": "UART:BAUD", "expected": "115200", "actual": "115201"},
             {"setting": "SPI:TIM", "expected": "1e-6", "actual": "9e-7"}]
    hard, rounded = classify_differences(items)
    assert hard == items[2:] and rounded == items[:2]


def test_large_event_pages_are_bounded(rig):
    sim, advanced = rig
    advanced.control("decode.uart.configure", {"decoder": 1, "tx": "CHAN1", "rx": "OFF", "baud": 10000})
    sim.event_rows = ("Time,Tx,Info\n" + "0.001s,F0," + "x" * 1000 + "\n").encode()
    sim.event_rows += ("0.002s,F0," + "x" * 1000 + "\n").encode() * 99
    result = advanced.read("decode.events", {"decoder": 1, "count": 100})
    assert 1 <= len(result["events"]) < 20 and result["next_start"] == len(result["events"])


def test_serial_search_returns_implemented_protocols():
    from rigol_mcp.operations import discover
    results = discover("serial")
    assert {"decode.uart.configure", "decode.i2c.configure", "decode.spi.configure"} <= {r["operation"] for r in results["operations"]}
