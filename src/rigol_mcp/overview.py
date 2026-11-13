"""Bounded envelopes of saved RAW samples, with optional host-rendered PNGs."""

import io
import math

from PIL import Image, ImageDraw, ImageFont

from .memory import MAX_POINTS, sample_amplitude, sample_time

MAX_BINS = 256


def overview(store, artifact_id, start=0, count=None, bins=64):
    if (type(start) is not int or start < 0 or type(bins) is not int
            or not 1 <= bins <= MAX_BINS or (count is not None and
            (type(count) is not int or not 1 <= count <= MAX_POINTS))):
        raise ValueError("Use start >= 0, count 1..24000000 (or omit), and bins 1..256")
    metadata, raw = store.read(artifact_id, payload=True)
    if start >= len(raw):
        raise ValueError("start must be within the saved capture")
    end = len(raw) if count is None else min(start + count, len(raw))
    size = end - start
    bin_count = min(bins, size)
    scaling = metadata["scaling"]
    rows = []
    for b in range(bin_count):
        left = start + b * size // bin_count
        right = start + (b + 1) * size // bin_count
        block = raw[left:right]
        low, high = min(block), max(block)
        # First occurrence resolves ties deterministically. All samples contribute;
        # stride sampling would lose isolated spikes between selected points.
        rows.append([left, right, left + block.index(low), sample_amplitude(low, scaling),
                     left + block.index(high), sample_amplitude(high, scaling)])
    low_row = min(rows, key=lambda r: r[3])
    high_row = max(rows, key=lambda r: r[5])
    extrema = {}
    actions = {}
    for name, index, value in (("minimum", low_row[2], low_row[3]),
                               ("maximum", high_row[4], high_row[5])):
        extrema[name] = {"sample_index": index, "time_seconds": sample_time(index, scaling),
                         "amplitude": value}
        page_start = max(start, index - 32)
        actions[name] = {"tool": "advanced_read", "arguments": {"operation": "waveform.samples",
            "arguments": {"artifact_id": artifact_id, "start": page_start,
                          "count": min(65, end - page_start)}}}
    return {"ok": True, "simulated": metadata["simulated"], "artifact_id": artifact_id,
            "source_sha256": metadata["sha256"], "start": start, "end_exclusive": end,
            "point_count": size, "total_points": len(raw), "bin_count": bin_count,
            "amplitude_unit": metadata["amplitude_unit"],
            "time_range_seconds": [sample_time(start, scaling), sample_time(end - 1, scaling)],
            "sample_interval_seconds": scaling["x_increment"],
            "columns": ["start", "end_exclusive", "min_index", "min_amplitude", "max_index", "max_amplitude"],
            "envelope": rows, "extrema": extrema, "sample_actions": actions,
            "image_action": {"tool": "advanced_read", "arguments": {"operation": "waveform.image",
                "arguments": {"artifact_id": artifact_id, "start": start, "count": size, "bins": bins}}},
            "note": "Every sample contributes to one bin. Extrema retain exact absolute sample indices; ties use the first occurrence. "
                    "This envelope does not resolve every transition or pulse width. Zoom with start/count, or use waveform.samples for exact data."}


def render_image(result):
    """Render interval ranges without inventing lines between unrelated extrema."""
    image = Image.new("RGB", (1000, 600), "#101923")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=15)
    title_font = ImageFont.load_default(size=20)
    left, top, right, bottom = 110, 108, 965, 450
    foreground, grid, trace = "#edf3fa", "#334253", "#69d5ff"
    lo = result["extrema"]["minimum"]["amplitude"]
    hi = result["extrema"]["maximum"]["amplitude"]
    padding = (hi - lo) * .1 if hi != lo else max(abs(lo) * .1, .001)
    ymin, ymax = lo - padding, hi + padding
    if not all(math.isfinite(v) for v in (ymin, ymax, ymax - ymin)):
        raise ValueError("Waveform amplitude range cannot be plotted")
    start, end = result["start"], result["end_exclusive"]
    # Half-sample outer margins keep a one-sample range visible and ensure each
    # bin's boundaries and sample markers share the same horizontal coordinate.
    def x(index):
        return left + (index - start + .5) / (end - start) * (right - left)
    def y(value):
        return bottom - (value - ymin) / (ymax - ymin) * (bottom - top)
    label = "SIMULATED" if result["simulated"] else "SAVED CAPTURE"
    draw.text((left, 14), f"Waveform envelope | {label}", fill=foreground, font=title_font)
    draw.text((left, 44), f"{result['artifact_id']} | {result['bin_count']} bins | {result['point_count']:,} samples", fill=foreground, font=font)
    draw.text((left, 68), "All-sample min/max ranges; dots mark first extrema. Not a resolved trace.", fill=trace, font=font)
    for step in range(6):
        fraction = step / 5
        value = ymin + fraction * (ymax - ymin)
        py = y(value)
        draw.line((left, py, right, py), fill=grid)
        draw.text((left - 10, py), f"{value:.4g}", fill=foreground, font=font, anchor="rm")
        if end - start == 1 and step > 0:
            continue
        index = start + fraction * (end - start - 1)
        px = x(index)
        draw.line((px, top, px, bottom), fill=grid)
        seconds = result["time_range_seconds"][0] + (index - start) * result["sample_interval_seconds"]
        draw.text((px, bottom + 10), f"{seconds:.5g}", fill=foreground, font=font, anchor="mt")
        draw.text((px, bottom + 55), str(round(index)), fill=foreground, font=font, anchor="mt")
    draw.text((left, top - 23), f"Amplitude ({result['amplitude_unit']})", fill=foreground, font=font)
    for a, b, mini, minv, maxi, maxv in result["envelope"]:
        x0, x1, y0, y1 = x(a - .5), x(b - .5), y(maxv), y(minv)
        draw.rectangle((x0, y0, x1, y1), fill="#23485c")
        draw.line(((x0 + x1) / 2, y0, (x0 + x1) / 2, y1), fill=trace, width=2)
        for idx, val in ((mini, minv), (maxi, maxv)):
            px, py = x(idx), y(val)
            draw.ellipse((px - 2, py - 2, px + 2, py + 2), fill=trace)
    draw.text(((left + right) / 2, 481), "Time (s)", fill=foreground, font=font, anchor="mt")
    draw.text(((left + right) / 2, 528), "Absolute sample index (zero-based)", fill=foreground, font=font, anchor="mt")
    draw.text((left, 552), "Zoom a bin using start/count; read exact samples to confirm pulse widths or edge timing.", fill=foreground, font=font)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    metadata = {k: v for k, v in result.items() if k not in ("columns", "envelope", "image_action")}
    metadata.update({"mime_type": "image/png", "width": 1000, "height": 600,
                     "image_kind": "host_rendered_min_max_envelope", "bytes": stream.tell()})
    return metadata, stream.getvalue()
