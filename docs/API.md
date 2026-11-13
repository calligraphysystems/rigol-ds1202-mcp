# Rigol DS1202Z-E MCP server

The server owns instrument details so AI callers can ask for results with minimal
setup, sequencing, or interpretation. Start with `inspect_signal()`; no identity
or status preflight is required. Other tools provide specific measurements,
trace details, and explicit controls.

```text
AI client ── SSH over Wi-Fi ── instrument-host ── USB ── DS1202Z-E
```

## AI-facing contract

1. **Ask for the result:** `inspect_signal()` returns a compact report of all
   enabled channels. Optionally select channel 1 or 2.
2. **Server handles mechanics:** Identity checking, units, measurement sequencing,
   invalid-value normalization, and connection lifecycle are internal.
3. **No invented certainty:** Unavailable readings are null. Reports distinguish
   stopped, running, and waiting-for-trigger acquisition states. Signal absence
   and pass/fail are not inferred from invalid readings.
4. **Small default output:** No sample arrays in normal inspection. `waveform`
   returns a compact summary unless `include_samples=true` is requested.
5. **Actionable failures:** Inspection reports connection problems and next
   actions. It advises a bounded retry, not indefinite polling.
6. **Shared instrument:** Calls are serialized within a process and across Linux
   MCP processes using the same user and exact VISA resource. Other applications
   and front-panel actions do not participate in that lock.

| Tool | When to use |
|---|---|
| `inspect_signal(channel=null)` | Default entry point: active channels, common measurements, freshness caveats, next actions |
| `identify()` | Explicit model, serial, firmware, and control/simulator diagnostics |
| `status()` | Detailed channel and timebase setup |
| `measure(channel, measurement)` | One named built-in quantity |
| `waveform(channel, include_samples=false)` | Screen trace summary; optional calibrated time/amplitude arrays |
| `acquisition(action)` | Explicit run, stop, or arm-single request |
| `set_timebase(seconds_per_division)` | Explicit horizontal-scale change, with actual setting read back |
| `discover_operations(query="", operation=null)` | Search advanced tasks; request one operation's exact schema and effects |
| `advanced_read(operation, arguments={})` | Read settings, save/retrieve screenshots, or retrieve saved waveform information/sample ranges |
| `advanced_control(operation, arguments)` | Apply validated settings or capture full acquisition memory |

Supported measurements: frequency, period, peak-to-peak, RMS, average, minimum,
maximum, rise time, fall time, and positive duty cycle. Each value includes its
unit. Probe ratio comes from the scope configuration and must match the physical
probe. Inspection readings are sequential, not a simultaneous snapshot.

## Advanced capabilities without a larger initial schema

The existing seven tools remain compatible. Three stable gateway tools expose
advanced work on demand; discovery never changes the tool list. For example:

```text
discover_operations(query="trigger")
discover_operations(operation="trigger.edge.set")
advanced_control(operation="trigger.edge.set", arguments={"source":"CHAN1","slope":"POS","level":1.5})
```

Discovery returns summaries first, then the chosen operation's parameter schema,
prerequisites, effects and precise hardware-test coverage. The server rejects
unknown parameters, unsupported operations, malformed values and read/write
gateway mismatches before sending a setting. It validates dependent limits against
the scope, serializes each complete operation, and reports actual readback and any
rounding. Partial failures include completed fields and the best available actual
state; ordinary settings changes do not automatically replay or roll back. The
full-memory capture workflow explicitly restores the transfer configuration and,
only when requested, resumes an acquisition it stopped.

Implemented operations:

- `channel.get` / `channel.set`: enable, probe ratio, fine scale, scale or range,
  offset, coupling, bandwidth limit, inversion, unit and deskew.
- `trigger.edge.get` / `trigger.edge.set`: source, slope, level, coupling,
  holdoff, noise rejection and sweep; setting selects EDGE explicitly.
- `acquisition.memory.get` / `acquisition.memory.set`: current memory policy,
  sample rate and permitted depths; select AUTO, MAXIMUM or an explicit supported
  point count. MAXIMUM resolves to 24M for one channel, 12M for both. AUTO may
  choose fewer points. Changing depth requires running acquisition and can
  invalidate retained samples. STOP/WAIT and armed single triggers are rejected.
- `waveform.full_memory`: capture every retained BYTE/RAW sample for one enabled
  channel. Returns a small receipt with summary statistics, point count, time
  range and artifact ID. Calibration and detailed setup remain behind
  `waveform.info`; no sample arrays are returned by the capture operation.
- `waveform.info` / `waveform.samples`: retrieve saved metadata or an exact
  contiguous range of at most 2000 calibrated samples. Indices are zero-based;
  the final page can be shorter. Neither operation accesses the instrument.
- `screenshot.capture`: reads an 800×480 BMP24 image and losslessly encodes PNG.
  Returns a compact receipt with an exact next-call argument object.
- `artifact.image`: returns the saved screenshot as native MCP image content.
  Images persist across connections in `~/.cache/rigol-mcp/screenshots` on the MCP
  host. Retention is at most 64 images or seven days, pruned on capture/retrieval.
  Files remain local to that host and are retrieved through authenticated MCP.

The current firmware's native PNG transfer contains invalid text-metadata CRCs.
The BMP24 route avoids the defect without weakening image validation. Image
retrieval verifies a stored hash and decodes the PNG before returning it.

Built-in measurement statistics, scope-side math/FFT controls, cursors, mask testing,
record/replay, advanced trigger types and maintenance still appear as **planned**,
not executable. Awaited capture, scoped setup restoration, hardware serial decoder
configuration/event reads and host FFT are implemented in 0.4.0. This catalog is incremental; it does not
claim to expose the entire instrument yet. No arbitrary SCPI escape hatch exists.

## Single-event capture and setup restoration (0.4.0)

```text
advanced_read(operation="setup.snapshot")
advanced_control(operation="acquisition.capture_single",
                 arguments={"channel":1,"timeout_seconds":10})
advanced_control(operation="setup.restore",
                 arguments={"artifact_id":"<setup ID>","acquisition_after":"saved"})
```

`acquisition.capture_single` saves the previous setup, arms SINGLE, waits up to
60 seconds (default 10), then transfers the completed full-memory acquisition
under the same instrument lock. The additional transfer budget defaults to
120 seconds. Allow a client timeout above both budgets plus USB/cleanup time
(180 seconds for defaults). Success returns a waveform artifact and a setup ID.
Timeout stops acquisition and publishes no waveform. There is no force-trigger,
automatic retry, or automatic restart; both outcomes leave the scope stopped in
SINGLE. Existing WAIT/armed acquisition and recording/replay are refused.
External front-panel STOP cannot be distinguished from completion: do not operate
the scope during the workflow.

**Setup restoration is explicitly scoped.** It restores both channels, MAIN
scale/offset, NORMAL acquisition depth, and analog EDGE trigger settings. Current
and saved modes must satisfy those prerequisites. It does not restore decoder,
math, display, other trigger modes, or waveform samples. Native setup export is
archived, but native import failed hardware readback verification and is disabled.
The server restores supported settings through validated commands and checks the
result. It temporarily runs with AUTO depth during the sequence, then defaults
to STOP. `acquisition_after="saved"` explicitly resumes a saved running setup;
it never automatically rearms a saved waiting/single acquisition.

Each restore first saves a rollback ID. Failure reports uncertainty and does not
automatically roll back. `setup.info` retrieves saved settings without scope access.
Artifacts are private to the MCP host, tied to instrument identity and firmware,
and retained for at most 30 days or 32 snapshots. `setup.snapshot` contains an
archive of the native setup but cannot promise restoration beyond the listed fields.

## Serial decoding and saved-waveform FFT (0.4.0)

Use `discover_operations(query="serial")` for separate UART, I2C and SPI schemas.
The operations are `decode.uart.configure`, `decode.i2c.configure`,
`decode.spi.configure`, `decode.settings`, `decode.events`, and `decode.restore`.
Sources must already be enabled, scaled and connected. Configuration does not
change channel settings, trigger or acquisition. It returns a decoder backup ID;
restore that separately with `decode.restore`. Decoder backups have the same
30-day/32-artifact retention policy, in a separate directory.

- UART: explicit TX/RX sources and baud, defaults 8N1/POS/LSB. OFF is allowed for
  one direction. Positive logic is typical TTL UART, negative logic inverted RS232.
- I2C: explicit distinct clock/data sources; default address format excludes R/W.
- SPI: clock plus one data line on this two-channel scope. Explicit sampling edge
  and frame timeout; defaults 8-bit/POS/MSB. The data line uses the scope's MOSI
  decoder field regardless of which physical direction you observe. Separate CS
  capture and simultaneous bidirectional decoding are not supported here.
- Threshold defaults to automatic. Supplying either channel threshold disables
  auto threshold; thresholds must be within ±4 divisions minus channel offset.

After a completed/stopped capture, `decode.events` returns up to 20 events by
default (100 maximum, further limited to 16 KB per page). It converts recognized
firmware time strings to seconds and single hexadecimal data cells to integers;
other cells are preserved without inferred protocol meanings. Firmware table
times can be rounded. Unknown table layouts return original cells. This reads
the scope's event table, not host decoding of every sample in a full-memory file.
Empty results are not proof of no traffic. Live I2C packets and narrow-window
SPI/UART frames were verified with a TinyPICO. Wide time views produced incorrect
hardware table values despite valid sampled signals; prefer `decode.capture`
for the supported formats below. Do not infer hardware-table reliability from
acquisition sample rate alone.

```text
advanced_read(operation="analysis.fft", arguments={"artifact_id":"<waveform ID>"})
```

FFT runs entirely on an integrity-checked saved waveform. It returns five dominant
non-DC peaks by default, with frequency, peak amplitude, units, bin spacing and
selected time range. It uses a contiguous region, default 262,144 samples and
maximum 1,048,576, shortened at the capture end; it never silently decimates.
Hann window and mean removal default on; rectangular window and explicit region
selection are available. Amplitude is single-sided and coherent-gain corrected,
not a power spectrum or PSD. Off-bin leakage, finite resolution, aliasing, 8-bit
quantization and physical probe accuracy still apply. No automatic pass/fail or
signal classification is inferred.

See the [validation summary](VALIDATION.md) for
hardware evidence, firmware workarounds and coverage limits.

## Capture and decode in one call (0.5.0)

```text
advanced_control(operation="decode.capture", arguments={
  "protocol":"uart", "bit_rate":9600, "data_channel":2,
  "logic_voltage":3.3, "probe_ratio":10, "window_seconds":0.1
})
```

The caller supplies known wiring, baud/maximum clock rate, logic voltage and
physical probe ratio. The server selects channel scales, timebase, memory depth
and trigger, captures once, decodes the full-resolution samples on the instrument host,
and restores the previous core setup and acquisition state. It does not use the
display-dependent hardware event table, change decoder settings, retry triggers,
or assume that an empty capture means no traffic.

Supported automatic formats are UART 8N1/normal polarity, SPI mode 0/8-bit/MSB
with timeout framing and no CS, and I2C 7-bit addresses. For SPI/I2C use
`clock_channel:1, data_channel:2`. SPI `frame_gap_seconds` defaults to 16 clock
periods and must be shorter than the actual idle gap. Other formats remain
available through protocol-specific hardware decoder operations where supported.
Use only positive 1–5 V logic signals; both probes must match `probe_ratio`.
UART requires at least ten idle bit periods in the capture to establish initial
framing. It then follows contiguous bytes; it does not guess alignment from a
mid-packet fragment. Capture the start of a burst or use the advanced hardware
decoder for a continuous stream with no observable idle boundary.

`window_seconds` defaults to 0.05 and rounds upward to a supported scale, with
10% pretrigger and 90% posttrigger. `bit_rate` is required, never inferred.
The memory planner targets at least 32 samples per declared bit and refuses
requests needing over 600,000 points per channel in 0.5.1. The planner uses only
legal dual-channel depths and rejects impossible plans before instrument access.
Decoding requires at least 16
actual samples per declared bit. High rates require shorter windows.

Interpret the result in this order:

1. `ok` and `restoration.ok`: the operation and cleanup succeeded. A failure
   includes a saved setup ID for recovery. Retained waveform data is not restored.
2. `quality.status`: `decoded`, `suspect`, or `no_data`; inspect flags for clipping,
   missing logic levels, sampling problems, framing errors or expected-data mismatch.
   `decoded` means no detected structural problem, not calibration/compliance or
   proof that an unknown message has correct contents.
3. `events`: at most the first 20 events, further limited to 16 KB. More data is
   retrieved with `decode.result` and the returned `artifact_id`/`next_start`.

Optional `expected_hex`, such as `"A5 5A 00 FF"`, checks for an ordered data-byte
subsequence. For I2C this excludes address and ACK; inspect those fields separately.
Complete events at capture boundaries may be omitted when framing is ambiguous;
`boundary_fragments` reports discarded fragments. I2C transactions are limited to
256 data bytes; total results to 4096 events. A longer view is not automatically
retried when a sequence is missing.

Result pages are integrity checked and retained for 30 days/up to 32 results.
Linked raw waveform artifacts keep their existing seven-day/eight-capture limit.
The workflow holds one instrument connection/lock through configuration, capture
and restoration. Do not change the front panel or use another control application
during a call. Requires an initial MAIN/NORMAL/analog EDGE setup with no armed
acquisition or recording. Allow an MCP timeout of at least 180 seconds.

The stable tool list remains ten tools; this operation is discovered only when
needed, using `discover_operations(query="capture decode")`.

## Artifact compatibility and failure reporting (0.5.1)

Saved screenshots, waveforms, core/decoder setups and decoded results now have a
versioned metadata checksum in addition to their payload checksum. This binds
calibration, units, identity, simulation markers, setup fields and quality/cleanup
information. It detects accidental edits/corruption, not a hostile host user who
can replace content and checksums together.

Artifacts from 0.5.0 or earlier lack that binding and are rejected for retrieval,
analysis and restoration. There is no automatic migration that would label old
metadata verified. Files are not rewritten by rejection, but normal cache retention
still applies; archive important evidence before upgrading. If an old setup needs
restoration, complete that with the old environment before upgrading, inspect the
actual state, then take a fresh snapshot using 0.5.1. Do not simply add checksums
to old files and treat their original integrity as established.

If saving or validating a decoded result fails, the call returns `ok:false`,
`persistence.ok:false`, the storage error, available waveform/setup IDs and the
original restoration outcome. A bounded in-memory event preview may remain; no
result artifact ID or next page is promised. A storage failure never replaces a
restoration failure, and no capture is automatically repeated.

## Full-memory capture

Capture a retained, stopped acquisition without restarting it:

```text
advanced_control(operation="waveform.full_memory", arguments={"channel":1})
```

Explicitly pause a running acquisition, save its memory and resume it:

```text
advanced_control(operation="waveform.full_memory", arguments={"channel":1,"acquisition_policy":"stop_and_resume"})
```

This reads the **currently acquired memory**, not necessarily 24M points, and
does not wait for a fresh trigger. To select the largest depth for future
acquisitions, use `acquisition.memory.set` with `{"depth":"MAXIMUM"}`. That
separate control can invalidate existing data and change sample rate.

Transfers use bounded 250,000-point windows, verifying window readback, block
lengths, acquisition state and calibration/settings consistency. The default
transfer budget is 120 seconds; allow a client timeout of at least 180 seconds
for the default budget. An in-flight USB call and cleanup may extend the budget.
Failure never publishes partial samples. Complete data can be saved even if
cleanup fails; in that case the capture returns an error plus its artifact ID.
No automatic retry occurs. Other applications/front-panel users are not covered
by the server lock, and very rapid stop/run cycles between checks cannot be ruled
out. Detected external changes discard the capture and skip restoration/restart
to avoid overriding another operator.

The scope must use MAIN timebase and NORMAL acquisition. This increment excludes
peak-detect, averaging, high-resolution and MATH-source memory captures. A
waiting or armed single trigger is never silently interrupted; a completed,
stopped single acquisition can be read and is left stopped.

Artifacts contain raw bytes and calibration metadata, with SHA-256 verification
on retrieval. They persist across MCP connections under
`~/.cache/rigol-mcp/waveforms` on the instrument host. At most eight complete captures are
retained for up to seven days; cleanup runs on save/read, including abandoned
payload files. Maximum retained binary payload is 192 MB, plus temporary space
for a capture being saved. Probe ratio must still match the physical probe.

## Operating limits

- Only DS1202Z-E model-specific commands are enabled.
- Waveforms require stopped acquisition; the tool does not silently interrupt an
  armed trigger or restart acquisition. It changes waveform transfer settings to
  BYTE/NORMAL and reads at most 1200 screen points, not full acquisition memory.
- Controls require `--allow-control`; the installed launcher enables them.
- Single acquisition arms and returns immediately; it does not wait for a trigger.
- Timebase changes require YT/MAIN mode. Actual settings are read back.
- Channel and edge-trigger settings are explicit changes, not automatic setup.
  EXT/AC trigger sources can be selected, but remote level/coupling configuration
  currently requires CHAN1/CHAN2. Deskew changes require below 10 microseconds/div.
- CSV export, arbitrary SCPI, reset, calibration and firmware tools are not
  implemented yet. Full-memory operating limits are described above.
- Control tools inspect one SCPI error before and after a write. Reading an error
  consumes that queue entry. A pre-existing error prevents the write; an error
  afterward may leave a setting changed. Ordinary settings changes do not roll
  back; the full-memory workflow attempts its documented cleanup.
- SSH disconnection releases software locks but does not restore scope settings.
- Locking does not prevent another application or someone at the panel from
  changing settings. Avoid those changes while taking measurements.
