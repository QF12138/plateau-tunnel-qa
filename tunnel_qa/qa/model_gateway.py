import json
from dataclasses import dataclass
from typing import Protocol

import httpx

from tunnel_qa.core.config import Settings
from tunnel_qa.core.errors import AppError


@dataclass(slots=True)
class ModelOutput:
    answer: str
    follow_up: list[str]


class ModelGateway(Protocol):
    provider: str
    model: str

    def generate(self, *, question: str, evidence: list[dict], scenario: str | None, response_mode: str = "detailed") -> ModelOutput: ...


def response_instructions(response_mode: str) -> str:
    if response_mode == "brief":
        return "简要回答：结论优先，通常不超过200字；只列关键依据。不得省略证据冲突、关键适用条件和安全复核提示，必要时可超出字数。"
    return "详细回答：按结论、证据依据、适用条件与限制分段说明，必要时列出操作要点；保留证据冲突和安全复核提示，避免重复堆砌。"


SYSTEM_PROMPT = """你是高原铁路隧道科研与施工知识助手。必须严格遵守：
1. 只能使用给定证据回答，不得使用未提供的事实或虚构引用；
2. 先给结论，再说明依据、适用条件与限制；
3. 证据冲突时并列说明，不自行裁决；
4. 施工安全、风险和应急内容仅为辅助建议，必须提示专家复核；
5. 输出 JSON：{\"answer\": \"...\", \"follow_up\": [\"...\"]}。
"""


class DeepSeekGateway:
    provider = "deepseek"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if settings.deepseek_api_key is None:
            raise AppError("MODEL_NOT_CONFIGURED", "系统环境未配置 DEEPSEEK_API_KEY")
        self.model = settings.deepseek_model
        self.api_key = settings.deepseek_api_key.get_secret_value()
        self.base_url = settings.deepseek_base_url.rstrip("/")
        self.temperature = settings.llm_temperature
        self.client = client or httpx.Client(timeout=settings.llm_timeout_seconds)

    def generate(
        self, *, question: str, evidence: list[dict], scenario: str | None, response_mode: str = "detailed"
    ) -> ModelOutput:
        evidence_text = "\n\n".join(
            f"[证据{index}] 来源={item['title']} 版本={item['version']} "
            f"位置={item['content_location']}\n{item['excerpt']}"
            for index, item in enumerate(evidence, 1)
        )
        user_prompt = f"回答要求：{response_instructions(response_mode)}\n场景：{scenario or '一般问答'}\n问题：{question}\n\n{evidence_text}"
        try:
            response = self.client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": self.temperature,
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise AppError(
                "MODEL_CALL_FAILED",
                "DeepSeek 模型调用失败",
                status_code=502,
                retryable=True,
                details={"error_type": type(exc).__name__},
            ) from exc
        return parse_model_output(content)


class OpenAICompatibleGateway(DeepSeekGateway):
    """Gateway for services exposing the OpenAI-compatible chat endpoint."""

    provider = "openai_compatible"


class OllamaGateway:
    provider = "ollama"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.model = settings.local_model
        self.base_url = settings.local_base_url.rstrip("/")
        self.temperature = settings.llm_temperature
        self.client = client or httpx.Client(timeout=settings.llm_timeout_seconds)

    def generate(
        self, *, question: str, evidence: list[dict], scenario: str | None, response_mode: str = "detailed"
    ) -> ModelOutput:
        evidence_text = "\n\n".join(
            f"[证据{index}] 来源={item['title']} 版本={item['version']} "
            f"位置={item['content_location']}\n{item['excerpt']}"
            for index, item in enumerate(evidence, 1)
        )
        user_prompt = f"回答要求：{response_instructions(response_mode)}\n场景：{scenario or '一般问答'}\n问题：{question}\n\n{evidence_text}"
        try:
            response = self.client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": self.temperature},
                },
            )
            response.raise_for_status()
            content = response.json()["message"]["content"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise AppError(
                "MODEL_CALL_FAILED",
                "本地 Ollama 模型调用失败",
                status_code=502,
                retryable=True,
                details={"error_type": type(exc).__name__},
            ) from exc
        return parse_model_output(content)


class ExtractiveFallbackGateway:
    provider = "local_fallback"
    model = "extractive-v1"

    def generate(
        self, *, question: str, evidence: list[dict], scenario: str | None, response_mode: str = "detailed"
    ) -> ModelOutput:
        excerpts = [item["excerpt"].strip() for item in evidence[:3] if item["excerpt"].strip()]
        if response_mode == "brief":
            # Do not cut a safety condition/negation midway through its sentence.
            answer = "关键证据摘录（非完整结论）：" + (excerpts[0] if excerpts else "暂无可用片段")
            answer += "。其他适用条件及可能冲突请结合全部来源核对。"
        else:
            answer = "根据已发布资料，" + "；".join(excerpts)
        if scenario in {"risk_analysis", "emergency_support"}:
            answer += "。以上仅为辅助建议，需由现场负责人和领域专家复核。"
        return ModelOutput(answer=answer, follow_up=[])


def parse_model_output(content: str) -> ModelOutput:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        if not content.strip():
            raise AppError("INVALID_MODEL_OUTPUT", "模型返回空内容", status_code=502)
        return ModelOutput(answer=content.strip(), follow_up=[])
    answer = data.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise AppError("INVALID_MODEL_OUTPUT", "模型响应缺少 answer", status_code=502)
    follow_up = data.get("follow_up", [])
    if not isinstance(follow_up, list):
        follow_up = []
    return ModelOutput(
        answer=answer.strip(),
        follow_up=[str(item) for item in follow_up[:5]],
    )


def build_model_gateway(settings: Settings) -> ModelGateway:
    mode = settings.model_mode
    if mode == "auto":
        mode = "api" if settings.deepseek_api_key is not None else "local"
    if mode == "api" and settings.deepseek_api_key is not None:
        if settings.api_provider == "openai_compatible":
            return OpenAICompatibleGateway(settings)
        return DeepSeekGateway(settings)
    if mode == "local" and settings.local_provider == "ollama":
        return OllamaGateway(settings)
    return ExtractiveFallbackGateway()
