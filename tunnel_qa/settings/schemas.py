from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


class SystemSettingsUpdate(BaseModel):
    model_mode: Literal["local", "api"]
    api_provider: Literal["deepseek", "openai_compatible"] = "deepseek"
    api_base_url: str = Field(min_length=8, max_length=500)
    api_model: str = Field(min_length=1, max_length=120)
    api_key: SecretStr | None = None
    clear_api_key: bool = False
    local_provider: Literal["builtin", "ollama"] = "builtin"
    local_base_url: str = Field(min_length=8, max_length=500)
    local_model: str = Field(min_length=1, max_length=120)
    llm_temperature: float = Field(ge=0, le=1)
    llm_timeout_seconds: float = Field(ge=1, le=300)
    retrieval_top_k: int = Field(ge=1, le=100)
    minimum_evidence_score: float = Field(ge=0, le=1)
    default_response_mode: Literal["detailed", "brief"] = "detailed"
    auto_report: bool = False
    report_format: Literal["markdown", "html"] = "markdown"
    theme: Literal["light", "dark", "system"] = "light"
    density: Literal["comfortable", "compact"] = "comfortable"
    audit_enabled: Literal[True] = True
    safety_review_enabled: Literal[True] = True

    @field_validator("api_base_url", "local_base_url")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("服务地址必须以 http:// 或 https:// 开头")
        return cleaned

    @field_validator("api_model", "local_model")
    @classmethod
    def clean_model_name(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_clear_key(self):
        if self.clear_api_key and self.api_key is not None:
            raise ValueError("清除 API Key 与输入新 Key 不能同时选择")
        return self


class SystemSettingsRead(BaseModel):
    model_mode: str
    active_provider: str
    active_model: str
    api_provider: str
    api_base_url: str
    api_model: str
    api_key_configured: bool
    api_key_masked: str | None
    local_provider: str
    local_base_url: str
    local_model: str
    llm_temperature: float
    llm_timeout_seconds: float
    retrieval_top_k: int
    minimum_evidence_score: float
    default_response_mode: str
    auto_report: bool
    report_format: str
    theme: str
    density: str
    audit_enabled: bool
    safety_review_enabled: bool
    updated_by: str
    updated_at: str
