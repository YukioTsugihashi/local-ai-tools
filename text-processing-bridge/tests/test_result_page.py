from __future__ import annotations

from app.result_page import build_result_page


def _page(**overrides) -> str:
    params = dict(
        operation_label="要約",
        result="結果本文",
        source_chars=100,
        result_chars=50,
        source_type="file:sample.txt",
        workflow_run_id="run-1",
        warnings=[],
    )
    params.update(overrides)
    return build_result_page(**params)


def test_result_is_html_escaped() -> None:
    # 結果本文にタグが含まれてもスクリプトとして動かないこと。
    page = _page(result="<script>alert(1)</script>")

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_crlf_note_only_when_counts_differ() -> None:
    same = _page(result_chars=50, result_chars_crlf=50)
    differ = _page(result_chars=50, result_chars_crlf=52)

    assert "CRLF換算" not in same
    assert "CRLF換算 52文字" in differ


def test_warnings_and_attachment_notes() -> None:
    page = _page(warnings=["数値が欠落"], attached_filename="要約.txt")

    assert "数値が欠落" in page
    assert "要約.txt" in page


def test_result_page_endpoint_returns_html(api_client) -> None:
    response = api_client.post(
        "/v1/render/result_page",
        json={
            "operation_label": "要約",
            "result": "結果本文",
            "source_chars": 100,
            "result_chars": 50,
            "source_type": "latest_user_message",
            "workflow_run_id": "",
            "warnings": [],
        },
    )

    assert response.status_code == 200
    html = response.json()["html"]
    assert html.startswith("<!DOCTYPE html>")
    assert "結果本文" in html
