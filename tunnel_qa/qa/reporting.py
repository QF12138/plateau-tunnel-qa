from dataclasses import dataclass
from html import escape
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from tunnel_qa.core.errors import AppError, NotFoundError, PermissionDeniedError
from tunnel_qa.core.security import AccessContext
from tunnel_qa.qa.models import Message, QATrace


ReportFormat = Literal["markdown", "html"]


@dataclass(frozen=True, slots=True)
class QAReportFile:
    content: str
    media_type: str
    extension: str
    trace_id: str


class QAReportService:
    """Rebuild a report from persisted messages instead of trusting browser payloads."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def generate(
        self, message_id: str, report_format: ReportFormat, access: AccessContext
    ) -> QAReportFile:
        access.require("qa:ask")
        message = self.session.get(Message, message_id)
        if message is None or message.role != "assistant":
            raise NotFoundError("问答回答", message_id)
        if (
            message.conversation.owner_user_id != access.user_id
            and access.role.value != "system_admin"
        ):
            raise PermissionDeniedError("不能导出其他用户的问答报告")
        trace = self.session.scalar(
            select(QATrace).where(QATrace.trace_id == message.trace_id)
        )
        question = trace.question if trace else None
        if question is None:
            question_message = self.session.scalar(
                select(Message)
                .where(
                    Message.conversation_id == message.conversation_id,
                    Message.role == "user",
                )
                .order_by(Message.created_at.desc(), Message.sequence.desc())
                .limit(1)
            )
            question = question_message.content if question_message else None
        if question is None:
            raise AppError("REPORT_QUESTION_MISSING", "回答缺少对应的原始问题")
        if report_format == "markdown":
            return QAReportFile(
                content=self._markdown(question, message, trace),
                media_type="text/markdown; charset=utf-8",
                extension="md",
                trace_id=message.trace_id,
            )
        if report_format == "html":
            return QAReportFile(
                content=self._html(question, message, trace),
                media_type="text/html; charset=utf-8",
                extension="html",
                trace_id=message.trace_id,
            )
        raise AppError("INVALID_REPORT_FORMAT", "报告格式仅支持 markdown 或 html")

    @staticmethod
    def _markdown(question: str, message: Message, trace: QATrace | None) -> str:
        citations = message.citations or []
        lines = [
            "# 高原铁路隧道科研—施工智能问答报告",
            "",
            "## 原始问题",
            "",
            _markdown_text(question),
            "",
            "## 回答",
            "",
            _markdown_text(message.content),
            "",
            "## 来源",
            "",
        ]
        if citations:
            for index, citation in enumerate(citations, start=1):
                lines.extend(
                    [
                        f"### {index}. {_markdown_text(citation.get('title', '未命名来源'))}",
                        "",
                        f"- 来源编号：{_markdown_text(citation.get('source_id', '—'))}",
                        f"- 版本：{_markdown_text(citation.get('version', '—'))}",
                        f"- 生效日期：{_markdown_text(citation.get('effective_date', '—'))}",
                        f"- 内容位置：{_markdown_text(citation.get('content_location', '—'))}",
                        "- 证据摘录：",
                        "",
                        _markdown_quote(citation.get("excerpt", "")),
                        "",
                    ]
                )
        else:
            lines.extend(["本次回答没有可导出的来源。", ""])
        if message.warnings:
            lines.extend(["## 适用限制", ""])
            lines.extend(f"- {_markdown_text(item)}" for item in message.warnings)
            lines.append("")
        lines.extend(
            [
                "## 追踪信息",
                "",
                f"- 追踪号：{_markdown_text(message.trace_id)}",
                f"- 模型：{_markdown_text(trace.model_provider if trace else '—')} / {_markdown_text(trace.model_version if trace else '—')}",
                f"- 知识版本：{trace.knowledge_version if trace else 0}",
                f"- 生成时间：{message.created_at.isoformat()}",
                "",
                "> 本报告由系统根据已保存的问答与引用自动生成，仅用于科研和施工辅助分析。",
                "",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _html(question: str, message: Message, trace: QATrace | None) -> str:
        citations = message.citations or []
        sources_html = "".join(
            f"""
            <article class="source">
              <h3>{index}. {escape(str(item.get('title', '未命名来源')))}</h3>
              <dl>
                <dt>来源编号</dt><dd>{escape(str(item.get('source_id', '—')))}</dd>
                <dt>版本</dt><dd>{escape(str(item.get('version', '—')))}</dd>
                <dt>生效日期</dt><dd>{escape(str(item.get('effective_date', '—')))}</dd>
                <dt>内容位置</dt><dd>{escape(str(item.get('content_location', '—')))}</dd>
              </dl>
              <blockquote>{_html_text(item.get('excerpt', ''))}</blockquote>
            </article>"""
            for index, item in enumerate(citations, start=1)
        ) or '<p class="empty">本次回答没有可导出的来源。</p>'
        warnings_html = "".join(
            f"<li>{escape(str(item))}</li>" for item in (message.warnings or [])
        )
        warnings_section = (
            f"<section><h2>适用限制</h2><ul>{warnings_html}</ul></section>"
            if warnings_html
            else ""
        )
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>问答报告 · {escape(message.trace_id)}</title>
<style>
@page{{size:A4;margin:20mm}}*{{box-sizing:border-box}}body{{margin:0 auto;max-width:900px;color:#152522;background:#eef2f0;font:15px/1.7 "Microsoft YaHei",sans-serif}}main{{background:#fff;padding:42px 48px;border-top:8px solid #0c1918}}header{{border-bottom:2px solid #ff6b2c;padding-bottom:18px;margin-bottom:30px}}.eyebrow{{font:12px "Cascadia Mono",monospace;color:#9a451f;letter-spacing:.12em}}h1{{font:800 30px/1.2 Bahnschrift,"Microsoft YaHei",sans-serif;margin:8px 0}}h2{{font-size:20px;margin-top:30px}}h3{{font-size:16px;margin-bottom:8px}}.answer,.question{{white-space:pre-wrap}}.source{{border-top:1px solid #bdc9c5;padding:16px 0}}dl{{display:grid;grid-template-columns:90px 1fr;margin:0}}dt{{color:#65756f}}dd{{margin:0}}blockquote{{margin:12px 0;padding:10px 14px;border-left:4px solid #70d7d0;background:#edf5f2;white-space:pre-wrap}}.trace{{font:12px/1.7 "Cascadia Mono",monospace;background:#0c1918;color:#d7ebe5;padding:16px}}footer{{margin-top:32px;color:#65756f;font-size:12px}}@media print{{body{{background:#fff}}main{{padding:0;border-top:0}}}}
</style></head><body><main><header><div class="eyebrow">PLATEAU TUNNEL / EVIDENCE REPORT</div><h1>高原铁路隧道科研—施工智能问答报告</h1></header>
<section><h2>原始问题</h2><div class="question">{_html_text(question)}</div></section>
<section><h2>回答</h2><div class="answer">{_html_text(message.content)}</div></section>
<section><h2>来源</h2>{sources_html}</section>{warnings_section}
<section><h2>追踪信息</h2><div class="trace">追踪号：{escape(message.trace_id)}<br>模型：{escape(trace.model_provider if trace else '—')} / {escape(trace.model_version if trace else '—')}<br>知识版本：{trace.knowledge_version if trace else 0}<br>生成时间：{escape(message.created_at.isoformat())}</div></section>
<footer>本报告由系统根据已保存的问答与引用自动生成，仅用于科研和施工辅助分析。</footer></main></body></html>"""


def _markdown_text(value: object) -> str:
    return str(value or "").replace("<", "&lt;").replace(">", "&gt;")


def _markdown_quote(value: object) -> str:
    text = _markdown_text(value)
    return "\n".join(f"> {line}" for line in text.splitlines()) or "> —"


def _html_text(value: object) -> str:
    return escape(str(value or "")).replace("\n", "<br>")
