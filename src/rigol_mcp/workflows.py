"""Native setup artifacts and bounded single-event acquisition workflows."""

import base64
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid

from .memory import capture_memory
from .operations import CHANNEL_FIELDS, TRIGGER_FIELDS, validate
from .transport import instrument_lock
from .integrity import seal, verify


def settings_fingerprint(io):
    commands = [":TIM:MODE", ":TIM:MAIN:SCAL", ":TIM:MAIN:OFFS", ":ACQ:TYPE", ":ACQ:MDEP",
                ":TRIG:MODE", ":TRIG:SWE"]
    commands += [f":CHAN{c}:{suffix}" for c in (1, 2) for suffix, _ in CHANNEL_FIELDS.values()]
    result = {cmd: io.query(cmd + "?") for cmd in commands}
    if result[":TRIG:MODE"] == "EDGE":
        result.update({f":TRIG:{suffix}": io.query(f":TRIG:{suffix}?") for suffix, _ in TRIGGER_FIELDS.values()})
    return result


def differences(expected, actual):
    result = []
    for key, value in expected.items():
        other = actual.get(key)
        try:
            equal = math.isclose(float(value), float(other), rel_tol=1e-6, abs_tol=1e-12)
        except (TypeError, ValueError):
            equal = value == other
        if not equal:
            result.append({"setting": key, "expected": value, "actual": other})
    return result


def idle_for_control(io):
    state, sweep = io.query(":TRIG:STAT?"), io.query(":TRIG:SWE?")
    if state not in ("STOP", "RUN", "TD", "AUTO") or (sweep == "SING" and state != "STOP"):
        raise ValueError("An acquisition is waiting/armed or its state is unknown; leave it undisturbed or explicitly stop it first")
    if io.query(":FUNC:WREC:ENAB?") != "0":
        raise ValueError("Disable waveform recording/replay before this workflow")
    return state


class SetupStore:
    """Private, same-instrument native blobs. Keep 32 snapshots for 30 days."""
    def __init__(self, root):
        self.root = Path(root)

    def _prune(self):
        for index, path in enumerate(sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)):
            if index >= 32 or time.time() - path.stat().st_mtime > 30 * 86400:
                path.unlink(missing_ok=True)

    def save(self, io, identity, simulated):
        before = settings_fingerprint(io)
        state = io.query(":TRIG:STAT?")
        data = io.binary(":SYST:SET?", 1024 * 1024)
        after = settings_fingerprint(io)
        if differences(before, after):
            raise RuntimeError("Settings changed while exporting setup; snapshot discarded")
        from .scope import Scope
        Scope._check_error(io, "exporting native setup")
        identifier = uuid.uuid4().hex
        metadata = {"ok": True, "artifact_id": identifier, "artifact_type": "setup.native",
                    "instrument": identity, "simulated": simulated, "acquisition_at_save": state,
                    "created_at_utc": datetime.now(timezone.utc).isoformat(), "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(), "verified_settings": after,
                    "verification_scope": "Restorable: both channels, MAIN scale/offset, NORMAL acquisition depth and analog EDGE trigger settings. Native export also archived, but native import is disabled after firmware verification failures. Other settings and waveform samples are not restored."}
        return self.put(data, metadata)

    def put(self, data, metadata):
        identifier = uuid.uuid4().hex
        if not 1 <= len(data) <= 1024 * 1024:
            raise ValueError("Setup/result payload exceeds storage limits")
        metadata = seal({**metadata, "artifact_id": identifier, "bytes": len(data),
                         "sha256": hashlib.sha256(data).hexdigest()})
        with instrument_lock("setups:" + str(self.root.resolve()), 5):
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.root / (identifier + ".json")
            tmp = path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps({**metadata, "payload": base64.b64encode(data).decode("ascii")}))
                tmp.chmod(0o600)
                tmp.replace(path)
                self._prune()
            finally:
                tmp.unlink(missing_ok=True)
        return metadata

    def load(self, identifier):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("Invalid setup artifact ID")
        with instrument_lock("setups:" + str(self.root.resolve()), 5):
            self._prune()
            path = self.root / (identifier + ".json")
            if not path.exists():
                raise ValueError("Setup missing or expired; save another snapshot")
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Invalid setup artifact size")
            metadata = json.loads(path.read_text())
        payload = metadata.pop("payload")
        verify(metadata, identifier)
        data = base64.b64decode(payload, validate=True)
        if not 1 <= len(data) <= 1024 * 1024 or len(data) != metadata["bytes"] or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise ValueError("Setup integrity check failed")
        return metadata, data


def compact_setup(metadata):
    return {k: v for k, v in metadata.items() if k != "verified_settings"}


def restore_plan(metadata):
    """Validate the full saved plan before any write; never execute stored command text."""
    saved = metadata["verified_settings"]
    if saved[":TIM:MODE"] != "MAIN" or saved[":ACQ:TYPE"] != "NORM" or saved[":TRIG:MODE"] != "EDGE":
        raise ValueError("Verified setup restore currently supports MAIN/NORMAL/EDGE only; native import is unavailable on this firmware")
    channels = []
    for c in (1, 2):
        values = {"channel": c}
        for key, (suffix, spec) in CHANNEL_FIELDS.items():
            if key == "range":
                continue
            raw = saved[f":CHAN{c}:{suffix}"]
            if spec["type"] == "boolean":
                if raw not in ("0", "1"):
                    raise ValueError("Invalid saved boolean")
                value = raw == "1"
            else:
                value = float(raw) if spec["type"] == "number" else raw
            values[key] = value
        validate("channel.set", values, "control")
        probe, scale = values["probe_ratio"], values["scale_per_division"]
        if not .001 * probe <= scale <= 10 * probe:
            raise ValueError("Invalid saved scale")
        if abs(values["offset"]) > (100 if scale / probe >= .5 else 2) * probe:
            raise ValueError("Invalid saved offset")
        channels.append(values)
    trigger = {}
    for key, (suffix, spec) in TRIGGER_FIELDS.items():
        raw = saved[f":TRIG:{suffix}"]
        if spec["type"] == "boolean":
            if raw not in ("0", "1"):
                raise ValueError("Invalid saved boolean")
            value = raw == "1"
        else:
            value = float(raw) if spec["type"] == "number" else raw
        trigger[key] = value
    validate("trigger.edge.set", trigger, "control")
    if trigger["source"] not in ("CHAN1", "CHAN2"):
        raise ValueError("Verified setup restore requires an analog EDGE trigger source")
    source = channels[int(trigger["source"][-1]) - 1]
    if not -5 * source["scale_per_division"] - source["offset"] <= trigger["level"] <= 5 * source["scale_per_division"] - source["offset"]:
        raise ValueError("Invalid saved trigger level")
    scale, offset = float(saved[":TIM:MAIN:SCAL"]), float(saved[":TIM:MAIN:OFFS"])
    if not 2e-9 <= scale <= 50 or not math.isfinite(offset):
        raise ValueError("Invalid saved timebase")
    depth = saved[":ACQ:MDEP"]
    if depth != "AUTO":
        depth = str(int(float(depth)))
    validate("acquisition.memory.set", {"depth": depth}, "control")
    enabled = sum(c["enabled"] for c in channels)
    allowed = ([12000, 120000, 1200000, 12000000, 24000000] if enabled == 1 else
               [6000, 60000, 600000, 6000000, 12000000] if enabled == 2 else [])
    if not allowed or depth != "AUTO" and int(depth) not in allowed:
        raise ValueError("Saved memory depth incompatible with saved enabled channels")
    return channels, trigger, scale, offset, depth


def restore_setup(scope, store, artifact_id, acquisition_after="stopped"):
    metadata, _ = store.load(artifact_id)
    channels, trigger, scale, offset, depth = restore_plan(metadata)
    with scope.session(control=True) as (io, identity):
        if identity != metadata["instrument"] or scope.simulated != metadata["simulated"]:
            raise ValueError("Setup belongs to a different instrument or firmware; restore refused")
        idle_for_control(io)
        # Do not silently convert unsupported current modes or overwrite untracked settings.
        if io.query(":TIM:MODE?") != "MAIN" or io.query(":ACQ:TYPE?") != "NORM" or io.query(":TRIG:MODE?") != "EDGE":
            raise ValueError("Current setup must also be MAIN/NORMAL/EDGE for verified restoration")
        saved_state = metadata["acquisition_at_save"]
        if acquisition_after == "saved" and (saved_state not in ("STOP", "RUN", "TD", "AUTO") or
                trigger["sweep"] == "SING" and saved_state != "STOP"):
            raise ValueError("Cannot automatically rearm a saved waiting/single acquisition; use acquisition_after=stopped")
        scope._check_error(io, "before setup restore")
        backup = store.save(io, identity, scope.simulated)
        problem, actual, completed = None, None, []
        def write(command):
            io.write(command)
            scope._check_error(io, "restoring settings")
            completed.append(command.split()[0])
        try:
            # Fixed memory/timebase changes while stopped can time out on this firmware.
            # Temporarily use AUTO depth and running acquisition for the settings sequence.
            write(":TRIG:SWE AUTO")
            write(":RUN")
            scope._wait_acquisition(io, "run")
            if io.query(":ACQ:MDEP?") != "AUTO":
                write(":ACQ:MDEP AUTO")
            # A large delayed-window offset can make a jump to 1 us/div time out.
            # Center the temporary window before shrinking the scale.
            if float(io.query(":TIM:MAIN:OFFS?")) != 0:
                write(":TIM:MAIN:OFFS 0")
            # Only enter a short timebase if deskew actually needs changing.
            # Avoid unnecessary scale jumps and writes to locked TCAL registers.
            deskew_changes = {c['channel']: not math.isclose(float(io.query(f":CHAN{c['channel']}:TCAL?")),
                              c['deskew_seconds'], rel_tol=1e-6, abs_tol=1e-12) for c in channels}
            if any(deskew_changes.values()) and float(io.query(':TIM:MAIN:SCAL?')) >= 1e-5:
                write(":TIM:MAIN:SCAL 0.000001")
            for channel in sorted(channels, key=lambda c: not c["enabled"]):
                for key, (suffix, _) in CHANNEL_FIELDS.items():
                    if key == "range":
                        continue
                    if key == 'deskew_seconds' and not deskew_changes[channel['channel']]:
                        continue
                    value = channel[key]
                    literal = str(int(value)) if type(value) is bool else f"{value:.12g}" if type(value) in (float, int) else value
                    write(f":CHAN{channel['channel']}:{suffix} {literal}")
            write(f":TIM:MAIN:SCAL {scale:.12g}")
            write(f":TIM:MAIN:OFFS {offset:.12g}")
            # Depth writes require RUN on this firmware; do not send one if unchanged.
            current = io.query(":ACQ:MDEP?")
            same = current == depth or current != "AUTO" and depth != "AUTO" and float(current) == float(depth)
            if not same:
                write(f":ACQ:MDEP {depth}")
            for key, (suffix, _) in TRIGGER_FIELDS.items():
                value = trigger[key]
                literal = str(int(value)) if type(value) is bool else f"{value:.12g}" if type(value) in (float, int) else value
                write(f":TRIG:{suffix} {literal}")
            # Selecting SING can arm the scope even while stopped.
            write(":STOP")
            scope._wait_acquisition(io, "stop")
            actual = settings_fingerprint(io)
            if differences(metadata["verified_settings"], actual):
                raise RuntimeError("Restored setup did not match independently verified settings")
            if acquisition_after == "saved" and saved_state != "STOP":
                write(":RUN")
                scope._wait_acquisition(io, "run")
        except Exception as exc:
            problem = str(exc)
        try:
            state = io.query(":TRIG:STAT?")
        except Exception:
            state = "unknown"
        return {"ok": problem is None, "simulated": scope.simulated, "artifact_id": artifact_id,
                "rollback_artifact_id": backup["artifact_id"], "acquisition_after": state,
                "problem": problem, "completed_writes": len(completed),
                "readback_differences": differences(metadata["verified_settings"], actual or {}),
                "verification_scope": metadata["verification_scope"], "restore_method": "validated_setting_commands",
                "note": "Restores the listed settings only, not all instrument settings or retained waveform data. Native import disabled after hardware readback failure.",
                "next_action": "Inspect state before retrying; pre-restore setup is saved." if problem else "Listed settings restored and checked."}


def capture_single(scope, store, setups, channel, timeout_seconds=10, transfer_timeout_seconds=120):
    with scope.session(control=True) as (io, identity):
        scope._enabled(io, channel)
        idle_for_control(io)
        if io.query(":TIM:MODE?") != "MAIN" or io.query(":ACQ:TYPE?") != "NORM":
            raise ValueError("Single capture currently requires MAIN timebase and NORMAL acquisition")
        scope._check_error(io, "before single capture")
        backup = setups.save(io, identity, scope.simulated)
        started = time.monotonic()
        result = {"ok": False, "simulated": scope.simulated, "setup_artifact_id": backup["artifact_id"],
                  "trigger_completed": False, "timed_out": False}
        armed = False
        try:
            io.write(":SING")
            armed = True
            scope._check_error(io, "arming single capture")
            # A short settling interval avoids interpreting an immediately stale STOP reply.
            time.sleep(.1)
            if io.query(":TRIG:SWE?") != "SING":
                raise RuntimeError("Scope did not confirm single sweep")
            while True:
                state = io.query(":TRIG:STAT?")
                if state == "STOP":
                    # Compare setup with expected SINGLE change before publishing any data.
                    expected = {**backup["verified_settings"], ":TRIG:SWE": "SING"}
                    if differences(expected, settings_fingerprint(io)):
                        raise RuntimeError("Settings changed during single acquisition; capture refused")
                    result["trigger_completed"] = True
                    result["wait_seconds"] = round(time.monotonic() - started, 3)
                    captured = capture_memory(scope, store, channel, timeout_seconds=transfer_timeout_seconds, _session=(io, identity))
                    captured = {k: v for k, v in captured.items() if k not in ("scaling", "preamble", "capture_settings")}
                    return {**result, **captured, "single_capture": True,
                            "note": "Single sweep completed and retained memory saved. Scope remains stopped in SINGLE; setup_artifact_id can restore the prior setup. Do not operate the front panel during this workflow; external stop cannot be distinguished from trigger completion."}
                if state not in ("WAIT", "RUN", "TD", "AUTO"):
                    raise RuntimeError(f"Unknown acquisition status {state!r}")
                if io.query(":TRIG:SWE?") != "SING":
                    armed = False
                    raise RuntimeError("Sweep changed externally; cleanup skipped")
                if time.monotonic() - started >= timeout_seconds:
                    result["timed_out"] = True
                    raise TimeoutError("No completed single acquisition within the wait budget; no waveform was published")
                time.sleep(.05)
        except Exception as exc:
            result["problem"] = str(exc)
            result["cleanup_errors"] = []
            if armed:
                try:
                    if io.query(":TRIG:SWE?") == "SING":
                        io.write(":STOP")
                        scope._wait_acquisition(io, "stop")
                        scope._check_error(io, "stopping unsuccessful single capture")
                    else:
                        result["cleanup_errors"].append("Sweep changed externally; cleanup skipped")
                except Exception as cleanup:
                    result["cleanup_errors"].append(str(cleanup))
            try:
                result["acquisition_after"] = io.query(":TRIG:STAT?")
            except Exception:
                result["acquisition_after"] = "unknown"
            result["next_action"] = "Inspect trigger conditions and connection. No automatic retry; restore prior settings with setup.restore if desired."
            return result
