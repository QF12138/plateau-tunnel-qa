import json

from fastapi.testclient import TestClient

from tunnel_qa.app import create_app
from tunnel_qa.core.enums import ProcessingStatus
from tunnel_qa.data.models import SourceDocument
from tunnel_qa.qa.model_gateway import ModelOutput


class ReportFakeGateway:
    provider = "report-test"
    model = "report-test-v1"

    def generate(self, *, question, evidence, scenario, response_mode="detailed"):
        return ModelOutput(answer="应持续监测围岩变化，并依据已发布资料组织复核。", follow_up=[])


def report_headers(user="report-user"):
    return {
        "X-User-Id": user,
        "X-Role": "field_engineer",
        "X-Security-Level": "internal",
    }


def seed_report_answer(database, settings):
    app = create_app(settings, database, ReportFakeGateway())
    client = TestClient(app)
    admin_headers = {
        "X-User-Id": "admin",
        "X-Role": "system_admin",
        "X-Security-Level": "confidential",
    }
    source = client.post(
        "/api/v1/data/sources",
        headers=admin_headers,
        json={
            "name": "报告测试资料源",
            "source_type": "standard",
            "owner": "资料管理员",
            "security_level": "internal",
        },
    )
    metadata = {
        "source_id": "SRC-REPORT-001",
        "title": "围岩监测与复核技术要求",
        "source_type": "standard",
        "project_site": "报告测试隧道",
        "version": "V2.1",
        "effective_date": "2026-06-01",
        "security_level": "internal",
        "owner_org": "测试单位",
        "content_location": "第4章/第12页",
    }
    imported = client.post(
        "/api/v1/data/import",
        headers={**admin_headers, "Idempotency-Key": "report-import-1"},
        data={"data_source_id": source.json()["id"], "metadata_json": json.dumps(metadata)},
        files={
            "file": (
                "report-source.txt",
                "围岩变化应持续监测，异常情况下应组织现场复核并记录处置依据。".encode(),
                "text/plain",
            )
        },
    )
    session = app.state.database.session_factory()
    document = session.get(SourceDocument, imported.json()["document_id"])
    document.status = ProcessingStatus.PUBLISHED
    session.commit()
    session.close()
    question = "围岩发生异常变化后应该如何处理？"
    answer = client.post(
        "/api/v1/qa/ask",
        headers=report_headers(),
        json={"question": question, "scenario": "risk_analysis"},
    )
    assert answer.status_code == 200
    return client, question, answer.json()


def test_markdown_report_contains_original_question_answer_and_sources(database, settings):
    client, question, answer = seed_report_answer(database, settings)

    response = client.get(
        f"/api/v1/qa/reports/{answer['message_id']}?format=markdown",
        headers=report_headers(),
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    assert question in response.text
    assert answer["answer"] in response.text
    assert "SRC-REPORT-001" in response.text
    assert "第4章/第12页" in response.text
    assert answer["trace_id"] in response.text


def test_html_report_is_print_ready_and_escapes_content(database, settings):
    client, question, answer = seed_report_answer(database, settings)

    response = client.get(
        f"/api/v1/qa/reports/{answer['message_id']}?format=html",
        headers=report_headers(),
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert "@page" in response.text
    assert question in response.text
    assert "围岩监测与复核技术要求" in response.text
    assert "本报告由系统根据已保存的问答与引用自动生成" in response.text


def test_report_cannot_be_exported_by_another_user(database, settings):
    client, _, answer = seed_report_answer(database, settings)

    response = client.get(
        f"/api/v1/qa/reports/{answer['message_id']}?format=markdown",
        headers=report_headers("other-user"),
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "PERMISSION_DENIED"
