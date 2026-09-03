from __future__ import annotations

import base64

from app.render_minutes import _tighten_spaces, _to_halfwidth_number


def test_to_halfwidth_number_converts_fullwidth() -> None:
    assert _to_halfwidth_number("資料１・２") == "資料1・2"


def test_tighten_spaces_joins_dates() -> None:
    assert _tighten_spaces("令和 8 年 7 月 10 日") == "令和8年7月10日"


def test_minutes_docx_endpoint_rejects_bad_base64(api_client) -> None:
    response = api_client.post(
        "/v1/render/minutes_docx",
        json={"document": {}, "template_b64": "これはbase64ではない"},
    )

    assert response.status_code == 422


def test_minutes_docx_endpoint_rejects_broken_template(api_client) -> None:
    # docxとして開けないテンプレートは500ではなく422で説明を返す。
    broken = base64.b64encode(b"not a docx file").decode("ascii")
    response = api_client.post(
        "/v1/render/minutes_docx",
        json={"document": {}, "template_b64": broken},
    )

    assert response.status_code == 422
    assert "流し込みに失敗" in response.json()["detail"]
