# Workbench roadmap — planned, not implemented

## Deployment requirement

Keep the Rigol, PPK II, SEGGER J-Link, prototype, probes and power wiring at the
Linux NUC on the workbench. Connect instruments by USB; use Wi-Fi/SSH from the desk.
Each instrument gets a small reusable MCP server. Board-specific sequences sit
above them rather than hard-coding board knowledge into generic instrument drivers.

The present Rigol server already supports remote operation and persistent completed
artifacts. It does not yet guarantee that a capture finishes after Wi-Fi/client loss.

## Proposed order

1. Finish documentation and prepare a reviewed public Rigol release.
2. Inventory and develop PPK II access: stable USB identity, status, bounded current
   capture, idle statistics and charge per event. Treat source output changes as
   explicit controls; verify target voltage, total peak demand and power paths first.
3. Inventory the exact J-Link model and compatible host software. Add target
   identification, supported log collection and fault inspection. Flash, halt,
   reset, memory writes and erase must remain distinct explicit operations.
4. Verify both adapters on a suitable known target before claiming target-board support.
5. Add coordinated startup/alert/sleep tasks with common event markers and bounded
   reports. Add firmware instrumentation and separate modem trace handling as needed.

No PPK II or J-Link software has been installed or hardware tested by this project.
Their availability is user-reported. Their exact device identities, ports and
connections remain to be discovered. RTT needs firmware support; it is not a
replacement for all modem traces. Active debugging may affect sleep current.

## Cross-instrument result contract

Return concise measurements with units, known configuration, timestamp basis,
quality/limitations, and evidence IDs. Separate execution success, measurement
quality, expected behavior and restoration success. Link large raw traces rather
than returning them by default. Never infer battery life solely from one capture:
it also needs a usage model, battery assumptions and sleep behavior.

PPK digital event markers and scope trigger channels could align firmware activity
with measurements. Wall-clock timestamps alone are not precise synchronization.
PPK sampling is not a substitute for scope bandwidth, and two scope channels limit
simultaneous observations. Antenna matching and RF qualification remain separate.

## Durable jobs requirement

A future host worker needs persistent job IDs, bounded execution, status/results,
explicit cancellation, ownership/locking, restart recovery and disk quotas. A
reconnected caller must retrieve the original job rather than starting it twice.
Specify safe device state and cleanup after client loss versus host power loss;
do not promise rollback after a hard failure. These are acceptance requirements,
not a claim that current synchronous SSH tools already implement them.

## Remaining Rigol scope work

Measurement statistics/limit checks, advanced trigger types, recording/replay,
cursors, mask testing, scope-side math controls and additional verified formats
remain candidates. Prioritize real diagnostic tasks and test signals, preserve the
small tool list, and expose exact schemas only through discovery. Do not weaken
model checks or add broad SCPI execution as a shortcut to unverified functionality.

Before prototype testing, map test points, safe voltage limits, expected rails,
reset timing, actual bus rates/formats, firmware logging, radio events and sleep
criteria. TinyPICO validates the present measurement path; it does not qualify
the target board or establish RF/antenna performance.
