# Changelog

Entries below were implemented on 2026-11-12. Hardware coverage is described in
docs/VALIDATION.md; software-only fixes do not imply new physical qualification.

## 0.5.1
Preserve restoration outcome and recovery IDs if decoded-result persistence fails.
Integrity-bind versioned artifact metadata as well as raw payloads; reject legacy
unbound artifacts without automatic migration. Plan only executable dual-channel
memory depths up to 600k points, rejecting impossible requests before instrument
access. Declare Python 3.12+ consistently with the dependency lock. Add focused
gateway, artifact-corruption, legacy-format and memory-boundary regression tests.
No firmware change; no new hardware qualification or remote deployment.

## 0.5.0
One-call host UART/SPI/I2C capture/decode/core restore; persistent paged results;
quality checks and conservative framing; trigger quantization and deskew restoration fixes.
193 automated tests and twelve physical workflow runs recorded.

## 0.4.1
Treat literal `measure error!` as unavailable while preserving other measurements.
Known-signal TinyPICO fixture and hardware-table investigation. 169 tests recorded.

## 0.4.0
Awaited single capture, core setup archives/scoped restore, hardware decoder
configuration/events/restore and host FFT. Native setup import disabled after
failed hardware verification. 166 tests recorded.

## 0.3.0
Chunked full-memory captures, depth selection, bounded samples and persistent
integrity-checked waveforms. Two identical 24M transfers; 132 tests recorded.

## 0.2.0
Discovery/read/control gateways, channel/EDGE configuration and persistent
screenshots through validated BMP24-to-PNG conversion. 88 tests recorded.

## Initial implementation
Compact signal inspection, basic measurement/control, simulator, USB/VISA transport,
Linux locking and authenticated remote stdio deployment. Bounded state confirmation.
