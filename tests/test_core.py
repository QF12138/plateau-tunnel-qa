from sqlalchemy import String, text
from sqlalchemy.orm import Mapped, mapped_column

from tunnel_qa.core.enums import Role, SecurityLevel
from tunnel_qa.core.errors import AppError, PermissionDeniedError
from tunnel_qa.core.security import AccessContext
from tunnel_qa.core.tracing import get_trace_id, set_trace_id
from tunnel_qa.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Probe(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "core_test_probe"

    name: Mapped[str] = mapped_column(String(50), nullable=False)


def test_database_persists_base_entity(database):
    Base.metadata.create_all(database.engine)
    session = database.session_factory()
    probe = Probe(name="test")
    session.add(probe)
    session.commit()

    loaded = session.get(Probe, probe.id)

    assert loaded is not None
    assert loaded.name == "test"
    assert loaded.created_at is not None


def test_sqlite_connections_enforce_integrity_and_wait_for_busy_writers(database):
    with database.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 60000


def test_rbac_and_security_scope():
    engineer = AccessContext(
        user_id="u-1",
        role=Role.FIELD_ENGINEER,
        security_level=SecurityLevel.INTERNAL,
    )

    assert engineer.can("qa:ask")
    assert not engineer.can("data:import")
    assert engineer.can_access(SecurityLevel.PUBLIC)
    assert not engineer.can_access(SecurityLevel.RESTRICTED)

    try:
        engineer.require("data:import")
    except PermissionDeniedError as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("缺少权限时必须拒绝")


def test_error_contract_and_trace_context():
    trace_id = set_trace_id("TRC-test")
    error = AppError("INVALID", "输入无效", field="question")

    assert get_trace_id() == trace_id
    assert error.as_dict(trace_id) == {
        "error_code": "INVALID",
        "message": "输入无效",
        "field": "question",
        "retryable": False,
        "trace_id": "TRC-test",
        "details": {},
    }


def test_trace_context_rejects_header_injection_and_oversized_values():
    injected = set_trace_id("TRC-safe\r\nX-Evil: yes")
    oversized = set_trace_id("T" * 129)

    assert injected.startswith("TRC-") and injected != "TRC-safe\r\nX-Evil: yes"
    assert oversized.startswith("TRC-") and oversized != "T" * 129
