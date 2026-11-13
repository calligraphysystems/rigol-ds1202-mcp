"""Spikes must survive compression, while payload size stays independent of depth."""
import io
import json

from PIL import Image
import pytest

from rigol_mcp.advanced import Advanced
from rigol_mcp.artifacts import Artifacts
from rigol_mcp.memory import MAX_POINTS, parse_preamble, sample_amplitude, sample_time
from rigol_mcp.operations import discover
from rigol_mcp.overview import overview
from rigol_mcp.scope import Scope
from rigol_mcp.simulator import Simulator


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sim = Simulator()
    advanced = Advanced(Scope(sim.connect, simulated=True), Artifacts(tmp_path / "screenshots"))
    def save(data):
        preamble = f"0,2,{len(data)},1,.000001,-.1,13,.02,10,127"
        scaling = parse_preamble(preamble)
        receipt = advanced.waveforms.save(bytes(data), {
            "ok": True, "simulated": True, "cleanup_errors": [], "scaling": scaling,
            "preamble": preamble, "amplitude_unit": "A"})
        return receipt, scaling
    return sim, advanced, save


def test_spikes_at_bin_edges_and_endpoints_survive_with_exact_locations(saved):
    sim, advanced, save = saved
    data = bytearray([137] * 10003)
    # Non-divisible window, spikes on both sides of bin boundaries, and endpoints.
    for i, code in ((0, 0), (2499, 254), (2500, 1), (5000, 253),
                    (5001, 2), (7501, 252), (7502, 3), (10002, 255)):
        data[i] = code
    receipt, scaling = save(data)
    result = advanced.read("waveform.overview", {"artifact_id": receipt["artifact_id"], "bins": 4})
    assert receipt["next_action"]["arguments"]["operation"] == "waveform.overview"
    assert result["simulated"] and result["amplitude_unit"] == "A"
    assert result["source_sha256"] == receipt["sha256"]
    covered = []
    for a, b, mini, minv, maxi, maxv in result["envelope"]:
        block = data[a:b]
        covered.extend(range(a, b))
        assert mini == a + block.index(min(block))
        assert maxi == a + block.index(max(block))
        assert minv == sample_amplitude(data[mini], scaling)
        assert maxv == sample_amplitude(data[maxi], scaling)
    assert covered == list(range(len(data)))
    for name, idx in (("minimum", 0), ("maximum", 10002)):
        assert result["extrema"][name]["sample_index"] == idx
        assert result["extrema"][name]["time_seconds"] == sample_time(idx, scaling)
        call = result["sample_actions"][name]["arguments"]
        page = advanced.read(**call)
        assert page["start"] <= idx < page["start"] + page["count"]
        assert page["amplitude"][idx - page["start"]] == result["extrema"][name]["amplitude"]
    assert not sim.commands


@pytest.mark.parametrize("start,count,bins", [(3, 7, 4), (18, 20, 256), (19, 1, 1), (0, 20, 256)])
def test_zoom_clamps_end_preserves_global_indices_and_first_ties(saved, start, count, bins):
    sim, advanced, save = saved
    receipt, scaling = save([150] * 20)
    result = advanced.read("waveform.overview", {"artifact_id": receipt["artifact_id"],
        "start": start, "count": count, "bins": bins})
    assert result["end_exclusive"] == min(start + count, 20)
    assert result["bin_count"] == min(bins, result["point_count"])
    assert result["envelope"][0][0] == start
    assert result["envelope"][-1][1] == min(start + count, 20)
    assert result["time_range_seconds"][0] == sample_time(start, scaling)
    assert all(row[0] == row[2] == row[4] for row in result["envelope"])
    assert not sim.commands


@pytest.mark.parametrize("args", [{"bins": 0}, {"bins": 257}, {"bins": True}, {"bins": 1.5},
    {"start": -1}, {"start": True}, {"start": 20}, {"count": 0}, {"count": True},
    {"count": MAX_POINTS + 1}, {"count": None}, {"bins": float("nan")}, {"bogus": 1}])
def test_invalid_parameters_are_rejected_without_instrument_access(saved, args):
    sim, advanced, save = saved
    receipt, _ = save([137] * 20)
    for operation in ("waveform.overview", "waveform.image"):
        with pytest.raises(ValueError):
            advanced.read(operation, {"artifact_id": receipt["artifact_id"], **args})
    assert not sim.commands


def test_direct_overview_guard_does_not_read_store(saved, monkeypatch):
    _, advanced, _ = saved
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid bounds must fail before reading payload")
    monkeypatch.setattr(advanced.waveforms, "read", unexpected)
    with pytest.raises(ValueError):
        overview(advanced.waveforms, "a" * 32, bins=257)


def test_24m_single_sample_spikes_and_bounded_response(saved):
    sim, advanced, save = saved
    data = bytearray([137]) * MAX_POINTS
    data[1234567], data[23456789] = 0, 255
    receipt, _ = save(data)
    for bins, budget in ((64, 14000), (256, 35000)):
        result = advanced.read("waveform.overview", {"artifact_id": receipt["artifact_id"], "bins": bins})
        assert len(result["envelope"]) == bins
        assert result["point_count"] == MAX_POINTS
        assert result["extrema"]["minimum"]["sample_index"] == 1234567
        assert result["extrema"]["maximum"]["sample_index"] == 23456789
        assert len(json.dumps(result, allow_nan=False).encode()) < budget
    assert not sim.commands


@pytest.mark.parametrize("data", [[137], [137] * 100, [0] + [137] * 100 + [255]])
def test_optional_image_is_bounded_labeled_and_uses_same_extrema(saved, data):
    sim, advanced, save = saved
    receipt, _ = save(data)
    args = {"artifact_id": receipt["artifact_id"]}
    numeric = advanced.read("waveform.overview", args)
    metadata, png = advanced.read("waveform.image", args)
    assert metadata["extrema"] == numeric["extrema"]
    assert metadata["simulated"] and metadata["amplitude_unit"] == "A"
    assert metadata["image_kind"] == "host_rendered_min_max_envelope"
    assert "envelope" not in metadata and "columns" not in metadata
    assert len(png) == metadata["bytes"] < 150000
    with Image.open(io.BytesIO(png)) as image:
        image.load()
        assert image.size == (1000, 600) and image.format == "PNG"
    assert not sim.commands and not advanced.artifacts.root.exists()


@pytest.mark.parametrize("operation", ["waveform.overview", "waveform.image"])
def test_payload_corruption_and_missing_artifact_rejected(saved, operation):
    sim, advanced, save = saved
    receipt, _ = save([137] * 20)
    path = advanced.waveforms.root / (receipt["artifact_id"] + ".bin")
    path.write_bytes(bytes([138] * 20))
    with pytest.raises(ValueError, match="integrity"):
        advanced.read(operation, {"artifact_id": receipt["artifact_id"]})
    path.unlink()
    with pytest.raises(ValueError, match="expired"):
        advanced.read(operation, {"artifact_id": receipt["artifact_id"]})
    assert not sim.commands


def test_discovery_exposes_bounds_without_expanding_primary_tool_surface():
    for operation in ("waveform.overview", "waveform.image"):
        entry = discover(operation=operation)
        assert entry["access"] == "read"
        assert entry["parameters"]["properties"]["bins"]["maximum"] == 256
