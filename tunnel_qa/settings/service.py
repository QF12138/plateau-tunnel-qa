from typing import Literal

from pydantic import SecretStr, TypeAdapter
from sqlalchemy.orm import Session

from tunnel_qa.core.config import Settings
from tunnel_qa.core.errors import AppError
from tunnel_qa.core.security import AccessContext
from tunnel_qa.core.secret_protection import PREFIX, protect_secret, reveal_secret
from tunnel_qa.qa.model_gateway import ModelGateway
from tunnel_qa.settings.models import SystemSetting
from tunnel_qa.settings.schemas import SystemSettingsRead, SystemSettingsUpdate


class SystemSettingsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def initialize(self, runtime: Settings) -> SystemSetting:
        record = self.session.get(SystemSetting, "system")
        if record is None:
            mode = runtime.model_mode if runtime.model_mode != "auto" else (
                "api" if runtime.deepseek_api_key is not None else "local"
            )
            record = SystemSetting(
                id="system",
                model_mode=mode,
                api_provider=runtime.api_provider,
                api_base_url=runtime.deepseek_base_url,
                api_model=runtime.deepseek_model,
                local_provider=runtime.local_provider,
                local_base_url=runtime.local_base_url,
                local_model=runtime.local_model,
                llm_temperature=runtime.llm_temperature,
                llm_timeout_seconds=runtime.llm_timeout_seconds,
                retrieval_top_k=runtime.retrieval_top_k,
                minimum_evidence_score=runtime.minimum_evidence_score,
                default_response_mode=runtime.default_response_mode,
            )
            self.session.add(record)
            self.session.commit()
            self.session.refresh(record)
        if record.api_key_secret and not record.api_key_secret.startswith(PREFIX):
            record.api_key_secret = protect_secret(record.api_key_secret, runtime)
            self.session.commit()
        self.apply_to_runtime(record, runtime)
        return record

    def get(
        self, access: AccessContext, runtime: Settings, gateway: ModelGateway
    ) -> SystemSettingsRead:
        access.require("settings:read")
        record = self.initialize(runtime)
        return self._read(record, runtime, gateway)

    def update(
        self,
        payload: SystemSettingsUpdate,
        access: AccessContext,
        runtime: Settings,
    ) -> SystemSetting:
        access.require("settings:write")
        record = self.initialize(runtime)
        supplied_key = payload.api_key.get_secret_value().strip() if payload.api_key else None
        previous_key = reveal_secret(record.api_key_secret, runtime) if record.api_key_secret else None
        effective_key = None if payload.clear_api_key else (supplied_key or previous_key)
        if effective_key is None and not payload.clear_api_key and runtime.deepseek_api_key is not None:
            effective_key = runtime.deepseek_api_key.get_secret_value()
        if payload.model_mode == "api" and not effective_key:
            raise AppError(
                "MODEL_API_KEY_REQUIRED",
                "选择 API 模型时必须配置 API Key",
                field="api_key",
            )

        stored_key = None if payload.clear_api_key else (
            protect_secret(supplied_key, runtime) if supplied_key else record.api_key_secret
        )
        values = payload.model_dump(exclude={"api_key", "clear_api_key"})
        for name, value in values.items():
            setattr(record, name, value)
        record.api_key_secret = stored_key
        record.updated_by = access.user_id
        self.session.commit()
        self.session.refresh(record)
        if payload.clear_api_key:
            runtime.deepseek_api_key = None
        self.apply_to_runtime(record, runtime)
        return record

    @staticmethod
    def apply_to_runtime(record: SystemSetting, runtime: Settings) -> None:
        runtime.model_mode = TypeAdapter(Literal["auto", "local", "api"]).validate_python(
            record.model_mode
        )
        runtime.api_provider = TypeAdapter(
            Literal["deepseek", "openai_compatible"]
        ).validate_python(record.api_provider)
        runtime.deepseek_base_url = record.api_base_url
        runtime.deepseek_model = record.api_model
        if record.api_key_secret:
            runtime.deepseek_api_key = SecretStr(reveal_secret(record.api_key_secret, runtime))
        # If the database has no secret, keep an environment-provided key in memory.
        runtime.local_provider = TypeAdapter(Literal["builtin", "ollama"]).validate_python(
            record.local_provider
        )
        runtime.local_base_url = record.local_base_url
        runtime.local_model = record.local_model
        runtime.llm_temperature = record.llm_temperature
        runtime.llm_timeout_seconds = record.llm_timeout_seconds
        runtime.retrieval_top_k = record.retrieval_top_k
        runtime.minimum_evidence_score = record.minimum_evidence_score
        runtime.default_response_mode = TypeAdapter(Literal["detailed", "brief"]).validate_python(record.default_response_mode)

    @staticmethod
    def _read(
        record: SystemSetting, runtime: Settings, gateway: ModelGateway
    ) -> SystemSettingsRead:
        key = reveal_secret(record.api_key_secret, runtime) if record.api_key_secret else None
        if key is None and runtime.deepseek_api_key is not None:
            key = runtime.deepseek_api_key.get_secret_value()
        masked = None if not key else f"••••••••{key[-4:]}"
        return SystemSettingsRead(
            model_mode=record.model_mode,
            active_provider=gateway.provider,
            active_model=gateway.model,
            api_provider=record.api_provider,
            api_base_url=record.api_base_url,
            api_model=record.api_model,
            api_key_configured=bool(key),
            api_key_masked=masked,
            local_provider=record.local_provider,
            local_base_url=record.local_base_url,
            local_model=record.local_model,
            llm_temperature=record.llm_temperature,
            llm_timeout_seconds=record.llm_timeout_seconds,
            retrieval_top_k=record.retrieval_top_k,
            minimum_evidence_score=record.minimum_evidence_score,
            default_response_mode=record.default_response_mode,
            auto_report=record.auto_report,
            report_format=record.report_format,
            theme=record.theme,
            density=record.density,
            audit_enabled=True,
            safety_review_enabled=True,
            updated_by=record.updated_by,
            updated_at=record.updated_at.isoformat(),
        )
