"""VISA transport; each operation gets a fresh connection to avoid stale replies."""

from contextlib import contextmanager
import fcntl
import hashlib
from pathlib import Path
import time

import pyvisa


@contextmanager
def instrument_lock(address: str, timeout_seconds: float):
    """Serialize separate MCP processes as well as calls within one process (Linux)."""
    directory = Path.home() / ".cache" / "rigol-mcp"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (hashlib.sha256(address.encode()).hexdigest() + ".lock")
    with path.open("a") as lock:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Scope busy with another MCP request; retry after it finishes")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def read_block(resource, maximum: int) -> bytes:
    """Read a bounded IEEE 488.2 definite-length block, including Rigol's LF."""
    previous = resource.read_termination
    resource.read_termination = None
    try:
        header = resource.read_bytes(2, break_on_termchar=False)
        if len(header) != 2 or header[:1] != b"#" or header[1:2] not in b"123456789":
            raise ValueError("Invalid definite-length binary block header")
        digits = resource.read_bytes(int(header[1:2]), break_on_termchar=False)
        if len(digits) != int(header[1:2]) or not digits.isdigit():
            raise ValueError("Invalid binary block length")
        length = int(digits)
        if not 1 <= length <= maximum:
            raise ValueError(f"Binary block length {length} exceeds allowed range 1..{maximum}")
        data = resource.read_bytes(length, break_on_termchar=False)
        if len(data) != length:
            raise ValueError("Truncated binary block")
        if resource.read_bytes(1, break_on_termchar=False) != b"\n":
            raise ValueError("Missing Rigol binary block terminator")
        return data
    finally:
        resource.read_termination = previous


class VisaTransport:
    def __init__(self, resource):
        self.resource = resource

    def query(self, command: str) -> str:
        return self.resource.query(command).strip()

    def write(self, command: str) -> None:
        self.resource.write(command)

    def binary(self, command: str, maximum: int) -> bytes:
        self.write(command)
        return read_block(self.resource, maximum)


def visa_factory(address: str, backend: str = "@py", timeout_ms: int = 5000):
    @contextmanager
    def connect():
        with instrument_lock(address, timeout_ms / 1000):
            manager = pyvisa.ResourceManager(backend)
            try:
                resource = manager.open_resource(address, open_timeout=timeout_ms)
                try:
                    resource.timeout = timeout_ms
                    resource.write_termination = "\n"
                    resource.read_termination = "\n"
                    yield VisaTransport(resource)
                finally:
                    resource.close()
            finally:
                manager.close()
    return connect


def discover_usb(backend: str = "@py") -> list[str]:
    manager = pyvisa.ResourceManager(backend)
    try:
        return list(manager.list_resources("USB?*::INSTR"))
    finally:
        manager.close()

