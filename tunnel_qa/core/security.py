from dataclasses import dataclass

from tunnel_qa.core.enums import Role, SECURITY_RANK, SecurityLevel
from tunnel_qa.core.errors import PermissionDeniedError


ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.FIELD_ENGINEER: frozenset({"qa:ask", "knowledge:read", "feedback:write"}),
    Role.RESEARCHER: frozenset(
        {"qa:ask", "knowledge:read", "knowledge:propose", "feedback:write", "evaluation:run"}
    ),
    Role.REVIEWER: frozenset(
        {"qa:ask", "knowledge:read", "knowledge:review", "knowledge:publish", "feedback:write"}
    ),
    Role.DATA_ADMIN: frozenset(
        {"data:import", "data:manage", "knowledge:read", "knowledge:propose", "audit:read"}
    ),
    Role.SYSTEM_ADMIN: frozenset({"*"}),
    Role.EXTERNAL_SYSTEM: frozenset({"qa:ask", "knowledge:read", "feedback:write"}),
}


@dataclass(frozen=True, slots=True)
class AccessContext:
    user_id: str
    role: Role
    security_level: SecurityLevel = SecurityLevel.INTERNAL
    organization: str | None = None

    def can(self, permission: str) -> bool:
        permissions = ROLE_PERMISSIONS[self.role]
        return "*" in permissions or permission in permissions

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise PermissionDeniedError(f"角色 {self.role.value} 缺少权限: {permission}")

    def can_access(self, resource_level: SecurityLevel | str) -> bool:
        # ORM String columns return strings, not enum instances. Unknown legacy
        # values must deny access rather than crash or default to public.
        try:
            level = SecurityLevel(resource_level)
        except (ValueError, TypeError):
            return False
        return SECURITY_RANK[self.security_level] >= SECURITY_RANK[level]

    def require_access(self, resource_level: SecurityLevel | str) -> None:
        if not self.can_access(resource_level):
            raise PermissionDeniedError(
                f"用户密级 {self.security_level.value} 不足以访问该数据"
            )

