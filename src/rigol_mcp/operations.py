"""Small, searchable operation registry. Schemas are returned only on request."""

import math


def choice(*values, description=""):
    return {"type": "string", "enum": list(values), "description": description}


def number(description, minimum=None, maximum=None):
    schema = {"type": "number", "description": description}
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


CHANNEL = {"type": "integer", "enum": [1, 2]}
BOOL = {"type": "boolean"}
OVERVIEW_FIELDS = {
    "artifact_id": {"type": "string"},
    "start": {"type": "integer", "minimum": 0, "default": 0,
              "description": "Absolute zero-based starting sample index."},
    "count": {"type": "integer", "minimum": 1, "maximum": 24000000,
              "description": "Source samples to summarize; omit for all remaining samples. Clipped at capture end."},
    "bins": {"type": "integer", "minimum": 1, "maximum": 256, "default": 64,
             "description": "Maximum output intervals, reduced for short windows. Every source sample contributes."},
}
CHANNEL_FIELDS = {
    "enabled": ("DISP", BOOL),
    "probe_ratio": ("PROB", {**number("Match the physical probe; changes displayed scale, not the probe."),
                             "enum": [.01, .02, .05, .1, .2, .5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000]}),
    "fine_scale": ("VERN", BOOL),
    "scale_per_division": ("SCAL", number("In the channel amplitude unit; range 0.001..10 times probe ratio.")),
    "range": ("RANG", number("Eight divisions, in the channel amplitude unit. Alternative to scale_per_division.")),
    "offset": ("OFFS", number("In the channel amplitude unit; bounds depend on scale and probe ratio.")),
    "coupling": ("COUP", choice("AC", "DC", "GND")),
    "bandwidth_limit": ("BWL", choice("OFF", "20M")),
    "inverted": ("INV", BOOL),
    "amplitude_unit": ("UNIT", choice("VOLT", "AMP", "WATT", "UNKN")),
    "deskew_seconds": ("TCAL", number("Delay correction, quantized by timebase; adjustable only below 10 us/div.", -1e-7, 1e-7)),
}
TRIGGER_FIELDS = {
    "source": ("EDGE:SOUR", choice("CHAN1", "CHAN2", "EXT", "AC")),
    "slope": ("EDGE:SLOP", choice("POS", "NEG", "RFAL", description="Rising, falling, or either edge.")),
    "level": ("EDGE:LEV", number("Source amplitude units; ±5 divisions minus channel offset. Remote setting currently requires CHAN1/CHAN2.")),
    "coupling": ("COUP", choice("AC", "DC", "LFR", "HFR", description="Only applicable with an analog channel source.")),
    "holdoff_seconds": ("HOLD", number("Time before trigger can rearm, in seconds.", 16e-9, 10)),
    "noise_reject": ("NREJ", BOOL),
    "sweep": ("SWE", choice("AUTO", "NORM", "SING", description="Single selects sweep behavior; use acquisition(single) to explicitly arm.")),
}


def schema(properties=None, required=(), minimum_properties=0):
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False, "minProperties": minimum_properties}


OPERATIONS = {
    "setup.snapshot": {
        "access": "read", "summary": "Save restorable channel/timebase/EDGE settings plus a native setup archive; return a persistent ID.",
        "parameters": schema(), "effects": "No scope settings changed. Saves instrument Setups, not waveform samples. Retains 32 snapshots for 30 days.",
        "requirements": "Do not operate the front panel while saving. Use setup.restore with the returned artifact_id."},
    "setup.info": {
        "access": "read", "summary": "Inspect a saved setup's identity and independently verified settings.",
        "parameters": schema({"artifact_id": {"type": "string"}}, ["artifact_id"]), "effects": "No instrument access; validates saved payload integrity."},
    "setup.restore": {
        "access": "control", "summary": "Restore saved channel/timebase/EDGE settings with readback checks and a pre-restore backup.",
        "parameters": schema({"artifact_id": {"type": "string"}, "acquisition_after": {**choice("stopped", "saved"), "default": "stopped"}}, ["artifact_id"]),
        "effects": "Restores listed settings using validated commands; does not import the native blob or restore other settings. May invalidate retained data. Temporarily runs with AUTO depth during restoration, then defaults to STOP; saved explicitly resumes a previously running setup. Saves a rollback ID; does not automatically roll back failures.",
        "requirements": "Same instrument serial/model/firmware; current and saved MAIN/NORMAL/analog EDGE setups. Controls enabled, no armed acquisition or recording/replay. Covers both channels, main scale/offset, memory depth and EDGE settings. Native import failed verification on this firmware and is disabled."},
    "acquisition.capture_single": {
        "access": "control", "summary": "Arm, wait for one event, and save its full-memory waveform in one bounded workflow.",
        "parameters": schema({"channel": CHANNEL,
            "timeout_seconds": {**number("Trigger wait budget; USB calls may extend it.", .2, 60), "default": 10},
            "transfer_timeout_seconds": {**number("Additional memory transfer budget. Client timeout must exceed both budgets plus cleanup.", 5, 300), "default": 120}}, ["channel"]),
        "effects": "Saves the prior setup, replaces retained acquisition, selects SINGLE and leaves STOP on completion/timeout. No forced trigger, automatic retry or automatic resume. On success returns a saved waveform; timeout publishes none.",
        "requirements": "Controls enabled, MAIN/NORMAL, enabled channel, no armed acquisition or recording/replay. Configure trigger first. Do not use the front panel during the call: external STOP cannot be distinguished from completion. Default client timeout should be at least 180 seconds."},
    "acquisition.memory.get": {
        "access": "read", "summary": "Read memory-depth selection, sample rate and available depths for enabled channels.",
        "parameters": schema(), "effects": "None."},
    "acquisition.memory.set": {
        "access": "control", "summary": "Explicitly select acquisition memory depth, including MAXIMUM or AUTO.",
        "parameters": schema({"depth": choice("AUTO", "MAXIMUM", "6000", "12000", "60000", "120000", "600000", "1200000", "6000000", "12000000", "24000000", description="String point count, AUTO, or MAXIMUM. MAXIMUM resolves to 24M for one enabled channel or 12M for two.")}, ["depth"]),
        "effects": "May invalidate retained data and change sampling rate. Does not start acquisition or wait for fresh data. Refuses STOP/WAIT or armed single acquisition; this firmware times out on memory-depth writes while stopped.",
        "requirements": "Controls enabled, acquisition running, at least one channel enabled. Permitted numeric depths depend on enabled channel count; use acquisition.memory.get if needed. Explicitly run the scope first only if replacing retained data is intended."},
    "waveform.full_memory": {
        "access": "control", "summary": "Capture all currently acquired memory for one channel into a saved, calibrated waveform artifact.",
        "parameters": schema({"channel": CHANNEL,
            "acquisition_policy": {**choice("require_stopped", "stop_and_resume", description="Default requires STOP. Explicit stop_and_resume pauses normal running acquisition and resumes it afterward; never interrupts waiting/single triggers."), "default": "require_stopped"},
            "timeout_seconds": {**number("Transfer time budget; an in-flight USB call and cleanup may extend it. Allow client timeout above this budget.", 5, 300), "default": 120}}, ["channel"]),
        "effects": "Uses BYTE/RAW transfer windows and attempts to restore previous transfer settings. Explicit stop_and_resume can replace the retained acquisition on resume. Complete captures are saved even if cleanup fails; partial captures are discarded. No automatic retry.",
        "requirements": "Controls enabled, channel enabled, MAIN timebase, NORMAL acquisition. Reads current memory depth (up to 24M points); does not increase depth or await a new trigger. Do not operate the scope during transfer. Artifacts persist for up to seven days or eight captures, whichever is shorter."},
    "waveform.info": {
        "access": "read", "summary": "Retrieve a saved full-memory capture's compact summary and calibration, without instrument access.",
        "parameters": schema({"artifact_id": {"type": "string"}}, ["artifact_id"]), "effects": "None; verifies saved data integrity."},
    "waveform.samples": {
        "access": "read", "summary": "Read an exact bounded region of saved full-memory waveform samples in seconds and channel amplitude units.",
        "parameters": schema({"artifact_id": {"type": "string"},
            "start": {"type": "integer", "minimum": 0, "default": 0, "description": "Zero-based sample index in the complete capture."},
            "count": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 1000}}, ["artifact_id"]),
        "effects": "No scope access. Returns at most 2000 contiguous calibrated samples; no decimation. A final short page is allowed; next_start is null at the end."},
    "waveform.overview": {
        "access": "read", "summary": "Locate narrow spikes in a compact min/max envelope of a saved waveform, then zoom or request exact samples.",
        "parameters": schema(OVERVIEW_FIELDS, ["artifact_id"]),
        "effects": "No instrument access. Verifies saved metadata and raw payload, scans every selected sample, returns at most 256 bins (64 default) with exact extrema indices. Returns no raw arrays or image. Retains the first occurrence of tied extrema; does not preserve all transitions or pulse widths.",
        "requirements": "Saved waveform artifact from version 0.5.1 or later, within the existing eight-capture/seven-day retention. Use bin start and end_exclusive-start to zoom, or follow sample_actions for exact extrema neighborhoods."},
    "waveform.image": {
        "access": "read", "summary": "View an optional spike-preserving waveform envelope image from a saved capture or zoomed sample window.",
        "parameters": schema(OVERVIEW_FIELDS, ["artifact_id"]),
        "effects": "No instrument access. Same verified min/max calculation as waveform.overview, returned as one 1000x600 native MCP PNG with compact metadata, not base64 text. Host-rendered on demand, not a scope screenshot or resolved trace; no additional persistent image artifact.",
        "requirements": "Saved waveform artifact from version 0.5.1 or later. Default 64 bins; up to 256. Image availability follows raw capture retention. Exact samples are required to establish pulse widths or transition ordering."},
    "channel.get": {
        "access": "read", "summary": "Read all channel settings, including coupling, probe, scale, offset and deskew.",
        "parameters": schema({"channel": CHANNEL}, ["channel"]), "effects": "None."},
    "channel.set": {
        "access": "control", "summary": "Configure a channel with validation and actual readback.",
        "parameters": schema({"channel": CHANNEL, **{k: v[1] for k, v in CHANNEL_FIELDS.items()}}, ["channel"], 2),
        "effects": "Changes only requested fields, but probe/scale/range/fine-scale can affect each other and trigger limits. May invalidate a retained capture. No automatic rollback after partial failure.",
        "requirements": "Controls enabled. Use either range or scale_per_division. Numeric amplitude values use the selected channel unit. Probe setting must match the physical probe."},
    "trigger.edge.get": {
        "access": "read", "summary": "Read active trigger type, sweep and edge-trigger configuration.",
        "parameters": schema(), "effects": "None. Edge settings may be inactive when the trigger type is not EDGE."},
    "trigger.edge.set": {
        "access": "control", "summary": "Select EDGE trigger and configure source, slope, level, sweep, coupling, holdoff or noise rejection.",
        "parameters": schema({k: v[1] for k, v in TRIGGER_FIELDS.items()}, minimum_properties=1),
        "effects": "Selects EDGE even if another trigger type was active. Can change acquisition behavior or replace a captured trace. Does not wait for a new trigger. No automatic rollback after partial failure.",
        "requirements": "Controls enabled. Level and coupling settings require CHAN1/CHAN2 source; level bounds use that channel's actual scale and offset."},
    "screenshot.capture": {
        "access": "read", "summary": "Save a color PNG screenshot of the current display; return a small artifact receipt.",
        "parameters": schema(), "effects": "Does not stop acquisition. Running display can change during capture; image is not raw waveform data.",
        "requirements": "Saved on the MCP host. Fetch with artifact.image; retained for up to seven days or the most recent 64 images, whichever is shorter."},
    "artifact.image": {
        "access": "read", "summary": "Retrieve a saved screenshot as native MCP image content, including from a later connection.",
        "parameters": schema({"artifact_id": {"type": "string", "description": "Opaque ID returned by screenshot.capture."}}, ["artifact_id"]),
        "effects": "None. Returns image content only when explicitly requested."},
}

# Protocol-specific schemas keep irrelevant parameters out of the caller's context.
DECODER_COMMON = {
    "decoder": {**CHANNEL, "description": "Decoder slot 1 or 2, not channel number."},
    "enabled": {**BOOL, "default": True},
    "threshold_ch1": number("CH1 threshold in its amplitude unit, within ±4 divisions minus offset; supplied thresholds disable automatic threshold."),
    "threshold_ch2": number("CH2 threshold in its amplitude unit, within ±4 divisions minus offset; supplied thresholds disable automatic threshold."),
    "auto_threshold": BOOL,
}
UART_FIELDS = {
    "tx": ("UART:TX", choice("CHAN1", "CHAN2", "OFF")),
    "rx": ("UART:RX", choice("CHAN1", "CHAN2", "OFF")),
    "baud": ("UART:BAUD", {"type": "integer", "minimum": 110, "maximum": 20000000}),
    "data_bits": ("UART:WIDT", {"type": "integer", "minimum": 5, "maximum": 8}),
    "stop_bits": ("UART:STOP", {"type": "number", "enum": [1, 1.5, 2]}),
    "parity": ("UART:PAR", choice("NONE", "EVEN", "ODD")),
    "polarity": ("UART:POL", choice("POS", "NEG", description="POS for normal TTL UART; NEG for inverted RS232 signaling.")),
    "bit_order": ("UART:END", choice("LSB", "MSB")),
}
I2C_FIELDS = {
    "clock": ("IIC:CLK", choice("CHAN1", "CHAN2")),
    "data": ("IIC:DATA", choice("CHAN1", "CHAN2")),
    "address_format": ("IIC:ADDR", choice("NORM", "RW", description="NORM excludes R/W from displayed address; RW includes it.")),
}
SPI_FIELDS = {
    "clock": ("SPI:CLK", choice("CHAN1", "CHAN2")),
    "data": ("SPI:MOSI", choice("CHAN1", "CHAN2", description="One observed data direction; mapped to scope MOSI field. Two channels cannot observe clock, CS and both data lines.")),
    "sample_edge": ("SPI:EDGE", choice("RISE", "FALL")),
    "data_bits": ("SPI:WIDT", {"type": "integer", "minimum": 4, "maximum": 32}),
    "bit_order": ("SPI:END", choice("LSB", "MSB")),
    "polarity": ("SPI:POL", choice("POS", "NEG")),
    "frame_timeout_seconds": ("SPI:TIM", number("Gap used to separate frames; greater than the longest clock pulse and shorter than the inter-frame idle gap.", 1e-9, 10)),
}
for protocol, fields, required in (("uart", UART_FIELDS, ["tx", "rx", "baud"]),
                                   ("i2c", I2C_FIELDS, ["clock", "data"]),
                                   ("spi", SPI_FIELDS, ["clock", "data", "sample_edge", "frame_timeout_seconds"])):
    OPERATIONS[f"decode.{protocol}.configure"] = {
        "access": "control", "summary": f"Configure {protocol.upper()} serial-bus hardware decoding with explicit wiring, readback and a saved previous decoder configuration.",
        "parameters": schema({**DECODER_COMMON, **{k: v[1] for k, v in fields.items()}}, ["decoder", *required]),
        "effects": "Changes only the selected decoder and its event-table display. Does not change trigger, channels or acquisition. Sets HEX/package/ascending event view. Returns previous configuration for decode.restore.",
        "requirements": "Connected channels must already be enabled and appropriately scaled. Configure while stopped or normally running, never while waiting/single armed. UART defaults 8N1/POS/LSB; I2C excludes R/W from address; SPI defaults 8-bit/POS/MSB and uses timeout framing (two-channel scope). Omitted threshold defaults to automatic. Actual decoding depends on valid signal and capture length."}
OPERATIONS.update({
    "decode.capture": {
        "access": "control", "summary": "Capture and decode UART, I2C or SPI from full-resolution samples; choose settings and restore the previous setup automatically.",
        "parameters": schema({
            "protocol": choice("uart", "i2c", "spi"),
            "bit_rate": number("Known UART baud or maximum clock rate in Hz. Required; not auto-detected.",100,1000000),
            "data_channel": {**CHANNEL,"default":2}, "clock_channel": {**CHANNEL,"default":1,"description":"I2C/SPI only; must differ from data_channel."},
            "logic_voltage": {**number("Actual positive logic supply voltage; threshold is half this value. No negative/inverted buses.",1,5),"default":3.3},
            "probe_ratio": {"type":"number","enum":[1,10,100],"default":10,"description":"Must match both physical probes."},
            "window_seconds": {**number("Minimum capture window; rounded up to a scope timebase. 10% before and 90% after trigger. Shorten for high rates.",.00001,.5),"default":.05},
            "frame_gap_seconds": number("SPI only: idle gap separating frames; >2 bit periods and less than the actual inter-frame gap. Defaults to 16/bit_rate.",.000001,.1),
            "timeout_seconds": {**number("Single-trigger wait budget; transfers/restoration add time. Set MCP client timeout >=180 seconds.",.2,30),"default":5},
            "expected_hex": {"type":"string","description":"Optional 1..256 known bytes, e.g. 'A5 5A 00 FF'. Checks for an ordered data subsequence; not a protocol compliance test."}
        },["protocol","bit_rate"]),
        "effects":"Enables both channels, sets DC/volts/20 MHz bandwidth, scales, timebase, memory and analog EDGE trigger; replaces retained acquisition. Holds one instrument session through capture and restoration. Saves core setup and waveform IDs; restores saved core/acquisition state even on failure when communication permits. Does not change hardware decoder settings. Returns <=20 events by default, with bounded paging.",
        "requirements":"Controls enabled; initial MAIN/NORMAL/analog EDGE, no armed acquisition or recorder. UART 8N1 normal polarity with >=10 idle bit periods observed before initial framing, SPI mode 0/8-bit/MSB/timeout without CS, I2C 7-bit. User supplies correct wiring/rate/voltage/probe ratio. No front-panel/other-app changes during the call. <=600000 samples per channel using legal dual-channel depths; unsupported rounded window/rate plans rejected before instrument access. >=16 observed samples per declared bit required. No automatic retries or force triggers. Inspect quality separately from ok; ok denotes workflow/restore success, not verified payload. Persistence errors retain restoration details and recovery IDs but do not promise saved result pages."},
    "decode.result": {
        "access":"read","summary":"Read a bounded page of saved capture-and-decode events and quality flags without accessing the scope.",
        "parameters":schema({"artifact_id":{"type":"string"},"start":{"type":"integer","minimum":0,"default":0},"count":{"type":"integer","minimum":1,"maximum":100,"default":20}},["artifact_id"]),
        "effects":"Read-only. Integrity checked; up to 32 results retained for 30 days. Linked waveform artifacts have their own shorter retention.",
        "requirements":"Use artifact_id from decode.capture. Interpret quality flags and expected_sequence_seen; decoded does not certify unknown bytes or electrical compliance."},
    "decode.settings": {"access": "read", "summary": "Inspect a hardware decoder and current protocol parameters.",
        "parameters": schema({"decoder": CHANNEL}, ["decoder"]), "effects": "None."},
    "decode.restore": {"access": "control", "summary": "Restore a saved decoder configuration and event-table settings after an experiment.",
        "parameters": schema({"artifact_id": {"type": "string"}}, ["artifact_id"]), "effects": "Changes decoder settings only; no acquisition changes. Same instrument and firmware required. Backups persist for 30 days or 32 backups. One-step UART baud/SPI timeout rounding is reported explicitly."},
    "decode.events": {"access": "read", "summary": "Read a bounded page of the stopped scope's current hardware decode event table.",
        "parameters": schema({"decoder": CHANNEL, "start": {"type": "integer", "minimum": 0, "default": 0},
            "count": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, ["decoder"]),
        "effects": "None. Reads instrument event table, not host decoding of full-memory artifacts. Normalizes recognized time/data cells and preserves other fields. Pages have a 16 KB event budget.",
        "requirements": "Scope STOP, selected decoder enabled, event table enabled. Use protocol configure first. Configured protocol/baud/thresholds must match the physical signal."},
    "analysis.fft": {"access": "read", "summary": "Find dominant frequencies in a saved waveform, returning a compact calibrated FFT summary.",
        "parameters": schema({"artifact_id": {"type": "string"},
            "start": {"type": "integer", "minimum": 0, "default": 0},
            "count": {"type": "integer", "minimum": 32, "maximum": 1048576, "default": 262144},
            "window": {**choice("hann", "rectangular"), "default": "hann"}, "remove_mean": {**BOOL, "default": True},
            "peaks": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5}}, ["artifact_id"]),
        "effects": "Host calculation only, no scope access. Uses an exact contiguous region without decimation, capped at 1,048,576 points. Returns bin-centered peaks, not a large spectrum array.",
        "requirements": "Saved waveform.full_memory or acquisition.capture_single artifact. For finer frequency resolution capture at a lower sample rate or request a larger count. No automatic signal identification or pass/fail."},
})

# Discoverable coverage gaps, never executable placeholders or claimed hardware features.
PLANNED = {
    "measurement.statistics": "Measurement statistics and comparisons against expected limits",
    "math.configure": "Scope-side math and FFT display controls",

    "cursor.measure": "Manual and tracking cursor measurements",
    "test.mask": "Mask/pass-fail testing; installed options must be checked",
    "record.replay": "Waveform record and playback; installed options must be checked",
    "trigger.advanced": "Pulse, runt, window and other trigger types",
    "system.maintenance": "Explicit reset, calibration and network maintenance",
}

HARDWARE_CHECKS = {
    "channel.get": "All returned settings read on both channels.",
    "channel.set": "All parameter fields exercised and restored across CH1/CH2. Deskew tested at 1 us/div. This does not verify every allowed value or analog accuracy.",
    "trigger.edge.get": "Read with EDGE active.",
    "trigger.edge.set": "Tested CHAN1/CHAN2, POS/NEG/RFAL, DC/HFR coupling, level, holdoff, noise reject, AUTO/NORM. EXT/AC source, AC/LFR coupling, SING sweep and switching from non-EDGE are not hardware-tested.",
    "screenshot.capture": "800x480 BMP24 transfer, lossless host PNG encoding and image integrity validated while running. Native firmware PNG has invalid metadata CRCs and is not used.",
    "artifact.image": "Native MCP image retrieved over SSH, including after a fresh MCP connection.",
}

MEMORY_CHECKS = {
    "waveform.full_memory": "CH1 NORMAL/MAIN captures at 2.4M, 6M and 24M points. Repeated 24M captures matched exactly; sample calibration agreed with the 1 kHz compensation signal. Stop/resume and stopped-state preservation verified. CH2 and other acquisition modes are not hardware-tested.",
    "waveform.info": "Saved 24M capture metadata and integrity read after a fresh network MCP connection.",
    "waveform.samples": "Exact calibrated sample ranges verified across two 250k transfer boundaries and at the final short page of a 24M capture.",
    "acquisition.memory.get": "AUTO and explicit 24M selection plus sample-rate queries verified on CH1-only setup.",
    "acquisition.memory.set": "MAXIMUM (24M, CH1-only) and restoration to AUTO verified while running. Stopped-state writes timed out on this firmware and are now rejected before writing. Other explicit depths and dual-channel settings are unit-tested only.",
}


WORKFLOW_CHECKS = {
    "setup.snapshot": "Native setup archive and explicit settings captured on the physical scope; persisted across sessions. Native import failed live verification and is disabled.",
    "setup.info": "Saved snapshot metadata and integrity retrieved across server instances; identity and corruption rejection unit-tested. Retained for 30 days or 32 snapshots.",
    "setup.restore": "Validated-command restoration of channel coupling, timebase, depth and running/stopped state passed live readback checks. Limited to the documented MAIN/NORMAL/analog EDGE settings; no all-settings or waveform restoration claim.",
    "acquisition.capture_single": "Live CH1 compensation signal completed a single full-memory capture; unreachable trigger timed out with STOP and no waveform artifact. Prior setup restored. External front-panel STOP cannot be distinguished from a completed trigger.",
    "decode.uart.configure": "CH1 10,000-baud 8N1 positive-polarity UART configuration read back correctly. Compensation square wave decoded as repeated F0 frames at 1 ms spacing.",
    "decode.i2c.configure": "Clock CH1/data CH2 configuration read back correctly. Actual I2C traffic decoding remains unverified pending a wired test source.",
    "decode.spi.configure": "Clock CH1/data CH2, RISE, 8-bit/MSB/POS, 10 us timeout configuration read back correctly. Actual SPI traffic decoding remains unverified pending a wired test source.",
    "decode.settings": "All implemented decoder configuration queries exercised on the physical scope.",
    "decode.restore": "Decoder and event-table settings restored after UART/I2C/SPI changes. Observed 1 ns SPI timeout and 1 baud UART quantization are reported explicitly.",
    "decode.events": "Physical UART event table returned 23 F0 frames with 1 ms spacing; parsed time units and hexadecimal values. I2C/SPI payload formats remain unverified; unknown firmware fields are preserved.",
    "analysis.fft": "Physical 12,000-sample capture at 500 kSa/s returned 1/3/5/7/9 kHz peaks, as expected for the compensation square wave. Numerical tone/DC/Nyquist/region tests pass; not calibrated RF spectral accuracy.",
}

def verification(operation):
    if operation in ("waveform.overview", "waveform.image"):
        return {"status": "host-side automated verification", "date": "2026-11-13",
                "scope": "Synthetic saved artifacts, including single-sample spikes and 24M points; calibration, bounded output, integrity rejection and MCP image transport. No new physical acquisition qualification.",
                "evidence": "tests/test_overview.py"}
    if operation in ('decode.capture','decode.result'):
        return {'model':'DS1202Z-E','firmware':'00.06.03.SP2','date':'2026-11-12',
                'scope':'Three physical captures each of TinyPICO UART 9600/8N1, SPI nominal 10 kHz/mode0/8-bit/MSB/timeout and synthetic I2C address 0x50 writes. Full known data sequences recovered, with checked core setup/acquisition restoration. Other rates, voltages, probe ratios and I2C read/repeated-start transactions are not hardware-verified. Full-resolution host decoding; not verification of wide-view hardware event tables.',
                'evidence':'reports/increment-0.5.0-2026-11-12.md'}
    if operation in WORKFLOW_CHECKS:
        return {"model": "DS1202Z-E", "firmware": "00.06.03.SP2", "date": "2026-11-12",
                "scope": WORKFLOW_CHECKS[operation], "evidence": "reports/increment-0.4.0-2026-11-12.md"}
    if operation not in HARDWARE_CHECKS and operation not in MEMORY_CHECKS:
        return {"status": "hardware verification pending"}
    memory = operation in MEMORY_CHECKS
    return {"model": "DS1202Z-E", "firmware": "00.06.03.SP2", "date": "2026-11-12",
            "scope": MEMORY_CHECKS[operation] if memory else HARDWARE_CHECKS[operation],
            "evidence": "reports/memory-test-20261112T202152Z.json" if memory else "reports/advanced-test-20261112T193448Z.json"}


def validate(operation, arguments, access):
    if operation not in OPERATIONS:
        raise ValueError("Operation is not implemented. Use discover_operations to find supported operations.")
    entry = OPERATIONS[operation]
    if entry["access"] != access:
        raise ValueError(f"Use advanced_{entry['access']} for {operation}.")
    spec = entry["parameters"]
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    unknown = set(arguments) - spec["properties"].keys()
    missing = set(spec["required"]) - arguments.keys()
    if unknown or missing or len(arguments) < spec["minProperties"]:
        raise ValueError(f"Invalid parameters: unknown={sorted(unknown)}, missing={sorted(missing)}. "
                         "Supply at least one setting for a control operation; discover its schema first.")
    for key, value in arguments.items():
        field = spec["properties"][key]
        kind = field["type"]
        valid_type = {"boolean": type(value) is bool, "integer": type(value) is int,
                      "number": type(value) in (float, int), "string": type(value) is str}[kind]
        if not valid_type:
            raise ValueError(f"{key} must be {kind}")
        if kind in ("number", "integer") and not math.isfinite(value):
            raise ValueError(f"{key} must be finite")
        if "enum" in field and value not in field["enum"]:
            raise ValueError(f"{key} must be one of {field['enum']}")
        if "minimum" in field and value < field["minimum"] or "maximum" in field and value > field["maximum"]:
            raise ValueError(f"{key} is outside its documented bounds")
    return entry


def discover(query="", operation=None):
    if operation is not None:
        if operation in OPERATIONS:
            return {"operation": operation, "availability": "implemented", **OPERATIONS[operation],
                    "verification": verification(operation),
                    "next_action": f"Call advanced_{OPERATIONS[operation]['access']} with this operation and arguments."}
        if operation in PLANNED:
            return {"operation": operation, "summary": PLANNED[operation], "availability": "planned", "executable": False}
        raise ValueError("Unknown operation; search by task with query instead.")
    words = query.lower().replace("-", " ").split()
    matches = []
    for name, entry in OPERATIONS.items():
        matches.append({"operation": name, "summary": entry["summary"], "access": entry["access"], "availability": "implemented"})
    for name, summary in PLANNED.items():
        matches.append({"operation": name, "summary": summary, "availability": "planned"})
    def score(item):
        haystack = (item["operation"] + " " + item["summary"]).lower()
        return sum(word in haystack for word in words)
    matches = [m for m in matches if not words or score(m)]
    matches.sort(key=lambda m: (-score(m), m["availability"] != "implemented", m["operation"]))
    return {"operations": matches[:10], "more_results": len(matches) > 10,
            "next_action": "Request operation=<exact ID> for parameters and effects. Planned entries cannot execute.",
            "coverage": "Incremental catalog, not the full SCPI command set; unsupported operations are never sent to the scope."}
