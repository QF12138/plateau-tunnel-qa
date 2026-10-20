from typing import Any


class AppError(Exception):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        status_code: int = 400,
        field: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.field = field
        self.retryable = retryable
        self.details = details or {}

    def as_dict(self, trace_id: str) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "message": self.message,
            "field": self.field,
            "retryable": self.retryable,
            "trace_id": trace_id,
            "details": self.details,
        }


class NotFoundError(AppError):
    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(
            "RESOURCE_NOT_FOUND",
            f"未找到{resource}: {identifier}",
            status_code=404,
        )


class ConflictError(AppError):
    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__("RESOURCE_CONFLICT", message, status_code=409, field=field)


class PermissionDeniedError(AppError):
    def __init__(self, message: str = "无权访问该资源") -> None:
        super().__init__("PERMISSION_DENIED", message, status_code=403)

