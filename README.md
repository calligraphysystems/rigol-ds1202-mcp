# Rigol DS1202Z-E MCP

Ask an AI for a measurement without making it manage SCPI commands, transfer
formats or instrument sequencing. This Linux MCP server gives the Rigol DS1202Z-E
a compact default interface and discoverable advanced operations.

```text
AI client -- SSH / Wi-Fi -- Linux instrument host -- USB -- DS1202Z-E
```

Version 0.5.1. The 0.5.0 baseline was physically tested on one DS1202Z-E running
firmware 00.06.03.SP2. Version 0.5.1 adds software-verified corrective changes;
it has not been deployed to or requalified on hardware. Other models and firmware
are not qualified. Independent community software; no vendor affiliation.

## What it does

- Inspect enabled channels with units, unavailable readings and acquisition context.
- Configure channels/edge triggers and acquire screenshots or full waveform memory.
- Capture single events, save supported setups and restore with readback checks.
- Configure scope serial decoders, or capture/decode supported UART, I2C and SPI
  formats on the host and restore the prior core setup.
- Analyze saved waveforms with FFT and retrieve bounded samples/events on demand.

Ten top-level tools stay fixed. Start with `inspect_signal()`; use
`discover_operations(query="capture decode")` for advanced tasks and request the
selected operation's schema before calling it. No arbitrary SCPI endpoint exists.

## Quick start without hardware

Linux and Python 3.12+ are required. A virtual environment keeps system Python intact.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/rigol-mcp --simulate --identify
.venv/bin/python -m pytest -q
```

The lock includes development/test dependencies. Build tools may need separate
installation. Start `.venv/bin/rigol-mcp --simulate --allow-control` from an MCP
client to exercise simulated operations; it speaks stdio, not a terminal prompt.
Simulation does not verify physical hardware or calibration.

## Connect a real scope

Follow [host and SSH setup](docs/INSTALLATION.md). Use `rigol-mcp --list-usb` to
discover the actual VISA address, then pass `--resource` or set `RIGOL_RESOURCE`.
Controls require `--allow-control`. Physical probe ratio must match configuration.
Review wiring and operation prerequisites before changing acquisition/settings.

## Documentation

- [Full operation guide and limits](docs/API.md)
- [Complete generated schemas and effects](docs/OPERATION-REFERENCE.json)
- [Architecture, ownership and saved artifacts](docs/ARCHITECTURE.md)
- [Validation and reproducible fixture](docs/VALIDATION.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Roadmap: remote bench, PPK II and J-Link](docs/ROADMAP.md)
- [Changes](CHANGELOG.md), [contributing](CONTRIBUTING.md), [security](SECURITY.md)

Historical validation: 193 automated tests and 12 live host-decode/restore runs.
Those counts describe 0.5.0; see the validation guide for the corrective release.
This is functional evidence, not calibration or compliance certification. Private
raw captures and machine configuration are excluded; the validation summary states
tested conditions and gaps. Source catalog evidence paths refer to those historical
private records; they are provenance labels, not downloadable public artifacts.

Completed artifacts persist on the host, subject to retention. Active operations
are synchronous: continuing through SSH loss and durable background jobs are planned,
not implemented. Ordinary setting changes do not automatically roll back.

Version 0.5.1 rejects legacy artifacts without metadata checksums: archive important
evidence, complete any old-version restoration before upgrading, and take new
snapshots after checking the actual state. No automatic migration is performed.
The decode planner now caps legal dual-channel allocations at 600,000 points;
impossible rounded window/rate combinations fail before instrument access.

## Part of the Calligraphy Systems Hardware MCP Suite

This server is one piece of the Calligraphy Systems hardware MCP suite: open,
locally run MCP servers that let an AI assistant work with real engineering tools
and lab equipment. The suite also includes
[calligraphy-eda-mcp](https://github.com/calligraphysystems/calligraphy-eda-mcp)
for PCB layout and design-rule verification. More at
[calligraphyeda.com](https://calligraphyeda.com).

## License and dependencies

Project code is covered by [MIT](LICENSE). Dependencies retain their own licenses
and are installed separately. No vendor firmware, manuals, private flash backups,
credentials or captured user data are distributed.

References: [Rigol programming guide](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/oscilloscopes/DS1000ZE_ProgrammingGuide_EN.pdf),
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk),
[PyVISA-py](https://pyvisa-py.readthedocs.io/en/latest/installation.html).
