"""Bounded, persistent screenshots. No caller-controlled paths or raw base64 text."""

import hashlib
import io
import json
from pathlib import Path
import re
import time
import uuid
from datetime import datetime, timezone

from PIL import Image

from .transport import instrument_lock
from .integrity import seal, verify


class Artifacts:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else Path.home() / ".cache" / "rigol-mcp" / "screenshots"

    @staticmethod
    def normalize_bitmap(data):
        # Firmware 00.06.03.SP2 emits bad tEXt CRCs in native PNG. BMP24 avoids
        # that defect; decode completely and re-encode without vendor metadata.
        if not data.startswith(b"BM") or len(data) > 2 * 1024 * 1024:
            raise ValueError("Expected a bounded BMP screenshot")
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "BMP" or image.size != (800, 480) or image.mode != "RGB":
                raise ValueError("Expected an 800 × 480 RGB bitmap from the scope")
            image.load()
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()

    @staticmethod
    def verify(data):
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > 2 * 1024 * 1024:
            raise ValueError("Expected a bounded PNG screenshot")
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or image.size != (800, 480):
                raise ValueError("Expected an 800 × 480 DS1202Z-E display image")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()

    def _prune(self):
        paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for index, path in enumerate(paths):
            if index >= 64 or time.time() - path.stat().st_mtime > 7 * 86400:
                path.with_suffix(".png").unlink(missing_ok=True)
                path.unlink(missing_ok=True)

    def save(self, data, context):
        self.verify(data)
        artifact_id = uuid.uuid4().hex
        receipt = {"ok": True, "artifact_id": artifact_id, "mime_type": "image/png",
                   "width": 800, "height": 480, "bytes": len(data),
                   "sha256": hashlib.sha256(data).hexdigest(),
                   "created_at_utc": datetime.now(timezone.utc).isoformat(), **context,
                   "next_action": {"tool": "advanced_read", "arguments": {
                       "operation": "artifact.image", "arguments": {"artifact_id": artifact_id}}}}
        receipt = seal({**receipt, "artifact_type": "screenshot.png"})
        with instrument_lock("artifacts:" + str(self.root.resolve()), 5):
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            png = self.root / f"{artifact_id}.png"
            meta = self.root / f"{artifact_id}.json"
            try:
                with png.open("xb") as stream:
                    stream.write(data)
                with meta.open("x") as stream:
                    json.dump(receipt, stream)
            except Exception:
                png.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                raise
            self._prune()
        return receipt

    def get(self, artifact_id):
        if not re.fullmatch(r"[0-9a-f]{32}", artifact_id):
            raise ValueError("Invalid artifact ID; use the ID returned by screenshot.capture")
        with instrument_lock("artifacts:" + str(self.root.resolve()), 5):
            self._prune()
            try:
                metadata = json.loads((self.root / f"{artifact_id}.json").read_text())
                verify(metadata, artifact_id, "screenshot.png")
                with (self.root / f"{artifact_id}.png").open("rb") as stream:
                    data = stream.read(2 * 1024 * 1024 + 1)
            except FileNotFoundError:
                raise ValueError("Screenshot missing or expired. Capture a new screenshot.") from None
            if len(data) != metadata["bytes"] or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
                raise ValueError("Screenshot failed its integrity check; capture a new one")
            self.verify(data)
        metadata["next_action"] = "Use this image to inspect the display; call inspect_signal for numeric readings."
        return metadata, data
