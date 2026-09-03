"""Open WebUI への Pipe / Tool の登録・更新(Kilo用の安全なデプロイ経路)。

管理画面への貼り付け作業を、正式な管理API経由で自動化する。
Kiloが扱えるのはこのスクリプトの引数だけで、接続先・対象ファイル・
APIの種類はスクリプト側で固定している。

使い方:
  python tools\\open_webui_deploy.py --list
      登録済みの Pipe / Tool の一覧を表示(読み取りのみ)
  python tools\\open_webui_deploy.py tools\\open_webui_excel_analysis_pipe.py
      実行計画とサーバー版との差分を表示(dry-run。何も変更しない)
  python tools\\open_webui_deploy.py tools\\open_webui_excel_analysis_pipe.py --apply
      登録または更新を実行し、直後にサーバーから取得し直して検証する

前提(1回だけ人の作業):
  Open WebUI に管理者でログイン → 設定 → アカウント → APIキー を作成し、
  次のどちらかへ保存する(Gitや .env には置かない)。
    1. 環境変数 OPEN_WEBUI_ADMIN_API_KEY
    2. ファイル %USERPROFILE%\\.open-webui\\admin_api_key.txt

安全のための固定事項:
  - 接続先は localhost の Open WebUI のみ(それ以外のホストは拒否)
  - 対象は tools/ 直下の open*webui*.py のみ
  - 登録前に構文チェックと class Pipe / class Tools の判定を行う
  - APIキーは表示・記録しない

モデル画面の提案チップ(Pipeでは必須):
  ファイル先頭docstringに次の形式で書くと、デプロイ時にモデル設定へ登録される。
    model_description: モデル選択時に出る説明文
    suggestion: タイトル | サブタイトル | 入力欄に入る文
  Pipeは model_description と suggestion(1つ以上) が無いと登録を拒否する
  (モデルとして画面に出る以上、何ができるかの提示を必須とする運用ルール)。
  Toolはモデルとして画面に出ないため対象外。
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
KEY_ENV = "OPEN_WEBUI_ADMIN_API_KEY"
KEY_FILE_ENV = "OPEN_WEBUI_ADMIN_API_KEY_FILE"
DEFAULT_KEY_FILE = Path.home() / ".open-webui" / "admin_api_key.txt"
URL_ENV = "OPEN_WEBUI_URL"
DEFAULT_URL = "http://localhost:3000"
ALLOWED_HOSTS = {"localhost", "127.0.0.1"}
# 対象ファイル名の柵。open_webui_*.py と openwebui_*.py の両方を許す
ALLOWED_NAME = re.compile(r"^open_?webui_.+\.py$")
ID_PATTERN = re.compile(r"^[a-z0-9_]+$")
TIMEOUT_SECONDS = 30
# pythonw(コンソール無し)から子プロセスを普通に起動すると黒い窓が開く。
# hub.py 等と同じ理由・同じ扱いにする
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class DeployError(Exception):
    """利用者に日本語で示すエラー。対処を含めること。"""


# ----------------------------------------------------------------------
# 対象ファイルの検査(ネットワーク不要の純関数群。テスト対象)
# ----------------------------------------------------------------------
def _generated_pipe_dirs(repo_root: Path) -> set[Path]:
    """追加ツールの「有効版の generated/」だけを許可パスとして集める。

    registry に載っていない版や、置いてあるだけのディレクトリは許可しない
    (TOOL_PACKAGE_DECISIONS.md D-4。registry が唯一の有効化スイッチ)。
    追加ツール基盤が未導入の環境では空集合を返し、従来どおり tools/ 限定で動く。
    """
    try:
        sys.path.insert(0, str(repo_root / "tools"))
        import toolpack_store

        store = toolpack_store.ToolpackStore(repo_root / "additional-tools")
        return {
            store.generated_dir(tool_id, info["version"]).resolve()
            for tool_id, info in store.active_tools().items()
        }
    except Exception:
        return set()


def validate_target_path(path_text: str, repo_root: Path = REPO_ROOT) -> Path:
    """対象が tools/ 直下、または追加ツールの有効版 generated/ であることを確かめる。"""
    path = Path(path_text)
    if not path.is_absolute():
        path = (repo_root / path).resolve()
    else:
        path = path.resolve()
    tools_dir = (repo_root / "tools").resolve()
    if path.parent != tools_dir and path.parent not in _generated_pipe_dirs(repo_root):
        raise DeployError(
            f"対象は {tools_dir} 直下、または追加ツールの有効版の generated/ に限られます: {path}\n"
            "対処: Pipe/Tool のファイルは tools/ に置いてください"
            "(追加ツールの生成Pipeは、registry に登録された版の generated/ から登録します)"
        )
    if not ALLOWED_NAME.match(path.name):
        raise DeployError(
            f"対象は open_webui_*.py という名前に限られます: {path.name}\n"
            "対処: ファイル名を open_webui_<機能名>_pipe.py のように変えてください"
        )
    if not path.exists():
        raise DeployError(f"ファイルが見つかりません: {path}")
    return path


def detect_plugin_type(source: str) -> str:
    """ソースのトップレベルクラスから 'pipe' / 'tool' を判定する。構文エラーは例外。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise DeployError(
            f"Pythonの構文エラーがあります(行{error.lineno}): {error.msg}\n"
            "対処: py_compile で構文を確認し、修正してから再実行してください"
        )
    class_names = {
        node.name for node in tree.body if isinstance(node, ast.ClassDef)
    }
    has_pipe = "Pipe" in class_names
    has_tools = "Tools" in class_names
    if has_pipe and has_tools:
        raise DeployError(
            "class Pipe と class Tools の両方があります。どちらか一方にしてください"
        )
    if has_pipe:
        return "pipe"
    if has_tools:
        return "tool"
    # よくある間違い(別名クラス・継承)は名指しで指摘して自己修正させる
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
        if bases & {"Pipe", "Tools"} or "Pipe" in node.name or "Tool" in node.name:
            raise DeployError(
                f"class {node.name} はOpen WebUIに認識されません。"
                "クラスの名前はちょうど Pipe (または Tools) にしてください\n"
                f"対処: `class {node.name}(...)` を `class Pipe:` に改名し、"
                "他のPipeからの継承・importはやめて、必要な処理は"
                "このファイル内に自己完結で書いてください"
            )
    raise DeployError(
        "class Pipe も class Tools もありません。Open WebUIには登録できない形式です\n"
        "対処: Pipeなら tools/open_webui_text_processing_pipe.py、"
        "Toolなら tools/open_webui_dify_text_processing_tool_v0.6.0.py を手本に、"
        "必要なクラス構造を先に作ってください(.kilo/rules/03-newtool.md)"
    )


def validate_container_imports(source: str) -> None:
    """Open WebUIコンテナ内に存在しないリポジトリ内モジュールのimportを拒否する。

    Pipe/ToolはOpen WebUIコンテナで実行されるため、`tools.〇〇` や他の
    open_webui_*.py ファイルはimportできない(登録後に読み込みで即クラッシュする)。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        else:
            continue
        for name in names:
            root = name.split(".")[0]
            if root == "tools" or root.startswith("open_webui_") or root.startswith("openwebui_"):
                raise DeployError(
                    f"`{name}` のimportはできません。Pipe/ToolはOpen WebUI"
                    "コンテナ内で動くため、このリポジトリの他ファイルは見えません\n"
                    "対処: importと継承をやめて、必要な処理はこのファイル内に"
                    "自己完結で書いてください(手本のPipe/Toolもすべて自己完結)"
                )


def parse_frontmatter(source: str) -> dict[str, str]:
    """先頭docstringの `キー: 値` 行(title/id/description等)を取り出す。"""
    fields: dict[str, str] = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return fields
    doc = ast.get_docstring(tree) or ""
    for line in doc.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)$", line.strip())
        if match:
            fields[match.group(1).lower()] = match.group(2).strip()
    return fields


def parse_suggestions(source: str) -> list[dict]:
    """先頭docstringの `suggestion: タイトル | サブタイトル | 入力文` 行を集める。

    Open WebUIのモデル設定 suggestion_prompts の形式
    ({"title": [タイトル, サブタイトル], "content": 入力文}) に変換する。
    """
    suggestions: list[dict] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return suggestions
    doc = ast.get_docstring(tree) or ""
    for line in doc.splitlines():
        match = re.match(r"^suggestion\s*:\s*(.+)$", line.strip(), re.IGNORECASE)
        if not match:
            continue
        parts = [p.strip() for p in match.group(1).split("|")]
        if len(parts) >= 3:
            suggestions.append(
                {"title": [parts[0], parts[1]], "content": parts[2]}
            )
        elif len(parts) == 2:
            suggestions.append({"title": [parts[0], ""], "content": parts[1]})
        elif parts[0]:
            suggestions.append({"title": [parts[0], ""], "content": parts[0]})
    return suggestions


def extract_operation_patterns(source: str) -> list[tuple[str, str]]:
    """パイプ規約の OPERATION_PATTERNS = [(操作名, re.compile(r"...")), ...] を
    ASTから静的に取り出す。規約に従わないファイルでは空を返す(照合はスキップ)。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "OPERATION_PATTERNS"
            for t in node.targets
        ):
            continue
        patterns: list[tuple[str, str]] = []
        if isinstance(node.value, ast.List):
            for element in node.value.elts:
                if not (isinstance(element, ast.Tuple) and len(element.elts) == 2):
                    continue
                name_node, compile_node = element.elts
                if not (
                    isinstance(name_node, ast.Constant)
                    and isinstance(name_node.value, str)
                ):
                    continue
                if (
                    isinstance(compile_node, ast.Call)
                    and compile_node.args
                    and isinstance(compile_node.args[0], ast.Constant)
                    and isinstance(compile_node.args[0].value, str)
                ):
                    patterns.append((name_node.value, compile_node.args[0].value))
        return patterns
    return []


def check_suggestion_routing(
    source: str, suggestions: list[dict]
) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """各チップの入力文がPipe内のどの操作へ振り分けられるかを照合する。

    返り値: ([(チップ名, 一致した操作名のリスト), ...], チップの無い操作名のリスト)。
    OPERATION_PATTERNSが見つからないファイルでは ([], []) で照合なし。
    """
    patterns = extract_operation_patterns(source)
    if not patterns:
        return [], []
    compiled = [(name, re.compile(pattern)) for name, pattern in patterns]
    mapping: list[tuple[str, list[str]]] = []
    covered: set[str] = set()
    for item in suggestions:
        matched = [name for name, regex in compiled if regex.search(item["content"])]
        covered.update(matched)
        mapping.append((item["title"][0], matched))
    uncovered = [name for name, _ in patterns if name not in covered]
    return mapping, uncovered


def validate_pipe_model_requirements(
    plugin_type: str, fields: dict[str, str], suggestions: list[dict]
) -> None:
    """Pipeに提案チップと説明文を強制する(モデル画面に何ができるか必ず出す)。"""
    if plugin_type != "pipe":
        return
    missing = []
    if not (fields.get("model_description") or "").strip():
        missing.append("model_description: モデル選択時に出る説明文")
    if not suggestions:
        missing.append("suggestion: タイトル | サブタイトル | 入力欄に入る文")
    if missing:
        raise DeployError(
            "Pipeのデプロイには、モデル画面に出す説明文と提案チップが必須です。"
            "ファイル先頭のdocstringに次の行を追加してください:\n  "
            + "\n  ".join(missing)
            + "\n(手本: tools/open_webui_excel_analysis_pipe.py の先頭)"
        )


# モデル設定を新規作成するときの既定の能力(文章処理モデルと同じ構成)
DEFAULT_MODEL_CAPABILITIES = {
    "file_upload": True,
    "citations": True,
    "status_updates": True,
    "image_generation": False,
    "vision": False,
    "code_interpreter": False,
    "terminal": False,
    "web_search": False,
    "file_context": False,
}


def resolve_plugin_id(fields: dict[str, str], override: str | None) -> str:
    """登録IDを決める。frontmatterの id: か --id の明示だけを認める。"""
    plugin_id = (override or fields.get("id") or "").strip()
    if not plugin_id:
        raise DeployError(
            "登録IDが決められません。ファイル先頭のdocstringに `id: <半角英数と_>` を"
            "書くか、--id で指定してください\n"
            "(IDはOpen WebUI上の識別子。既存と同じIDなら更新、無ければ新規作成になる)"
        )
    if not ID_PATTERN.match(plugin_id):
        raise DeployError(
            f"IDに使えるのは小文字英数と _ だけです: {plugin_id}"
        )
    return plugin_id


def ensure_local_url(url_text: str) -> str:
    """接続先URLが localhost であることを強制する(キーの流出防止)。"""
    parsed = urlparse(url_text)
    if parsed.scheme != "http" or parsed.hostname not in ALLOWED_HOSTS:
        raise DeployError(
            f"接続先は http://localhost のOpen WebUIに限られます: {url_text}"
        )
    return url_text.rstrip("/")


def build_diff(server_content: str, local_content: str, name: str) -> str:
    """サーバー版とローカル版の unified diff(登録前の確認表示用)。"""
    lines = difflib.unified_diff(
        server_content.splitlines(),
        local_content.splitlines(),
        fromfile=f"OpenWebUI:{name}",
        tofile=f"local:{name}",
        lineterm="",
    )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Open WebUI 管理APIクライアント(localhost限定)
# ----------------------------------------------------------------------
def load_api_key() -> str:
    key = (os.environ.get(KEY_ENV) or "").strip()
    if key:
        return key
    key_file = Path(os.environ.get(KEY_FILE_ENV) or DEFAULT_KEY_FILE)
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip()
        if key:
            return key
    raise DeployError(
        "Open WebUIの管理者APIキーが見つかりません\n"
        "対処(1回だけ人の作業):\n"
        "  1. Open WebUIに管理者でログイン → 設定 → アカウント → APIキー を作成\n"
        f"  2. {DEFAULT_KEY_FILE} にキーだけを保存する\n"
        f"     (または環境変数 {KEY_ENV} に設定する)"
    )


def resolve_base_url() -> str:
    env_url = (os.environ.get(URL_ENV) or "").strip()
    if env_url:
        return ensure_local_url(env_url)
    # doctor.py と同じ方法で公開ポートを自動検出する
    try:
        result = subprocess.run(
            ["docker", "port", "open-webui", "8080/tcp"],
            capture_output=True,
            text=True,
            timeout=10,
            # 自動実行(pythonw・コンソール無し)から呼ばれると、ここで黒い窓が
            # 一瞬開く。5分ごとの見張りから呼ぶため利用者の画面を邪魔していた
            creationflags=NO_WINDOW,
        )
        for line in (result.stdout or "").splitlines():
            match = re.search(r":(\d+)\s*$", line.strip())
            if match:
                return f"http://localhost:{match.group(1)}"
    except (OSError, subprocess.SubprocessError):
        pass
    return DEFAULT_URL


@dataclass
class ApiClient:
    base_url: str
    api_key: str

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        none_on: tuple[int, ...] = (),
    ) -> object:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            if error.code in none_on:
                return None
            detail = error.read().decode("utf-8", errors="replace")[:300]
            if error.code in (401, 403):
                raise DeployError(
                    f"Open WebUIに認証を拒否されました(HTTP {error.code})\n"
                    "対処: APIキーが管理者ユーザーのものか、失効していないかを"
                    "Open WebUIの 設定 → アカウント → APIキー で確認してください"
                )
            raise DeployError(
                f"Open WebUIの応答がエラーです(HTTP {error.code} {method} {path})\n"
                f"詳細: {detail}"
            )
        except (urllib.error.URLError, OSError) as error:
            raise DeployError(
                f"Open WebUIへ接続できません: {self.base_url}\n"
                f"詳細: {error}\n"
                "対処: Docker Desktop で open-webui コンテナが起動しているか確認し、"
                "python tools\\doctor.py で全体を診断してください"
            )
        try:
            return json.loads(body) if body else None
        except json.JSONDecodeError:
            raise DeployError(f"Open WebUIの応答を解釈できません: {body[:200]}")

    # --- Pipe(Open WebUIではFunction)とToolでAPIの系統を切り替える ---
    def api_prefix(self, plugin_type: str) -> str:
        return "/api/v1/functions" if plugin_type == "pipe" else "/api/v1/tools"

    def list_entries(self, plugin_type: str) -> list[dict]:
        entries = self.request("GET", f"{self.api_prefix(plugin_type)}/")
        return entries if isinstance(entries, list) else []

    def get_entry(self, plugin_type: str, plugin_id: str) -> dict | None:
        for entry in self.list_entries(plugin_type):
            if entry.get("id") == plugin_id:
                detail = self.request(
                    "GET", f"{self.api_prefix(plugin_type)}/id/{plugin_id}"
                )
                return detail if isinstance(detail, dict) else entry
        return None

    def create_entry(
        self, plugin_type: str, plugin_id: str, name: str, description: str,
        content: str,
    ) -> dict:
        payload = {
            "id": plugin_id,
            "name": name,
            "content": content,
            "meta": {"description": description, "manifest": {}},
        }
        result = self.request(
            "POST", f"{self.api_prefix(plugin_type)}/create", payload
        )
        if not isinstance(result, dict) or result.get("id") != plugin_id:
            raise DeployError(f"作成結果を確認できません: {str(result)[:200]}")
        return result

    def update_entry(
        self, plugin_type: str, plugin_id: str, name: str, description: str,
        content: str,
    ) -> dict:
        payload = {
            "id": plugin_id,
            "name": name,
            "content": content,
            "meta": {"description": description, "manifest": {}},
        }
        result = self.request(
            "POST", f"{self.api_prefix(plugin_type)}/id/{plugin_id}/update", payload
        )
        if not isinstance(result, dict) or result.get("id") != plugin_id:
            raise DeployError(f"更新結果を確認できません: {str(result)[:200]}")
        return result

    def deactivate_function(self, plugin_id: str) -> None:
        """Pipeを無効へ倒す(toggleは反転動作なので、有効なときだけ叩く)。"""
        entry = self.request(
            "GET", f"/api/v1/functions/id/{plugin_id}", none_on=(400, 401, 404)
        )
        if isinstance(entry, dict) and entry.get("is_active"):
            self.request("POST", f"/api/v1/functions/id/{plugin_id}/toggle")

    def delete_entry(self, plugin_type: str, plugin_id: str) -> bool:
        """登録を削除する。**新規登録のロールバックに要る**(台帳 5-3)。"""
        result = self.request(
            "DELETE", f"{self.api_prefix(plugin_type)}/id/{plugin_id}/delete",
            none_on=(400, 401, 404),
        )
        return result is not None

    def delete_model(self, model_id: str) -> bool:
        """モデル設定を削除する。Pipeだけ消してもモデル一覧に残るため対で行う。

        呼び方は実機で確認した形に合わせる(TOOL_PACKAGE_DECISIONS.md 5-3):
        **POST /api/v1/models/model/delete、本文は {"id": ...}**。
        DELETE + クエリ文字列ではない。
        """
        result = self.request(
            "POST", "/api/v1/models/model/delete", {"id": model_id},
            none_on=(400, 401, 404),
        )
        return result is not None

    def remove_pipe_completely(self, plugin_id: str) -> dict[str, bool]:
        """Pipe本体とモデル設定をまとめて取り消し、**消えたことを再取得で確かめる**。

        戻り値は「本当に消えているか」であって、APIが成功を返したかではない。
        片方でも残っていれば呼び出し側がロールバック失敗として扱えるようにする
        (「戻しました」と言いながら残っている状態を作らないため)。
        """
        self.delete_model(plugin_id)
        self.delete_entry("pipe", plugin_id)
        return {
            "model": self.get_model(plugin_id) is None,
            "function": self.get_entry("pipe", plugin_id) is None,
        }

    def activate_function(self, plugin_id: str) -> None:
        """新規作成直後のPipeは無効状態のため、有効に切り替える。"""
        result = self.request("POST", f"/api/v1/functions/id/{plugin_id}/toggle")
        if isinstance(result, dict) and not result.get("is_active", True):
            # toggleは反転動作。既に有効だったものを無効へ倒してしまったら戻す
            self.request("POST", f"/api/v1/functions/id/{plugin_id}/toggle")

    # --- モデル設定(提案チップ・説明文)。Pipeのモデル画面の見え方を決める ---
    def get_model(self, model_id: str) -> dict | None:
        result = self.request(
            "GET", f"/api/v1/models/model?id={model_id}", none_on=(400, 401, 404)
        )
        return result if isinstance(result, dict) else None

    def upsert_model_meta(
        self, model_id: str, name: str, meta: dict, existing: dict | None
    ) -> None:
        payload = {
            "id": model_id,
            "base_model_id": (existing or {}).get("base_model_id"),
            "name": name,
            "meta": meta,
            "params": (existing or {}).get("params") or {},
            # 0.9系はaccess_grantsがNoneだとバリデーションで500になる
            "access_grants": (existing or {}).get("access_grants") or [],
        }
        if existing is None:
            self.request("POST", "/api/v1/models/create", payload)
        else:
            if existing.get("access_control") is not None:
                payload["access_control"] = existing["access_control"]
            self.request(
                "POST", f"/api/v1/models/model/update?id={model_id}", payload
            )


# ----------------------------------------------------------------------
# サブコマンド
# ----------------------------------------------------------------------
def command_list(client: ApiClient) -> int:
    print(f"接続先: {client.base_url}")
    for plugin_type, label in (("pipe", "Pipe(Function)"), ("tool", "Tool")):
        print(f"\n== {label} ==")
        entries = client.list_entries(plugin_type)
        if not entries:
            print("  (登録なし)")
        for entry in entries:
            state = ""
            if plugin_type == "pipe":
                state = "有効" if entry.get("is_active") else "無効"
            print(
                f"  {entry.get('id')}: {entry.get('name')}"
                + (f" [{state}]" if state else "")
            )
    return 0


def command_deploy(client: ApiClient, path: Path, plugin_id_override: str | None,
                   apply: bool) -> int:
    source = path.read_text(encoding="utf-8")
    plugin_type = detect_plugin_type(source)
    validate_container_imports(source)
    fields = parse_frontmatter(source)
    plugin_id = resolve_plugin_id(fields, plugin_id_override)
    name = fields.get("title") or plugin_id
    description = fields.get("description") or name
    label = "Pipe(Function)" if plugin_type == "pipe" else "Tool"

    print(f"接続先    : {client.base_url}")
    print(f"対象      : {path.relative_to(REPO_ROOT)}")
    print(f"種別      : {label}")
    print(f"ID / 名前 : {plugin_id} / {name}")

    # 提案チップ・説明文。Pipeでは必須(無ければここで拒否され、何も送信しない)
    suggestions = parse_suggestions(source)
    validate_pipe_model_requirements(plugin_type, fields, suggestions)
    model_description = fields.get("model_description") or ""
    manage_model = plugin_type == "pipe"

    existing = client.get_entry(plugin_type, plugin_id)
    unchanged = False
    if existing is None:
        print("操作      : 新規作成(サーバーに同IDなし)")
        diff = build_diff("", source, plugin_id)
    else:
        server_content = str(existing.get("content") or "")
        unchanged = server_content == source
        if unchanged:
            print("操作      : 本体は変更なし(サーバー版とローカル版が一致)")
            diff = ""
        else:
            print("操作      : 更新(サーバー版と差分あり)")
            diff = build_diff(server_content, source, plugin_id)

    if manage_model:
        print(f"モデル設定: 提案チップ{len(suggestions)}件と説明文を登録・更新します")
        mapping, uncovered = check_suggestion_routing(source, suggestions)
        dead_chips: list[str] = []
        for index, item in enumerate(suggestions):
            matched = mapping[index][1] if mapping else []
            note = f" → 操作: {', '.join(matched)}" if matched else ""
            if mapping and not matched:
                dead_chips.append(item["title"][0])
                note = " → ⚠どの操作にも一致しない"
            print(f"  - {item['title'][0]}({item['title'][1]}): "
                  f"{item['content']}{note}")
        if dead_chips:
            raise DeployError(
                "どの操作にも振り分けられない提案チップがあります: "
                + ", ".join(dead_chips)
                + "\n対処: チップの入力文を、このPipeの OPERATION_PATTERNS の"
                "いずれかに一致する言い回しへ直してください"
                "(押しても意図した処理へ入らない死にボタンになるため)"
            )
        if uncovered:
            print(f"  注意: 提案チップの無い操作があります: {', '.join(uncovered)}")

    if not unchanged:
        print("\n--- 差分 ---")
        print(diff if diff else "(差分なし)")

    if not apply:
        print("\ndry-runのため変更していません。反映するには --apply を付けてください")
        return 0

    if existing is None:
        client.create_entry(plugin_type, plugin_id, name, description, source)
        if plugin_type == "pipe":
            client.activate_function(plugin_id)
        print("\n作成しました")
    elif not unchanged:
        client.update_entry(plugin_type, plugin_id, name, description, source)
        print("\n更新しました")

    # 反映確認: サーバーから取り直して内容が一致するかを見る
    stored = client.get_entry(plugin_type, plugin_id)
    if stored is None or str(stored.get("content") or "") != source:
        raise DeployError(
            "登録後の確認で内容の不一致を検出しました。"
            "Open WebUIの管理画面(Workspace → Functions/Tools)で状態を確認してください"
        )
    state_note = ""
    if plugin_type == "pipe":
        active = bool(stored.get("is_active"))
        state_note = f" / 状態: {'有効' if active else '無効'}"
        if not active:
            state_note += "(管理画面のFunctionsでトグルを有効にしてください)"
    print(f"検証OK: サーバー版とローカル版が一致{state_note}")

    if manage_model:
        existing_model = client.get_model(plugin_id)
        meta = dict((existing_model or {}).get("meta") or {})
        meta.setdefault("capabilities", dict(DEFAULT_MODEL_CAPABILITIES))
        if model_description:
            meta["description"] = model_description
        if suggestions:
            meta["suggestion_prompts"] = suggestions
        client.upsert_model_meta(plugin_id, name, meta, existing_model)
        print("モデル設定(提案チップ・説明文)を反映しました")

    if plugin_type == "pipe":
        print("モデル一覧に表示されるまで数秒かかることがあります")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    parser = argparse.ArgumentParser(
        description="Open WebUIへPipe/Toolを登録・更新する(localhost限定)"
    )
    parser.add_argument("file", nargs="?", help="tools/ 直下の open_webui_*.py")
    parser.add_argument("--apply", action="store_true",
                        help="実際に登録・更新する(無指定はdry-run)")
    parser.add_argument("--id", dest="plugin_id",
                        help="登録IDの明示(通常はfrontmatterのid:を使う)")
    parser.add_argument("--list", action="store_true",
                        help="登録済みのPipe/Toolを一覧表示")
    args = parser.parse_args(argv)

    try:
        if not args.list and not args.file:
            parser.print_help()
            return 1
        client = ApiClient(base_url=resolve_base_url(), api_key=load_api_key())
        if args.list:
            return command_list(client)
        path = validate_target_path(args.file)
        return command_deploy(client, path, args.plugin_id, args.apply)
    except DeployError as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
