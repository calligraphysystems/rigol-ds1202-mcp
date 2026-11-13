import asyncio
import sys
import os
import base64

import pytest
from mcp import Client, StdioServerParameters


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_stdio_end_to_end(mode, tmp_path):
    async def run():
        params = StdioServerParameters(command=sys.executable, args=[
            "-m", "rigol_mcp.server", "--simulate", "--allow-control"], env={**os.environ, "HOME": str(tmp_path)})
        async with Client(params, mode=mode, read_timeout_seconds=15) as client:
            listing = await client.list_tools()
            assert {tool.name for tool in listing.tools} == {
                "inspect_signal", "identify", "status", "measure", "waveform", "acquisition", "set_timebase",
                "discover_operations", "advanced_read", "advanced_control"}
            for tool, arguments in [
                ("inspect_signal", {}), ("identify", {}), ("status", {}),
                ("measure", {"channel": 1, "measurement": "frequency"}),
                ("waveform", {"channel": 1}),
                ("acquisition", {"action": "run"}),
                ("set_timebase", {"seconds_per_division": .002}),
            ]:
                result = await client.call_tool(tool, arguments)
                assert not result.is_error, result
                assert result.structured_content["simulated"] is True
                if tool == "measure":
                    assert result.structured_content["value"] == 1000
                if tool == "waveform":
                    assert result.structured_content["point_count"] == 1200
                    assert "amplitude" not in result.structured_content
                if tool == "inspect_signal":
                    assert result.structured_content["ok"]
                    assert len(result.structured_content["channels"]) == 2
            rejected = await client.call_tool("waveform", {"channel": 1})
            assert rejected.is_error  # Running scope must not be silently stopped.
            rejected = await client.call_tool("acquisition", {"action": "reset"})
            assert rejected.is_error
            discovered = await client.call_tool("discover_operations", {"operation": "channel.set"})
            assert "probe_ratio" in discovered.structured_content["parameters"]["properties"]
            result = await client.call_tool("advanced_control", {"operation": "channel.set", "arguments": {"channel": 1, "coupling": "AC"}})
            assert not result.is_error
            assert result.structured_content["actual"]["coupling"] == "AC"
            rejected = await client.call_tool("advanced_read", {"operation": "channel.set", "arguments": {"channel": 1, "enabled": False}})
            assert rejected.is_error
            assert "advanced_control" in rejected.structured_content["problem"]
            rejected = await client.call_tool("advanced_control", {"operation": "channel.set", "arguments": {"channel": 1, "probe_ratio": 3}})
            assert rejected.is_error and "probe_ratio" in rejected.structured_content["problem"]
            capture = await client.call_tool("advanced_read", {"operation": "screenshot.capture"})
            assert not capture.is_error
            assert len(capture.content) == 1  # No unsolicited image or base64 in context.
            artifact_id = capture.structured_content["artifact_id"]
            image = await client.call_tool("advanced_read", {"operation": "artifact.image", "arguments": {"artifact_id": artifact_id}})
            assert not image.is_error
            assert image.content[1].mime_type == "image/png"
            assert base64.b64decode(image.content[1].data).startswith(b"\x89PNG")
            memory = await client.call_tool("advanced_control", {"operation": "waveform.full_memory", "arguments": {
                "channel": 1, "acquisition_policy": "stop_and_resume"}})
            assert not memory.is_error, memory
            memory_id = memory.structured_content["artifact_id"]
            assert memory.structured_content["point_count"] == 600001
            assert len(str(memory.structured_content)) < 8000
            page = await client.call_tool("advanced_read", {"operation": "waveform.samples", "arguments": {
                "artifact_id": memory_id, "start": 249999, "count": 3}})
            assert not page.is_error and len(page.structured_content["amplitude"]) == 3
            spectrum = await client.call_tool("advanced_read", {"operation": "analysis.fft", "arguments": {"artifact_id": memory_id}})
            assert not spectrum.is_error and spectrum.structured_content["point_count"] == 262144
            saved = await client.call_tool("advanced_read", {"operation": "setup.snapshot"})
            assert not saved.is_error
            restore = await client.call_tool("advanced_control", {"operation": "setup.restore", "arguments": {"artifact_id": saved.structured_content["artifact_id"]}})
            assert not restore.is_error and restore.structured_content["acquisition_after"] == "STOP"
            decode = await client.call_tool("advanced_control", {"operation": "decode.uart.configure", "arguments": {
                "decoder": 1, "tx": "CHAN1", "rx": "OFF", "baud": 10000}})
            assert not decode.is_error
            events = await client.call_tool("advanced_read", {"operation": "decode.events", "arguments": {"decoder": 1}})
            assert not events.is_error and events.structured_content["events"]
            restored = await client.call_tool("advanced_control", {"operation": "decode.restore", "arguments": {
                "artifact_id": decode.structured_content["rollback_artifact_id"]}})
            assert not restored.is_error
            timeout = await client.call_tool("advanced_control", {"operation": "acquisition.capture_single", "arguments": {
                "channel": 1, "timeout_seconds": .2}})
            assert timeout.is_error and timeout.structured_content["timed_out"]
            discovered = await client.call_tool('discover_operations', {'operation':'decode.capture'})
            assert discovered.structured_content['availability']=='implemented'
            bus_timeout = await client.call_tool('advanced_control', {'operation':'decode.capture',
                'arguments':{'protocol':'uart','bit_rate':9600,'timeout_seconds':.2}})
            assert bus_timeout.is_error and bus_timeout.structured_content['restoration']['ok']
            assert bus_timeout.structured_content['quality']['status']=='unavailable'
            # Discovery never changes the stable tool list.
            assert [t.name for t in (await client.list_tools()).tools] == [t.name for t in listing.tools]
        async with Client(params, mode=mode, read_timeout_seconds=15) as client:
            image = await client.call_tool("advanced_read", {"operation": "artifact.image", "arguments": {"artifact_id": artifact_id}})
            assert not image.is_error  # Artifact survives an MCP process restart.
            info = await client.call_tool("advanced_read", {"operation": "waveform.info", "arguments": {"artifact_id": memory_id}})
            assert not info.is_error and info.structured_content["point_count"] == 600001
            rejected = await client.call_tool("measure", {"channel": 3, "measurement": "frequency"})
            assert rejected.is_error
    asyncio.run(run())


def test_stdio_controls_disabled():
    async def run():
        async with Client(StdioServerParameters(command=sys.executable, args=[
            "-m", "rigol_mcp.server", "--simulate"]), read_timeout_seconds=15) as client:
            result = await client.call_tool("acquisition", {"action": "run"})
            assert result.is_error
            result = await client.call_tool("advanced_control", {"operation": "trigger.edge.set", "arguments": {"level": 1}})
            assert result.is_error
    asyncio.run(run())
