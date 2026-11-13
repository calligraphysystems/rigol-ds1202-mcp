"""Validated settings transactions; each holds one instrument session/lock."""

import math

from .operations import CHANNEL_FIELDS, TRIGGER_FIELDS, validate
from .scope import finite
from .memory import WaveformStore, capture_memory, memory_settings, configure_memory
from .workflows import SetupStore, compact_setup, restore_setup, capture_single
from . import decoders
from .analysis import spectrum
from .overview import overview, render_image


def read_fields(io, prefix, fields):
    result = {}
    for name, (suffix, schema) in fields.items():
        value = io.query(f"{prefix}:{suffix}?")
        if schema["type"] == "boolean":
            if value not in ("0", "1"):
                raise ValueError(f"Unexpected boolean reply for {name}: {value!r}")
            value = value == "1"
        elif schema["type"] == "number":
            value = finite(value)
        elif "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"Unexpected reply for {name}: {value!r}")
        result[name] = value
    return result


def read_channel(io, channel):
    return {"channel": channel, **read_fields(io, f":CHAN{channel}", CHANNEL_FIELDS)}


def read_trigger(io):
    mode = io.query(":TRIG:MODE?")
    return {"mode": mode, "edge_settings_active": mode == "EDGE",
            **read_fields(io, ":TRIG", TRIGGER_FIELDS)}


def apply_settings(scope, io, prefix, fields, requested, before, readback, select_edge=False):
    """No silent success: record completed writes and report uncertain partial failure."""
    scope._check_error(io, "before changes; no setting sent")
    completed = []
    attempted = None
    try:
        if select_edge and before["mode"] != "EDGE":
            attempted = "mode"
            io.write(":TRIG:MODE EDGE")
            scope._check_error(io, "selecting EDGE")
            completed.append("mode")
        for name, (suffix, _) in fields.items():
            if name not in requested:
                continue
            attempted = name
            value = requested[name]
            literal = str(int(value)) if type(value) is bool else (f"{value:.12g}" if type(value) in (int, float) else value)
            io.write(f"{prefix}:{suffix} {literal}")
            scope._check_error(io, f"setting {name}")
            completed.append(name)
        actual = readback()
        scope._check_error(io, "reading back settings")
        if select_edge and actual["mode"] != "EDGE":
            raise RuntimeError("Scope did not confirm EDGE trigger mode")
    except Exception as exc:
        try:
            actual = readback()
        except Exception:
            actual = None
        return {"ok": False, "simulated": scope.simulated, "problem": str(exc),
                "completed_fields": completed, "attempted_field": attempted,
                "before": before, "actual": actual, "state_may_have_changed": True,
                "next_action": "Inspect actual settings before retrying. No automatic rollback was attempted."}
    differences = []
    for name, requested_value in requested.items():
        actual_value = actual[name]
        equal = (math.isclose(actual_value, requested_value, rel_tol=1e-6, abs_tol=1e-12)
                 if type(requested_value) in (int, float) else actual_value == requested_value)
        if not equal:
            differences.append({"field": name, "requested": requested_value, "actual": actual_value})
    return {"ok": True, "simulated": scope.simulated, "before": before, "actual": actual,
            "requested": requested, "readback_differences": differences,
            "note": "Actual settings are authoritative; hardware may quantize values. Configuration may invalidate a retained acquisition."}


class Advanced:
    def __init__(self, scope, artifacts):
        self.scope, self.artifacts = scope, artifacts
        self.waveforms = WaveformStore(artifacts.root.parent / "waveforms")
        self.setups = SetupStore(artifacts.root.parent / "setups")
        self.decoders = SetupStore(artifacts.root.parent / "decoders")
        self.bus_results = SetupStore(artifacts.root.parent / "bus-results")

    def read(self, operation, arguments):
        validate(operation, arguments, "read")
        if operation == "decode.result":
            from .bus_workflow import result_page
            return result_page(self.bus_results, **arguments)
        if operation == "analysis.fft":
            return spectrum(self.waveforms, **arguments)
        if operation == "decode.events":
            return decoders.events(self.scope, **arguments)
        if operation == "decode.settings":
            with self.scope.session() as (io, _):
                result = decoders.read_settings(io, **arguments)
                self.scope._check_error(io, "reading decoder settings")
                return {"ok": True, "simulated": self.scope.simulated, **result}
        if operation == "setup.info":
            return self.setups.load(arguments["artifact_id"])[0]
        if operation == "setup.snapshot":
            with self.scope.session() as (io, identity):
                self.scope._check_error(io, "before setup snapshot")
                return compact_setup(self.setups.save(io, identity, self.scope.simulated))
        if operation == "acquisition.memory.get":
            with self.scope.session() as (io, _):
                return {"ok": True, "simulated": self.scope.simulated, **memory_settings(io)}
        if operation == "waveform.info":
            return self.waveforms.read(arguments["artifact_id"])
        if operation in ("waveform.overview", "waveform.image"):
            result = overview(self.waveforms, **arguments)
            return render_image(result) if operation == "waveform.image" else result
        if operation == "waveform.samples":
            return self.waveforms.read(arguments["artifact_id"], start=arguments.get("start", 0), count=arguments.get("count", 1000))
        if operation == "artifact.image":
            return self.artifacts.get(arguments["artifact_id"])
        if operation == "screenshot.capture":
            with self.scope.session() as (io, identity):
                state_before = io.query(":TRIG:STAT?")
                data = io.binary(":DISP:DATA? ON,OFF,BMP24", 2 * 1024 * 1024)
                state_after = io.query(":TRIG:STAT?")
            return self.artifacts.save(self.artifacts.normalize_bitmap(data), {
                "source_format": "BMP24", "encoding": "Lossless PNG encoded on MCP host",
                "instrument": identity, "simulated": self.scope.simulated,
                "acquisition_before": state_before, "acquisition_after": state_after,
                "note": "Display image, not raw samples; acquisition was not stopped. Running display may change during transfer."})
        with self.scope.session() as (io, _):
            result = (read_channel(io, arguments["channel"]) if operation == "channel.get" else read_trigger(io))
            return {"ok": True, "simulated": self.scope.simulated, **result}

    def control(self, operation, arguments):
        validate(operation, arguments, "control")
        if operation == "decode.capture":
            from .bus_workflow import capture
            return capture(self, **arguments)
        if operation in ("decode.uart.configure", "decode.i2c.configure", "decode.spi.configure"):
            return decoders.configure(self.scope, self.decoders, operation.split(".")[1], arguments)
        if operation == "decode.restore":
            return decoders.restore(self.scope, self.decoders, **arguments)
        if operation == "setup.restore":
            return restore_setup(self.scope, self.setups, **arguments)
        if operation == "acquisition.capture_single":
            return capture_single(self.scope, self.waveforms, self.setups, **arguments)
        if operation == "acquisition.memory.set":
            return configure_memory(self.scope, arguments["depth"])
        if operation == "waveform.full_memory":
            result = capture_memory(self.scope, self.waveforms, **arguments)
            # Calibration and SCPI setup stay in the saved artifact, available through waveform.info.
            return {k: v for k, v in result.items() if k not in ("scaling", "preamble", "capture_settings")}
        # Validate all supplied values before any setting is sent, including dependent limits.
        if operation == "channel.set":
            if "range" in arguments and "scale_per_division" in arguments:
                raise ValueError("Use either range or scale_per_division, not both")
            channel = arguments["channel"]
            requested = {k: v for k, v in arguments.items() if k != "channel"}
            with self.scope.session(control=True) as (io, _):
                before = read_channel(io, channel)
                probe = requested.get("probe_ratio", before["probe_ratio"])
                scale = requested.get("scale_per_division", requested.get("range", before["scale_per_division"] * probe / before["probe_ratio"] * 8) / 8)
                if not .001 * probe <= scale <= 10 * probe:
                    raise ValueError(f"Scale must be {.001 * probe:g}..{10 * probe:g} per division for this probe ratio")
                limit = (100 if scale / probe >= .5 else 2) * probe
                if "offset" in requested and abs(requested["offset"]) > limit:
                    raise ValueError(f"Offset must be within ±{limit:g} for the requested scale/probe ratio")
                if "deskew_seconds" in requested and finite(io.query(":TIM:MAIN:SCAL?")) >= 1e-5:
                    if requested["deskew_seconds"] != before["deskew_seconds"]:
                        raise ValueError("Deskew is adjustable only below 10 us/div")
                    # A full settings roundtrip can contain the unchanged, currently locked value.
                    requested.pop("deskew_seconds")
                return apply_settings(self.scope, io, f":CHAN{channel}", CHANNEL_FIELDS,
                                      requested, before, lambda: read_channel(io, channel))
        with self.scope.session(control=True) as (io, _):
            before = read_trigger(io)
            source = arguments.get("source", before["source"])
            if source not in ("CHAN1", "CHAN2") and ("level" in arguments or "coupling" in arguments):
                raise ValueError("Remote level/coupling configuration currently requires CHAN1/CHAN2 source")
            if "level" in arguments:
                scale = finite(io.query(f":{source}:SCAL?"))
                offset = finite(io.query(f":{source}:OFFS?"))
                low, high = -5 * scale - offset, 5 * scale - offset
                if not low <= arguments["level"] <= high:
                    raise ValueError(f"Trigger level must be {low:g}..{high:g} in the source channel amplitude unit")
            return apply_settings(self.scope, io, ":TRIG", TRIGGER_FIELDS, arguments,
                                  before, lambda: read_trigger(io), select_edge=True)
