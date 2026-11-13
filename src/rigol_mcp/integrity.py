"""Versioned artifact metadata checksums, in addition to payload checksums.

Detect accidental changes; not authentication against a writer able to replace
both content and checksums. Legacy unbound metadata is deliberately rejected.
"""
import hashlib
import hmac
import json

VERSION = 1


def _digest(metadata):
    content = {k: v for k, v in metadata.items() if k != "metadata_sha256"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def seal(metadata):
    result = {**metadata, "artifact_schema_version": VERSION}
    result["metadata_sha256"] = _digest(result)
    return result


def verify(metadata, identifier, artifact_type=None):
    if (not isinstance(metadata, dict)
            or type(metadata.get("artifact_schema_version")) is not int
            or metadata["artifact_schema_version"] != VERSION
            or "metadata_sha256" not in metadata):
        raise ValueError("Legacy or unsupported artifact integrity format; preserve the old file "
                         "for offline review and create a new capture/snapshot. Automatic migration is disabled.")
    checksum = metadata["metadata_sha256"]
    try:
        valid = isinstance(checksum, str) and hmac.compare_digest(checksum, _digest(metadata))
    except (ValueError, TypeError):
        valid = False
    if (not valid or metadata.get("artifact_id") != identifier
            or artifact_type is not None and metadata.get("artifact_type") != artifact_type):
        raise ValueError("Artifact metadata integrity check failed; do not use its settings or measurements")
