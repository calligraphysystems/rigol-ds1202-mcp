# Architecture and caller contract

## Current execution path

```text
MCP client -> authenticated SSH/stdin/stdout -> server on Linux host
           -> operation validation -> instrument session/lock -> PyVISA-py/USB
           -> DS1202Z-E
                                  -> private saved artifacts
```

Each client launches a stdio process. There is no HTTP listener, USB-over-IP
bridge, background job service or cross-instrument coordinator. Logs must not
contaminate stdout, which carries MCP. The host performs waveform decoding and
FFT locally; clients receive compact results and explicitly requested detail.

## Interface design

Ten stable tools expose seven basic operations and three advanced gateways.
`inspect_signal` is the default entry; no identity/status preflight is necessary.
`discover_operations` returns compact search results or the exact operation
schema. `advanced_read` and `advanced_control` enforce access classes. Planned
catalog entries cannot execute. There is no arbitrary SCPI escape hatch.

Inputs are checked before commands where possible; dependent limits also require
current instrument settings. Readbacks are authoritative. Controls require
`--allow-control`; that flag is not a promise that other operations send no writes
(for example, waveform transfer parameters are configured when reading samples).

Null measurements mean unavailable, not zero or no signal. `simulated` labels
synthetic evidence. A workflow's `ok` is distinct from decoded quality and expected
payload checks. Responses bound samples/events/images; full details are opt-in.

## Source map

| Module | Responsibility |
| --- | --- |
| `server.py` | CLI, ten MCP tools, annotations and response envelopes |
| `scope.py` | Model identity, basic SCPI measurements, screen traces and controls |
| `transport.py` | VISA lifecycle, IEEE definite-length blocks, Linux file locking |
| `operations.py` | Advanced schemas, requirements, effects and verification catalog |
| `advanced.py` | Gateway dispatch and validated channel/trigger controls |
| `artifacts.py` | Screenshot conversion/storage/integrity and retention |
| `integrity.py` | Versioned canonical metadata checksums and rejection of legacy unbound records |
| `memory.py` | Chunked full-memory capture, calibration and saved waveform retrieval |
| `workflows.py` | Setup snapshots/restoration and awaited single-event capture |
| `decoders.py` | Scope-side UART/I2C/SPI configuration, event tables and restoration |
| `analysis.py` | FFT over a bounded exact region of a saved waveform |
| `bus_workflow.py` | Capture planning, shared-session capture/decode/cleanup and result pages |
| `serial_decode.py` | Pure host sample decoding and quality diagnostics |
| `simulator.py` | Explicitly labeled deterministic instrument substitute for tests |

## Ownership and cleanup

Process-local sequencing and Linux `flock` serialize cooperating processes using
the same user and exact VISA address. Different users, equivalent resource aliases,
front-panel actions and other instrument applications are outside this guarantee.
Multi-step workflows hold one instrument connection/lock through capture/cleanup.

Ordinary controls can partly succeed and do not automatically roll back.
`waveform.full_memory` restores transfer configuration and follows its explicit
acquisition policy. `acquisition.capture_single` leaves STOP even after success.
`decode.capture` restores the prior supported core setup and acquisition state;
it cannot restore overwritten waveform samples. `setup.restore` saves a rollback
snapshot, restores supported fields and defaults to STOP. Decoder restore is
separate. Failures must retain partial-state/restoration evidence for the caller.

No workflow guarantees cleanup after process death, power loss or SSH disruption.
Saved complete artifacts survive reconnection; unfinished jobs are not resumable.
Do not blindly repeat a control after a lost response: inspect current state first.

## Artifact lifecycle

Artifacts live below the MCP host user's `~/.cache/rigol-mcp`, not the client.
Opaque IDs prevent caller-supplied paths; payload and canonical metadata hashes
detect accidental changes to data and its meaning in 0.5.1. Older unbound artifacts
are rejected without automatic migration. Archive evidence before upgrading and
create fresh snapshots after verifying the current instrument state.
Hashes are not authentication against a host-account attacker. Cleanup is performed
on relevant storage/read operations, not by a continuously running expiry service.

| Data | Retention | Retrieval |
| --- | --- | --- |
| Screenshots | 64 / seven days | `artifact.image` |
| Full waveforms | Eight / seven days; at most 192 MB raw retained payload | `waveform.info`, `waveform.samples` |
| Core setups | 32 / 30 days | `setup.info`, explicit `setup.restore` |
| Decoder backups | 32 / 30 days, separate store | Explicit `decode.restore` |
| Host decode results | 32 / 30 days, separate store | `decode.result` |

Decode results may outlive their linked raw waveform artifacts. Archive needed
evidence outside the cache before retention removes it. A saved result ID is not
a permanent laboratory record. Memory capture uses up to 250,000 points per chunk;
sample pages cap at 2,000 points. Decode pages cap at 100 events and 16 KB, default
20; host decoding caps at 4,096 events and 256 I2C data bytes per transaction.

## Limits and dependency boundary

Linux and Python 3.12+ are required; the deployed host uses isolated Python 3.12.
PyVISA/pyvisa-py/PyUSB handle USB, Pillow image validation and NumPy analysis.
`requirements.lock` pins runtime and development dependency artifacts;
`pyproject.toml` declares supported ranges. The test extra adds pytest. Tests are
not physical calibration.

The simulator does not qualify a new scope model, firmware or USB backend. The
model check must not be relaxed merely because another Rigol has similar SCPI.
Hardware features beyond the tested format/rate/voltage conditions require new
evidence, including the target board's actual buses. Firmware and silicon behavior remain
outside the server's control.
