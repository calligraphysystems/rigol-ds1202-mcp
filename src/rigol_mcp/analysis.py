"""Bounded, calibrated host FFT on an integrity-checked saved acquisition."""
import numpy as np

from .memory import sample_time


def spectrum(store, artifact_id, start=0, count=262144, window="hann", remove_mean=True, peaks=5):
    metadata, raw = store.read(artifact_id, payload=True)
    if not 0 <= start < len(raw):
        raise ValueError("FFT start is outside the saved capture")
    count = min(count, len(raw) - start)
    if count < 32:
        raise ValueError("FFT requires at least 32 contiguous samples")
    scaling = metadata["scaling"]
    values = (np.frombuffer(raw, dtype=np.uint8, count=count, offset=start).astype(np.float64)
              - scaling["y_origin"] - scaling["y_reference"]) * scaling["y_increment"]
    mean = float(values.mean())
    if remove_mean:
        values -= mean
    weights = np.hanning(count) if window == "hann" else np.ones(count)
    amplitude = np.abs(np.fft.rfft(values * weights)) / weights.sum()
    # Single-sided peak amplitude: never double DC or the even-length Nyquist bin.
    amplitude[1:-1 if count % 2 == 0 else None] *= 2
    spacing = 1 / (count * scaling["x_increment"])
    candidates = np.flatnonzero((amplitude[1:-1] > amplitude[:-2]) & (amplitude[1:-1] >= amplitude[2:])) + 1
    if amplitude[-1] > amplitude[-2]:
        candidates = np.append(candidates, len(amplitude) - 1)
    # Flat/no-AC data should not manufacture meaningful spectral peaks.
    floor = max(float(amplitude.max()) * 1e-9, np.finfo(float).eps)
    candidates = candidates[amplitude[candidates] > floor]
    ranked = candidates[np.argsort(amplitude[candidates])[::-1]]
    chosen = []
    for index in ranked:
        if all(abs(int(index) - item) >= (3 if window == "hann" else 1) for item in chosen):
            chosen.append(int(index))
        if len(chosen) == peaks:
            break
    result = [{"frequency_hz": index * spacing, "peak_amplitude": float(amplitude[index]), "bin": index} for index in chosen]
    return {"ok": True, "simulated": metadata["simulated"], "artifact_id": artifact_id,
            "method": "Host single-sided real FFT, coherent-gain corrected peak amplitude; not scope FFT or power spectral density.",
            "start": start, "point_count": count, "total_capture_points": len(raw),
            "time_range_seconds": [sample_time(start, scaling), sample_time(start + count - 1, scaling)],
            "sample_rate_hz": 1 / scaling["x_increment"], "nyquist_hz": .5 / scaling["x_increment"],
            "bin_spacing_hz": spacing, "window": window, "mean_removed": remove_mean,
            "mean_amplitude": mean, "dc_bin_amplitude": float(amplitude[0]), "amplitude_unit": metadata["amplitude_unit"],
            "peaks": result,
            "limitations": "Contiguous selected region only, with no decimation. Frequencies are bin centers; off-bin tones leak and have amplitude error. DC is excluded from peak ranking. BYTE quantization, aliasing and probe calibration still apply."}
