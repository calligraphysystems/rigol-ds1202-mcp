"""Bounded RAW-memory transfers and persistent, calibrated waveform artifacts."""

from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid

from .scope import AMPLITUDE_UNITS, finite
from .transport import instrument_lock
from .integrity import seal, verify

MAX_POINTS = 24_000_000
CHUNK_POINTS = 250_000
MEMORY_DEPTHS = {1: (12000, 120000, 1200000, 12000000, 24000000),
                 2: (6000, 60000, 600000, 6000000, 12000000)}


def memory_settings(io):
    channels = [c for c in (1, 2) if io.query(f":CHAN{c}:DISP?") == "1"]
    allowed = list(MEMORY_DEPTHS.get(len(channels), ()))
    selection = io.query(":ACQ:MDEP?")
    return {"enabled_channels": channels, "selected_depth": "AUTO" if selection == "AUTO" else int(selection),
            "available_depths": allowed, "sample_rate_hz": finite(io.query(":ACQ:SRAT?")),
            "note": "AUTO is selected depth policy, not a fixed sample count. Actual retained points come from the waveform preamble."}


def configure_memory(scope, depth):
    with scope.session(control=True) as (io, _):
        state = io.query(":TRIG:STAT?")
        if state not in ("RUN", "TD", "AUTO"):
            raise ValueError("Memory depth must be changed during running acquisition, never STOP/WAIT. Explicitly run the scope first if replacing retained data is intended.")
        if io.query(":TRIG:SWE?") == "SING":
            raise ValueError("Do not change memory depth during an armed single trigger")
        before = memory_settings(io)
        if not before["enabled_channels"]:
            raise ValueError("Enable an analog channel before selecting memory depth")
        requested = (before["available_depths"][-1] if depth == "MAXIMUM" else
                     "AUTO" if depth == "AUTO" else int(depth))
        if requested != "AUTO" and requested not in before["available_depths"]:
            raise ValueError(f"Memory depth for the enabled channels must be AUTO or {before['available_depths']}")
        scope._check_error(io, "before memory depth change")
        io.write(f":ACQ:MDEP {requested}")
        scope._check_error(io, "after memory depth change; acquisition may have changed")
        actual = memory_settings(io)
        return {"ok": actual["selected_depth"] == requested, "simulated": scope.simulated,
                "requested_depth": requested, "before": before, "actual": actual,
                "acquisition_after": io.query(":TRIG:STAT?"),
                "note": "Changing depth can invalidate retained data and change sample rate. It does not wait for a new acquisition."}


def parse_preamble(raw):
    fields = list(map(finite, raw.split(",")))
    if len(fields) != 10:
        raise ValueError("Expected ten waveform preamble fields")
    fmt, mode, points, count, dx, x0, xref, dy, y0, yref = fields
    if fmt != 0 or mode != 2 or points != int(points) or not 1 <= points <= MAX_POINTS:
        raise ValueError("Expected BYTE/RAW memory with 1..24000000 points")
    if count < 1 or count != int(count) or dx <= 0 or dy <= 0:
        raise ValueError("Invalid RAW waveform calibration")
    return {"point_count": int(points), "averages": int(count), "x_increment": dx,
            "x_origin": x0, "x_reference": xref, "y_increment": dy,
            "y_origin": y0, "y_reference": yref}


def sample_time(index, scaling):
    return finite((index - scaling["x_reference"]) * scaling["x_increment"] + scaling["x_origin"])


def sample_amplitude(code, scaling):
    return finite((code - scaling["y_origin"] - scaling["y_reference"]) * scaling["y_increment"])


class WaveformStore:
    """Keep eight complete acquisitions for up to seven days; maximum 192 MB payload."""
    def __init__(self, root):
        self.root = Path(root)

    def _prune(self):
        # A killed process can leave payload without committed metadata.
        # All publishers/readers share this lock, so these cannot be live writes.
        for path in self.root.glob("*.bin"):
            if not path.with_suffix(".json").exists():
                path.unlink()
        paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for index, path in enumerate(paths):
            if index >= 8 or time.time() - path.stat().st_mtime > 7 * 86400:
                path.with_suffix(".bin").unlink(missing_ok=True)
                path.unlink(missing_ok=True)

    def save(self, data, context):
        scaling = context["scaling"]
        if len(data) != scaling["point_count"] or not 1 <= len(data) <= MAX_POINTS:
            raise ValueError("Cannot publish an incomplete memory capture")
        counts = Counter(data)
        amplitudes = {code: sample_amplitude(code, scaling) for code in counts}
        mean = sum(amplitudes[c] * n for c, n in counts.items()) / len(data)
        rms = math.sqrt(sum(amplitudes[c] ** 2 * n for c, n in counts.items()) / len(data))
        identifier = uuid.uuid4().hex
        receipt = {**context, "artifact_id": identifier, "artifact_type": "waveform.raw_byte",
                   "created_at_utc": datetime.now(timezone.utc).isoformat(),
                   "point_count": len(data), "bytes": len(data),
                   "sha256": hashlib.sha256(data).hexdigest(),
                   "sample_interval_seconds": scaling["x_increment"],
                   "time_range_seconds": [sample_time(0, scaling), sample_time(len(data) - 1, scaling)],
                   "summary": {"minimum": min(amplitudes.values()), "maximum": max(amplitudes.values()),
                               "mean": finite(mean), "rms": finite(rms),
                               "unit": context["amplitude_unit"], "method": "All captured BYTE samples; not scope built-in measurements."},
                   "next_action": {"tool": "advanced_read", "arguments": {"operation": "waveform.overview",
                                   "arguments": {"artifact_id": identifier}}}}
        if context["cleanup_errors"]:
            receipt["next_action"] = "Capture is saved, but restoration failed. Inspect acquisition state before further control."
        receipt = seal(receipt)
        with instrument_lock("waveforms:" + str(self.root.resolve()), 5):
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            binary = self.root / f"{identifier}.bin"
            metadata = self.root / f"{identifier}.json"
            try:
                with binary.open("xb") as stream:
                    stream.write(data)
                with metadata.open("x") as stream:
                    json.dump(receipt, stream, allow_nan=False)
            except Exception:
                binary.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                raise
            self._prune()
        return receipt

    def read(self, identifier, *, start=None, count=1000, payload=False):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("Invalid waveform artifact ID")
        with instrument_lock("waveforms:" + str(self.root.resolve()), 5):
            self._prune()
            try:
                metadata = json.loads((self.root / f"{identifier}.json").read_text())
                verify(metadata, identifier, "waveform.raw_byte")
                with (self.root / f"{identifier}.bin").open("rb") as stream:
                    data = stream.read(MAX_POINTS + 1)
            except FileNotFoundError:
                raise ValueError("Waveform missing or expired; capture again") from None
            if (len(data) != metadata["point_count"] or len(data) != metadata["bytes"]
                    or len(data) != metadata["scaling"]["point_count"] or not 1 <= len(data) <= MAX_POINTS
                    or hashlib.sha256(data).hexdigest() != metadata["sha256"]):
                raise ValueError("Waveform failed integrity check; capture again")
            if "preamble" in metadata and parse_preamble(metadata["preamble"]) != metadata["scaling"]:
                raise ValueError("Waveform calibration is inconsistent with its preamble")
        if payload:
            return metadata, data
        if start is None:
            return metadata
        if type(start) is not int or type(count) is not int or not 0 <= start < len(data) or not 1 <= count <= 2000:
            raise ValueError("Use a zero-based start within the capture and count 1..2000")
        end = min(start + count, len(data))
        scaling = metadata["scaling"]
        return {"ok": True, "simulated": metadata["simulated"], "artifact_id": identifier,
                "start": start, "count": end - start, "total_points": len(data),
                "time_seconds": [sample_time(i, scaling) for i in range(start, end)],
                "amplitude": [sample_amplitude(code, scaling) for code in data[start:end]],
                "amplitude_unit": metadata["amplitude_unit"],
                "next_start": end if end < len(data) else None,
                "note": "Exact contiguous samples, not decimated; request only the region needed."}


def transfer_settings(io):
    return {key: io.query(f":WAV:{key}?") for key in ("SOUR", "MODE", "FORM", "STAR", "STOP")}


def fingerprint(io, channel):
    commands = [":TIM:MODE?", ":TIM:MAIN:SCAL?", ":TIM:MAIN:OFFS?", ":ACQ:TYPE?",
                ":ACQ:MDEP?", ":ACQ:SRAT?", ":CHAN1:DISP?", ":CHAN2:DISP?", ":TRIG:SWE?"]
    commands += [f":CHAN{channel}:{suffix}?" for suffix in ("SCAL", "OFFS", "PROB", "COUP", "INV", "BWL", "UNIT", "TCAL")]
    return {command: io.query(command) for command in commands}


def capture_memory(scope, store, channel, acquisition_policy="require_stopped", timeout_seconds=120, _session=None):
    """One locked capture. Partial data is never published. No arbitrary commands."""
    with (nullcontext(_session) if _session is not None else scope.session(control=True)) as (io, identity):
        scope._enabled(io, channel)
        state = io.query(":TRIG:STAT?")
        if state != "STOP" and acquisition_policy == "require_stopped":
            raise ValueError("Full-memory capture requires STOP. Use acquisition_policy='stop_and_resume' to explicitly pause a running acquisition.")
        if state != "STOP" and (state not in ("RUN", "TD", "AUTO") or io.query(":TRIG:SWE?") == "SING"):
            raise ValueError("Will not interrupt a waiting or single trigger. Wait for it to finish, or explicitly stop acquisition first.")
        if io.query(":TIM:MODE?") != "MAIN" or io.query(":ACQ:TYPE?") != "NORM":
            raise ValueError("This increment supports MAIN timebase with NORMAL acquisition only")
        scope._check_error(io, "before memory capture; no settings sent")
        original = transfer_settings(io)
        # Validate device replies before interpolating them into restoration commands.
        if original["SOUR"] not in ("CHAN1", "CHAN2", "MATH") or original["MODE"] not in ("NORM", "MAX", "RAW") or original["FORM"] not in ("BYTE", "WORD", "ASC"):
            raise ValueError("Unexpected original waveform transfer settings")
        for key in ("STAR", "STOP"):
            if not original[key].isdigit() or not 1 <= int(original[key]) <= MAX_POINTS:
                raise ValueError("Unexpected original transfer range")
        data = bytearray()
        cleanup_errors = []
        problem = None
        external_change = False
        resume = state != "STOP"
        started = time.monotonic()
        capture_started_at = datetime.now(timezone.utc).isoformat()
        scaling = None
        before = None
        transfer_changed = False
        try:
            if resume:
                io.write(":STOP")
                scope._wait_acquisition(io, "stop")
                scope._check_error(io, "stopping for memory capture")
            before = fingerprint(io, channel)
            transfer_changed = True
            # Reset the old range before switching modes; then use ascending ranges.
            io.write(":WAV:MODE NORM")
            io.write(":WAV:STAR 1")
            io.write(":WAV:STOP 1200")
            io.write(f":WAV:SOUR CHAN{channel}")
            io.write(":WAV:MODE RAW")
            io.write(":WAV:FORM BYTE")
            scope._check_error(io, "selecting RAW transfer")
            preamble = io.query(":WAV:PRE?")
            scaling = parse_preamble(preamble)
            if not math.isclose(scaling["x_increment"] * finite(before[":ACQ:SRAT?"]), 1, rel_tol=1e-5):
                raise ValueError("RAW sample interval disagrees with acquisition sample rate")
            for start in range(1, scaling["point_count"] + 1, CHUNK_POINTS):
                if time.monotonic() - started >= timeout_seconds:
                    raise TimeoutError("Full-memory transfer exceeded its time budget; no partial artifact published")
                if io.query(":TRIG:STAT?") != "STOP":
                    external_change = True
                    raise RuntimeError("Acquisition restarted during memory transfer")
                end = min(start + CHUNK_POINTS - 1, scaling["point_count"])
                io.write(f":WAV:STOP {end}")
                io.write(f":WAV:STAR {start}")
                actual = transfer_settings(io)
                if actual != {"SOUR": f"CHAN{channel}", "MODE": "RAW", "FORM": "BYTE", "STAR": str(start), "STOP": str(end)}:
                    raise RuntimeError("Scope did not confirm the requested RAW transfer window")
                block = io.binary(":WAV:DATA?", end - start + 1)
                if len(block) != end - start + 1:
                    raise ValueError("Incomplete RAW chunk; no partial artifact published")
                data.extend(block)
                scope._check_error(io, "reading memory chunk")
            if (io.query(":TRIG:STAT?") != "STOP" or fingerprint(io, channel) != before
                    or parse_preamble(io.query(":WAV:PRE?")) != scaling):
                external_change = True
                raise RuntimeError("Acquisition/settings changed during transfer; capture discarded")
        except Exception as exc:
            problem = str(exc)
        finally:
            if external_change:
                cleanup_errors.append("External state change detected; transfer restoration and restart skipped to avoid overriding another operator.")
            else:
                if transfer_changed:
                    try:
                        for command in (":WAV:MODE NORM", ":WAV:STAR 1", ":WAV:STOP 1200",
                                        f":WAV:SOUR {original['SOUR']}", f":WAV:MODE {original['MODE']}",
                                        f":WAV:FORM {original['FORM']}", f":WAV:STOP {original['STOP']}",
                                        f":WAV:STAR {original['STAR']}"):
                            io.write(command)
                        scope._check_error(io, "restoring waveform transfer settings")
                        if transfer_settings(io) != original:
                            raise RuntimeError("Original waveform transfer settings were not confirmed")
                    except Exception as exc:
                        cleanup_errors.append(str(exc))
                if resume:
                    try:
                        io.write(":RUN")
                        scope._wait_acquisition(io, "run")
                        scope._check_error(io, "resuming acquisition")
                    except Exception as exc:
                        cleanup_errors.append(str(exc))
            try:
                final_state = io.query(":TRIG:STAT?")
            except Exception:
                final_state = "unknown"
        outcome = {"ok": problem is None and not cleanup_errors, "simulated": scope.simulated,
                   "instrument": identity, "channel": channel, "acquisition_policy": acquisition_policy,
                   "transfer_started_at_utc": capture_started_at,
                   "acquisition_before": state, "acquisition_after": final_state,
                   "cleanup_errors": cleanup_errors, "transfer_seconds": round(time.monotonic() - started, 3)}
        if problem:
            return {**outcome, "problem": problem, "transferred_points": len(data),
                    "next_action": "Inspect acquisition state and cleanup errors before retrying; partial samples were discarded."}
        receipt = store.save(data, {**outcome, "scaling": scaling, "preamble": preamble,
                            "amplitude_unit": AMPLITUDE_UNITS.get(before[f":CHAN{channel}:UNIT?"], "unknown"),
                            "probe_ratio": finite(before[f":CHAN{channel}:PROB?"]),
                            "capture_complete": True, "capture_settings": before,
                            "limitations": "Current acquired memory, not a request to increase depth or wait for a fresh trigger. BYTE data has 8-bit codes. Other applications/front-panel changes are not locked; rapid restart/stop between checks cannot be excluded."})
        return receipt
