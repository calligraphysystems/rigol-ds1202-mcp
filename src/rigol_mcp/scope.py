"""DS1202Z-E commands based on Rigol's DS1000Z-E programming guide."""

import math
import time
from datetime import datetime, timezone
from contextlib import contextmanager
from threading import RLock


MEASUREMENTS = {
    "frequency": ("FREQ", "Hz"), "period": ("PER", "s"),
    "peak_to_peak": ("VPP", None), "rms": ("VRMS", None),
    "average": ("VAVG", None), "minimum": ("VMIN", None),
    "maximum": ("VMAX", None), "rise_time": ("RTIM", "s"),
    "fall_time": ("FTIM", "s"), "positive_duty_cycle": ("PDUT", "%"),
}
AMPLITUDE_UNITS = {"VOLT": "V", "AMP": "A", "WATT": "W", "UNKN": "unknown"}


def finite(value: str | float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Expected a finite number")
    return number


def measurement_value(raw: str) -> float | None:
    # Observed on DS1202Z-E SP2 when burst traffic has no measurable frequency.
    # Keep unknown text as an error rather than disguising a protocol failure.
    if raw.strip().casefold() == "measure error!":
        return None
    value = float(raw)
    return value if math.isfinite(value) and abs(value) < 1e37 else None


def channel_number(channel: int) -> int:
    if type(channel) is not int or channel not in (1, 2):
        raise ValueError("Channel must be 1 or 2")
    return channel


def identity(raw: str) -> dict:
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 4:
        raise ValueError(f"Malformed instrument identity: {raw!r}")
    return dict(zip(("manufacturer", "model", "serial", "firmware"), parts))


def decode_waveform(preamble: str, data: bytes) -> dict:
    fields = preamble.split(",")
    if len(fields) != 10:
        raise ValueError("Waveform preamble must contain ten fields")
    fmt, mode, points, count, dx, x0, xref, dy, y0, yref = map(finite, fields)
    if fmt != 0 or mode != 0 or points != len(data) or not 1 <= points <= 1200:
        raise ValueError("Expected a complete BYTE/NORMAL screen waveform")
    if dx <= 0 or dy <= 0 or count < 1:
        raise ValueError("Invalid waveform scaling")
    # Rigol Y origin is in ADC codes, not volts; subtract BEFORE scaling.
    return {
        "point_count": len(data), "sample_interval_seconds": dx,
        "time_seconds": [finite((i - xref) * dx + x0) for i in range(len(data))],
        "amplitude": [finite((value - y0 - yref) * dy) for value in data],
        "preamble": preamble,
    }


class Scope:
    def __init__(self, connect, *, allow_control: bool = False, simulated: bool = False):
        self.connect = connect
        self.allow_control = allow_control
        self.simulated = simulated
        self.lock = RLock()

    @contextmanager
    def session(self, *, model_required: bool = True, control: bool = False):
        if control and not self.allow_control:
            raise PermissionError("Controls are disabled. Start with --allow-control to enable them.")
        with self.lock, self.connect() as io:
            info = identity(io.query("*IDN?"))
            if model_required and ("RIGOL" not in info["manufacturer"].upper()
                                   or info["model"].upper() != "DS1202Z-E"):
                raise ValueError(f"Unsupported instrument {info['manufacturer']} {info['model']}. "
                                 "Only DS1202Z-E commands are implemented; identify remains available.")
            yield io, info

    def identify(self) -> dict:
        with self.session(model_required=False) as (_, info):
            return {**info, "simulated": self.simulated, "controls_enabled": self.allow_control}

    def inspect_signal(self, channel: int | None = None) -> dict:
        """One read-only operation for the caller's usual 'what is on the scope?' request."""
        if channel is not None:
            channel_number(channel)
        with self.session() as (io, info):
            trigger = io.query(":TRIG:STAT?")
            timebase = finite(io.query(":TIM:MAIN:SCAL?"))
            channels = []
            actions = []
            for number in ((channel,) if channel is not None else (1, 2)):
                if io.query(f":CHAN{number}:DISP?") != "1":
                    if channel is not None:
                        actions.append(f"Enable channel {number} on the scope, then call inspect_signal again.")
                    continue
                unit = AMPLITUDE_UNITS.get(io.query(f":CHAN{number}:UNIT?"), "unknown")
                measurements = {}
                unavailable = []
                for name in ("frequency", "peak_to_peak", "rms", "average", "minimum", "maximum"):
                    command, fixed_unit = MEASUREMENTS[name]
                    value = measurement_value(io.query(f":MEAS:ITEM? {command},CHAN{number}"))
                    measurements[name] = {"value": value, "unit": fixed_unit or unit}
                    if value is None:
                        unavailable.append(name)
                notes = []
                if unavailable:
                    notes.append("Unavailable: " + ", ".join(unavailable) +
                                 ". This does not by itself prove the signal is absent.")
                    actions.append(f"For channel {number}, check the probe connection, vertical scale, "
                                   "timebase and trigger if these readings are needed.")
                channels.append({"channel": number,
                                 "probe_ratio": finite(io.query(f":CHAN{number}:PROB?")),
                                 "scale_per_division": finite(io.query(f":CHAN{number}:SCAL?")),
                                 "measurements": measurements, "notes": notes})
            if not channels and not actions:
                actions.append("Enable the channel connected to the signal, then call inspect_signal again.")
            state = {"STOP": "stopped; readings describe the retained acquisition",
                     "WAIT": "waiting for trigger; displayed readings may be from an earlier acquisition",
                     "AUTO": "running in auto trigger", "RUN": "running",
                     "TD": "trigger detected"}.get(trigger, f"unrecognized status {trigger!r}")
            return {"ok": True, "simulated": self.simulated,
                    "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "instrument": f"{info['model']} ({info['serial']})",
                    "acquisition": state, "seconds_per_division": timebase,
                    "channels": channels, "next_actions": actions,
                    "controls_enabled": self.allow_control,
                    "limitations": "Sequential built-in readings, not a simultaneous snapshot. "
                    "Amplitudes assume the scope's probe ratio matches the physical probe. "
                    "No pass/fail judgment without expected signal limits."}

    def status(self) -> dict:
        with self.session() as (io, info):
            return {"instrument": info, "simulated": self.simulated,
                    "trigger_status": io.query(":TRIG:STAT?"),
                    "timebase_mode": io.query(":TIM:MODE?"),
                    "seconds_per_division": finite(io.query(":TIM:MAIN:SCAL?")),
                    "channels": [{"channel": c, "enabled": io.query(f":CHAN{c}:DISP?") == "1",
                                  "scale_per_division": finite(io.query(f":CHAN{c}:SCAL?")),
                                  "amplitude_unit": io.query(f":CHAN{c}:UNIT?"),
                                  "probe_ratio": finite(io.query(f":CHAN{c}:PROB?"))}
                                 for c in (1, 2)]}

    def measure(self, channel: int, measurement: str) -> dict:
        channel_number(channel)
        if measurement not in MEASUREMENTS:
            raise ValueError(f"Measurement must be one of {', '.join(MEASUREMENTS)}")
        command, unit = MEASUREMENTS[measurement]
        with self.session() as (io, _):
            self._enabled(io, channel)
            if unit is None:
                unit = AMPLITUDE_UNITS.get(io.query(f":CHAN{channel}:UNIT?"), "unknown")
            raw = io.query(f":MEAS:ITEM? {command},CHAN{channel}")
            value = measurement_value(raw)
            valid = value is not None
            return {"channel": channel, "measurement": measurement,
                    "value": value if valid else None, "unit": unit, "valid": valid,
                    "raw": raw, "simulated": self.simulated}

    def waveform(self, channel: int) -> dict:
        channel_number(channel)
        with self.session() as (io, _):
            self._enabled(io, channel)
            if io.query(":TRIG:STAT?") != "STOP":
                raise ValueError("Stop acquisition before reading a coherent screen waveform, "
                                 "using acquisition('stop') or the front-panel RUN/STOP button.")
            io.write(f":WAV:SOUR CHAN{channel}")
            io.write(":WAV:MODE NORM")
            io.write(":WAV:FORM BYTE")
            io.write(":WAV:STAR 1")
            io.write(":WAV:STOP 1200")
            preamble = io.query(":WAV:PRE?")
            data = io.binary(":WAV:DATA?", 1200)
            if io.query(":TRIG:STAT?") != "STOP":
                raise ValueError("Acquisition restarted during waveform transfer; retry while stopped")
            return {**decode_waveform(preamble, data), "channel": channel,
                    "amplitude_unit": AMPLITUDE_UNITS.get(io.query(f":CHAN{channel}:UNIT?"), "unknown"),
                    "simulated": self.simulated, "mode": "screen",
                    "note": "Screen samples, not full acquisition memory. Transfer settings remain BYTE/NORMAL."}

    def acquisition(self, action: str) -> dict:
        commands = {"run": ":RUN", "stop": ":STOP", "single": ":SING"}
        if action not in commands:
            raise ValueError("Action must be run, stop, or single")
        with self.session(control=True) as (io, _):
            self._check_error(io, "before command; no command sent")
            io.write(commands[action])
            self._check_error(io, "after command; instrument state may have changed")
            # Firmware can report the previous state briefly after accepting a command.
            # Keep the instrument lock until run/stop is actually observed.
            state = self._wait_acquisition(io, action)
            return {"action": action, "trigger_status": state,
                    "simulated": self.simulated,
                    "note": "Single arms the scope; it does not wait for a trigger."}

    @staticmethod
    def _wait_acquisition(io, action: str, timeout: float = 3.0) -> str:
        deadline = time.monotonic() + timeout
        while True:
            state = io.query(":TRIG:STAT?")
            if (action == "single" or (action == "stop" and state == "STOP")
                    or (action == "run" and state in {"TD", "WAIT", "RUN", "AUTO"})):
                return state
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Scope accepted {action!r} but did not confirm it within {timeout:g}s "
                                   f"(last status {state!r}); state may have changed. Read status before retrying.")
            time.sleep(0.05)

    def set_timebase(self, seconds_per_division: float) -> dict:
        value = finite(seconds_per_division)
        if not 2e-9 <= value <= 50:
            raise ValueError("Timebase must be between 2 ns/div and 50 s/div")
        with self.session(control=True) as (io, _):
            if io.query(":TIM:MODE?") != "MAIN":
                raise ValueError("Select YT (MAIN) timebase mode before changing the timebase")
            self._check_error(io, "before command; no command sent")
            io.write(f":TIM:MAIN:SCAL {value:.12g}")
            self._check_error(io, "after command; instrument state may have changed")
            return {"requested_seconds_per_division": value,
                    "actual_seconds_per_division": finite(io.query(":TIM:MAIN:SCAL?")),
                    "simulated": self.simulated}

    @staticmethod
    def _enabled(io, channel):
        if io.query(f":CHAN{channel}:DISP?") != "1":
            raise ValueError(f"Enable channel {channel} on the scope before reading it")

    @staticmethod
    def _check_error(io, context):
        error = io.query(":SYST:ERR?")
        if int(error.split(",", 1)[0]) != 0:
            raise RuntimeError(f"Instrument error {context}: {error}")

