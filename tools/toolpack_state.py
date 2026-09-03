r"""追加ツールが「いま実際にどうなっているか」を3か所へ問い合わせる。

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-10・D-11)。

見るのは **稼働中のハブ(8010)** と **Open WebUI** の2つ(残る1つの台帳は
`toolpack_store` が持つ)。**同じプロセスでコードを読み直しても、
別プロセスで動いている本番のハブが見えている証拠にはならない。**
だから実際にHTTPで問い合わせる。

**確かめられなかったことを「無い」と混ぜない。** どの関数も、
問い合わせられなかったときは `None` を返す。
「見に行けなかった」と「見に行ったが無かった」は別物であり、
混ぜると、実際には動いているものを消したり、
消えていないものを消えたと報告したりする。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HUB_STATUS_URL = "http://127.0.0.1:8010/v1/status"
TIMEOUT_SECONDS = 10


def hub_status(timeout: float = TIMEOUT_SECONDS) -> dict | None:
    """稼働中のハブの状態。問い合わせられなければ None。"""
    try:
        with urllib.request.urlopen(HUB_STATUS_URL, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def hub_tool_names(timeout: float = TIMEOUT_SECONDS) -> set[str] | None:
    """稼働中のハブが配っているツール名。問い合わせられなければ None。"""
    status = hub_status(timeout)
    if status is None:
        return None
    return {str(tool.get("name")) for tool in status.get("tools", []) if tool.get("name")}


def hub_load_errors(timeout: float = TIMEOUT_SECONDS) -> list[str]:
    """ハブが読み込めなかった接続役の理由(空なら問題なし・未確認も空)。"""
    status = hub_status(timeout)
    return [str(item) for item in (status or {}).get("errors") or []]


def hub_recognizes(tool_id: str, timeout: float = TIMEOUT_SECONDS) -> bool | None:
    names = hub_tool_names(timeout)
    return None if names is None else tool_id in names


def openwebui_ids() -> set[str] | None:
    """Open WebUI に登録されている Pipe の id。問い合わせられなければ None。"""
    models = openwebui_models()
    return None if models is None else set(models)


def openwebui_models() -> dict[str, str] | None:
    """Open WebUI に登録されているモデルの {id: 表示名}。問い合わせられなければ None。

    **これが「モデル」の正本である。** リポジトリにファイルがあっても、
    登録されていなければ利用者からは選べない。
    """
    try:
        import open_webui_deploy as deploy

        client = deploy.ApiClient(
            base_url=deploy.resolve_base_url(), api_key=deploy.load_api_key()
        )
        result: dict[str, str] = {}
        for entry in client.list_entries("pipe"):
            model_id = str(entry["id"])
            model = client.get_model(model_id)
            result[model_id] = str(
                (model or {}).get("name") or entry.get("name") or model_id
            )
        return result
    except Exception:
        return None


def openwebui_has(tool_id: str) -> bool | None:
    """Open WebUI に登録があるか。**Function とモデル設定の両方**を見る。

    Function だけ消えてモデル設定が残っている状態があり、
    片方だけ見ると「消えた」と誤って報告する。
    """
    try:
        import open_webui_deploy as deploy

        client = deploy.ApiClient(
            base_url=deploy.resolve_base_url(), api_key=deploy.load_api_key()
        )
        if any(entry.get("id") == tool_id for entry in client.list_entries("pipe")):
            return True
        return client.get_model(tool_id) is not None
    except Exception:
        return None


def describe(label: str, value: bool | None, expected: bool) -> str:
    """確認結果を利用者向けの1行にする。未確認を「合っている」と書かない。"""
    if value is None:
        return f"{label}: 確認できませんでした"
    if value == expected:
        return f"{label}: {'あり' if value else 'なし'}(想定どおり)"
    return f"{label}: {'あり' if value else 'なし'}(**想定と違います**)"
