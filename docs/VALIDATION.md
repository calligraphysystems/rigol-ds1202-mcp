# Validation record and reproducibility

## Corrective candidate 0.5.1

255 automated tests pass, including 62 new cases for the pre-publication review.
They cover gateway responses after result write/readback failure with both successful
and failed restoration; metadata changes across setup, waveform, screenshot and
decoded-result stores; legacy artifact rejection; and executable dual-channel
memory boundaries/rejection before instrument access. Python 3.12 is the declared
minimum, matching the hash-locked dependency set.

This corrective candidate is software-verified. It has not been deployed to the
workbench or physically requalified; the physical evidence below belongs to 0.5.0.
Old artifacts without metadata binding are not silently migrated or trusted by
0.5.1. See the API guide for the upgrade/retention implications.

## Status at 0.5.0

The implementation record reports 193 passing automated tests and 12 passing live
capture/decode/restore runs. The documentation/publication preparation pass records
its own test outcome separately. No new physical test is implied by copying code
or rerunning simulation tests.

| Physical exercise | Result | Interpretation boundary |
| --- | --- | --- |
| CH1 compensation output | About 1 kHz, 3.06 Vpp, 50% duty; two matching screen transfers | Built-in source, not independent calibration |
| Channel/EDGE/screenshots | 14 checks; settings restored; BMP24-to-PNG and reconnect retrieval | Tested settings, not every value or analog accuracy |
| Full memory | 21 checks; two identical 24M BYTE records | CH1 maximum-memory case; actual sample rate 250 MSa/s |
| Single capture/setup | Seven checks including unreachable-trigger timeout | Supported MAIN/NORMAL/analog EDGE core only |
| Hardware decoding/FFT | Ten checks; known UART frames and square-wave harmonics | Scope-table view dependence remains; no RF calibration |
| Host UART | Six captures, 40 bytes each at 9600 baud | 8N1 positive logic; ten idle bits needed for initial framing |
| Host SPI | Three captures, ten words each, nominal 10 kHz | Mode 0, 8-bit MSB, gap framing, no chip select |
| Host I2C | Three captures, three transactions each, approximately 3 kHz | 7-bit writes; synthetic ACKs; no real slave |

All 12 host decode runs verified core setup restoration. Fresh MCP sessions fetched
every saved event page and checked full byte sequences. Twelve runs do not prove
all baud rates, logic voltages, probe ratios, continuous UART, I2C repeated starts,
reads/NACKs or real bus electrical behavior. Some of those cases have synthetic
unit-test coverage only. Inspect the operation's verification metadata.

## Automated suite

From a Linux checkout with Python 3.12+:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

The suite covers validation, units/unavailable values, binary framing, artifact
integrity and expiry, chunk/calibration boundaries, explicit controls, partial
failures, setup restrictions, timeout cleanup, decoder/FFT logic and modern/legacy
MCP stdio integration. Subprocess MCP tests need normal local process/IPC support;
the project's restricted execution sandbox has previously caused hangs. A sandbox
hang is not evidence of an instrument fault or a passing test.

The simulator is started with `rigol-mcp --simulate`; adding `--allow-control`
enables simulated controls. Every simulated result must remain labeled. Do not
present simulator values as a connected physical measurement.


## Physical reproduction

Use the [TinyPICO fixture](../fixture/README.md) and the API guide's decode.capture
example. Acquire three runs per protocol with known wiring, rate, ratio and expected
bytes. Check restoration, quality, expected sequence and every saved event page.
For I2C also verify address/direction/ACK/STOP, not just data subsequence. Keep failed
runs and actual settings. Compensation tests require a different physical hookup.

Private machine-specific hardware scripts and raw reports are not distributed.
Historical source catalog coverage text reflects the increment when written;
this summary and the fixture documentation describe later tests without extending
their physical coverage. A source evidence filename is not a public download.

Native setup import, native PNG capture, memory writes while stopped and wide-view
hardware decoding exposed firmware limitations. Production uses the documented
workarounds. No instrument firmware upgrade, unlock or calibration was performed.
Tests do not qualify high-speed buses, analog edge fidelity, RF or antenna behavior.
