"""Local stdio MCP entry point. Logs belong on stderr, never stdout."""

import argparse
import asyncio
import json
import os
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pyvisa.errors import VisaIOError

from .scope import Scope
from .simulator import Simulator
from .transport import discover_usb, visa_factory
from .advanced import Advanced
from .artifacts import Artifacts
from .operations import discover
import base64


def advanced_error(exc, operation):
    if isinstance(exc, PermissionError):
        action = "Controls are disabled; use an authorized server launched with --allow-control."
    elif isinstance(exc, ValueError):
        action = "Check the operation's schema and stated prerequisites with discover_operations."
    else:
        action = "Check scope connection/current settings before retrying. Do not automatically repeat a control request."
    result = {"ok": False, "operation": operation, "problem": str(exc), "next_action": action}
    return CallToolResult(is_error=True, structured_content=result, content=[TextContent(text=json.dumps(result))])


def build_server(scope: Scope, artifacts: Artifacts | None = None) -> MCPServer:
    advanced = Advanced(scope, artifacts or Artifacts())
    server = MCPServer("Rigol DS1202Z-E", instructions=
                       "Start with inspect_signal() for a compact report of all enabled channels. "
                       "No identify/status preflight is needed; the server handles identity and sequencing. "
                       "Use measure for one specific quantity, waveform for trace detail. "
                       "Follow next_actions when readings are unavailable; do not invent signal values. "
                       "Simulated results are not real measurements. Single only arms a trigger. "
                       "For screenshots, channel setup, trigger settings or other advanced tasks, "
                       "use discover_operations(query=task), then request the chosen operation's schema. "
                       "Use advanced_read for observation and advanced_control for changes. "
                       "Settings readback is authoritative; do not assume a write matched the request.")
    read = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    change = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

    @server.tool(annotations=read, structured_output=True)
    async def inspect_signal(channel: Literal[1, 2] | None = None) -> dict[str, Any]:
        """Start here: inspect all enabled channels, or one channel, in one call.

        Returns acquisition freshness, frequency/amplitude readings with units, probe ratios,
        unavailable readings and concrete next actions. No setup, identify, or status call needed.
        Does not change acquisition or settings. Null readings are not proof of no signal.
        """
        try:
            return await asyncio.to_thread(scope.inspect_signal, channel)
        except (VisaIOError, OSError, TimeoutError) as exc:
            return {"ok": False, "simulated": scope.simulated,
                    "problem": "The instrument connection is unavailable, busy, or timed out.",
                    "detail": str(exc),
                    "next_actions": ["Check that the scope is powered on and its rear USB Device port "
                                     "is connected to workbench; close other scope-control applications.",
                                     "If the problem persists, check USB group permissions and the configured resource."],
                    "retry_guidance": "Retry once after addressing the cause; do not loop indefinitely."}
        except (ValueError, RuntimeError) as exc:
            return {"ok": False, "simulated": scope.simulated, "problem": str(exc),
                    "next_actions": ["Use identify to confirm the supported model; report unexpected instrument replies."]}

    @server.tool(annotations=read, structured_output=True)
    async def identify() -> dict[str, Any]:
        """Read model, serial, firmware, and whether this is a simulator."""
        return await asyncio.to_thread(scope.identify)

    @server.tool(annotations=read, structured_output=True)
    async def status() -> dict[str, Any]:
        """Read acquisition status, timebase, channel scales, units, and probe ratios."""
        return await asyncio.to_thread(scope.status)

    @server.tool(annotations=read, structured_output=True)
    async def measure(channel: Literal[1, 2], measurement: Literal[
        "frequency", "period", "peak_to_peak", "rms", "average", "minimum", "maximum",
        "rise_time", "fall_time", "positive_duty_cycle"
    ]) -> dict[str, Any]:
        """Read a built-in measurement. Invalid/no-signal readings return null with valid=false."""
        return await asyncio.to_thread(scope.measure, channel, measurement)

    @server.tool(annotations=change, structured_output=True)
    async def waveform(channel: Literal[1, 2], include_samples: bool = False) -> dict[str, Any]:
        """Read a compact screen-waveform summary from a STOPPED scope.

        Set include_samples=true only when the individual time/amplitude arrays are needed.
        Changes waveform transfer source/format/range; does not stop or restart acquisition.
        Leaves transfer settings at BYTE/NORMAL. Not a full-memory capture.
        """
        result = await asyncio.to_thread(scope.waveform, channel)
        result["time_range_seconds"] = [result["time_seconds"][0], result["time_seconds"][-1]]
        result["amplitude_range"] = [min(result["amplitude"]), max(result["amplitude"])]
        if not include_samples:
            for key in ("time_seconds", "amplitude", "preamble"):
                del result[key]
        return result

    @server.tool(annotations=change, structured_output=True)
    async def acquisition(action: Literal["run", "stop", "single"]) -> dict[str, Any]:
        """Start, stop, or arm a single acquisition. Requires --allow-control.

        Run/single may replace an existing captured waveform. Single returns before triggering.
        """
        return await asyncio.to_thread(scope.acquisition, action)

    @server.tool(annotations=change, structured_output=True)
    async def set_timebase(seconds_per_division: float) -> dict[str, Any]:
        """Change horizontal scale in YT mode and read back the actual setting.

        Requires --allow-control. Scope may round to a supported 1-2-5 scale.
        """
        return await asyncio.to_thread(scope.set_timebase, seconds_per_division)

    @server.tool(annotations=read, structured_output=True)
    async def discover_operations(query: str = "", operation: str | None = None) -> dict[str, Any]:
        """Find advanced capabilities by task. Supply an operation ID to get its exact schema and effects.

        No instrument access. Planned entries cannot execute; search results stay compact.
        """
        return discover(query, operation)

    @server.tool(annotations=read)
    async def advanced_read(operation: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        """Execute a discovered read operation. Exact parameters come from discover_operations.

        Screenshots return a saved artifact receipt; artifact.image explicitly retrieves the image.
        Cannot change acquisition or channel/trigger settings.
        """
        try:
            result = await asyncio.to_thread(advanced.read, operation, arguments or {})
        except Exception as exc:
            return advanced_error(exc, operation)
        if operation == "artifact.image":
            metadata, data = result
            return CallToolResult(structured_content=metadata, content=[
                TextContent(text=json.dumps(metadata)),
                ImageContent(data=base64.b64encode(data).decode("ascii"), mime_type="image/png")])
        return CallToolResult(structured_content=result, content=[TextContent(text=json.dumps(result))])

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
    async def advanced_control(operation: str, arguments: dict[str, Any]) -> CallToolResult:
        """Execute a discovered control or full-memory capture, requiring --allow-control.

        Validates before writing; returns actual settings and any partial failure. Changes may
        invalidate a retained capture. Inspect readback before retrying. Settings changes
        do not generally roll back; decode.capture explicitly restores the previous core setup.
        Full-memory capture performs its documented transfer/acquisition cleanup.
        """
        try:
            result = await asyncio.to_thread(advanced.control, operation, arguments)
        except Exception as exc:
            return advanced_error(exc, operation)
        return CallToolResult(structured_content=result, content=[TextContent(text=json.dumps(result))],
                              is_error=not result["ok"])

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--simulate", action="store_true", help="Use an explicitly labeled simulator")
    source.add_argument("--resource", help="Explicit VISA resource; defaults to RIGOL_RESOURCE")
    parser.add_argument("--backend", default="@py", help="VISA backend (default: pure Python)")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--allow-control", action="store_true", help="Enable acquisition, timebase, channel and trigger changes")
    parser.add_argument("--list-usb", action="store_true", help="Print USB VISA addresses and exit")
    parser.add_argument("--identify", action="store_true", help="Print identity as JSON and exit")
    args = parser.parse_args()
    if not 100 <= args.timeout_ms <= 60000:
        parser.error("--timeout-ms must be between 100 and 60000")
    if args.list_usb:
        print(json.dumps(discover_usb(args.backend), indent=2))
        return
    address = args.resource or os.getenv("RIGOL_RESOURCE")
    if not args.simulate and not address:
        parser.error("Provide --resource, RIGOL_RESOURCE, or --simulate")
    connect = (Simulator().connect if args.simulate else
               visa_factory(address, args.backend, args.timeout_ms))
    scope = Scope(connect, allow_control=args.allow_control, simulated=args.simulate)
    if args.identify:
        print(json.dumps(scope.identify(), indent=2))
    else:
        build_server(scope).run(transport="stdio")


if __name__ == "__main__":
    main()
