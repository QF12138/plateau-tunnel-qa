"""Shape-safe readers for optional processing metadata on legacy documents.

Older versions allowed arbitrary user values in ``metadata_extra``.  Missing or
non-object values must therefore behave like absent processing information, not
make document lists, citations or idempotent import replays fail.  These helpers
validate object shape only; producers remain responsible for report contents and
bounded samples.
"""

from pydantic import ValidationError

from tunnel_qa.data.schemas import ChunkingConfig, QualityReport


def metadata_object(metadata: object) -> dict:
    return metadata if isinstance(metadata, dict) else {}


def metadata_for_version(metadata: object, version: int | None = None) -> dict:
    current = metadata_object(metadata)
    if version is None or version == current.get("processing_version", 1):
        return current
    history = current.get("processing_history")
    return metadata_object(history.get(str(version))) if isinstance(history, dict) else {}


def saved_chunking(metadata: object, version: int | None = None) -> ChunkingConfig | None:
    stored = metadata_for_version(metadata, version).get("chunking")
    if not isinstance(stored, dict) or not stored:
        return None
    try:
        # Missing historical strategy must not acquire the new key-v2 default.
        return ChunkingConfig.model_validate({"chunk_model": "legacy-structural-v1", **stored})
    except ValidationError:
        return None


def saved_quality(metadata: object) -> QualityReport | None:
    stored = metadata_object(metadata).get("ingestion_quality")
    if not isinstance(stored, dict):
        return None
    try:
        return QualityReport.model_validate(stored)
    except ValidationError:
        return None


def processing_report(metadata: object) -> dict | None:
    """Return a report object, or ``None`` when its container is malformed."""
    if not isinstance(metadata, dict):
        return None
    processing = metadata.get("chunk_processing")
    if not isinstance(processing, dict):
        return None
    report = processing.get("report")
    return dict(report) if isinstance(report, dict) else None


def processing_chunk(metadata: object, ordinal: int) -> dict:
    """Return one chunk's metadata, falling back to an empty object safely."""
    if not isinstance(metadata, dict):
        return {}
    processing = metadata.get("chunk_processing")
    if not isinstance(processing, dict):
        return {}
    chunks = processing.get("chunks")
    if not isinstance(chunks, dict):
        return {}
    chunk = chunks.get(str(ordinal))
    return dict(chunk) if isinstance(chunk, dict) else {}
