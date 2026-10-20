from contextvars import ContextVar
import re
from uuid import uuid4


_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def new_trace_id() -> str:
    return f"TRC-{uuid4().hex}"


def set_trace_id(value: str | None = None) -> str:
    trace_id = value if value and _TRACE_ID_PATTERN.fullmatch(value) else new_trace_id()
    _trace_id.set(trace_id)
    return trace_id


def get_trace_id() -> str:
    return _trace_id.get() or set_trace_id()
