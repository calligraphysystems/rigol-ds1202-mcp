import math
import numpy as np
import pytest

from rigol_mcp.advanced import Advanced
from rigol_mcp.artifacts import Artifacts
from rigol_mcp.scope import Scope
from rigol_mcp.simulator import Simulator


def rig(tmp_path, monkeypatch, values, dt=.001, dy=.01):
    monkeypatch.setenv("HOME", str(tmp_path))
    sim = Simulator()
    advanced = Advanced(Scope(sim.connect, simulated=True), Artifacts(tmp_path / "screenshots"))
    n = len(values)
    saved = advanced.waveforms.save(bytes(values), {"ok": True, "simulated": True, "cleanup_errors": [],
        "scaling": {"point_count": n, "x_increment": dt, "x_origin": -1, "x_reference": 0,
                    "y_increment": dy, "y_origin": 0, "y_reference": 127}, "amplitude_unit": "V"})
    return sim, advanced, saved["artifact_id"]


@pytest.mark.parametrize("n,window", [(1024, "hann"), (1024, "rectangular"), (1023, "rectangular")])
def test_fft_calibration_and_bin_center_tone(tmp_path, monkeypatch, n, window):
    data = np.rint(127 + 50 * np.sin(2 * np.pi * 40 * np.arange(n) / n)).astype(np.uint8)
    sim, advanced, identifier = rig(tmp_path, monkeypatch, data)
    result = advanced.read("analysis.fft", {"artifact_id": identifier, "count": n, "window": window})
    assert result["peaks"][0]["frequency_hz"] == pytest.approx(40 / (n * .001))
    assert result["peaks"][0]["peak_amplitude"] == pytest.approx(.5, abs=.002)
    assert result["bin_spacing_hz"] == pytest.approx(1 / (n * .001))
    assert result["time_range_seconds"][0] == -1 and not sim.commands


def test_nyquist_and_dc_are_not_doubled(tmp_path, monkeypatch):
    data = [137 + (30 if i % 2 == 0 else -30) for i in range(1024)]
    _, advanced, identifier = rig(tmp_path, monkeypatch, data)
    result = advanced.read("analysis.fft", {"artifact_id": identifier, "window": "rectangular", "remove_mean": False})
    assert result["peaks"][0]["frequency_hz"] == 500
    assert result["peaks"][0]["peak_amplitude"] == pytest.approx(.3)
    assert result["dc_bin_amplitude"] == pytest.approx(.1)


def test_constant_signal_has_no_ac_peaks_and_bounds(tmp_path, monkeypatch):
    sim, advanced, identifier = rig(tmp_path, monkeypatch, [145] * 100)
    result = advanced.read("analysis.fft", {"artifact_id": identifier})
    assert result["peaks"] == [] and result["point_count"] == 100
    for args in ({"count": 1048577}, {"start": 90}, {"start": 100}, {"count": True}, {"window": "unknown"}):
        with pytest.raises(ValueError):
            advanced.read("analysis.fft", {"artifact_id": identifier, **args})
    assert not sim.commands


def test_region_does_not_silently_decimate(tmp_path, monkeypatch):
    _, advanced, identifier = rig(tmp_path, monkeypatch, [127] * 200)
    result = advanced.read("analysis.fft", {"artifact_id": identifier, "start": 100, "count": 128})
    assert result["point_count"] == 100 and result["start"] == 100
    assert result["time_range_seconds"] == pytest.approx([-.9, -.801])
