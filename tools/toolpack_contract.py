r"""追加ツールの起動・返却契約(request.json と結果JSON)。

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-5)。

起動: `python main.py --request <ジョブ内のrequest.json>`(引数は --request だけ)。
返却: 標準出力へJSON1行のみ。

    { "status": "ok", "message": "…", "files": ["out相対"], "skipped": [], "notes": [] }

このモジュールは「契約の形」と「返り値の機械検証」だけを持つ。
実行・隔離は toolpack_runner / toolpack_child、業務判断は追加ツールの main.py の仕事。
"""

from __future__ import annotations

import json
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

CONTRACT_VERSION = 1

# ジョブディレクトリ内の決まった配置
INPUT_SUBDIR = "in"
OUTPUT_SUBDIR = "out"
TMP_SUBDIR = "tmp"
MPL_SUBDIR = "mpl"
REQUEST_NAME = "request.json"

# 返却ファイルの上限(台帳 D-9)
MAX_OUTPUT_FILES = 20
MAX_OUTPUT_TOTAL_BYTES = 200 * 1024 * 1024

VALID_STATUS = ("ok", "user_error")

_SAFE_NAME = re.compile(r"[^0-9A-Za-zぁ-んァ-ヶーｦ-ﾟ一-龠々〆._-]+")


@dataclass
class ToolResult:
    """検証済みの結果。files は out ディレクトリ内の絶対パス。"""

    status: str
    message: str
    files: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class ContractError(RuntimeError):
    """契約違反(request.json の生成不能・結果JSONの検証失敗)。

    利用者へは個人情報を含まないエラーコードと対処を返すため、
    ここには本文・氏名・元ファイル名を入れない(台帳 D-9)。
    """


def safe_stored_name(input_id: str, filename: str) -> str:
    """入力の内部保存名。id を接頭辞にして、同名でも衝突・上書きしないようにする。"""
    suffix = Path(filename).suffix.lower()
    stem = _SAFE_NAME.sub("_", Path(filename).stem).strip("._") or "input"
    return f"{input_id}_{stem}{suffix}"


# ---------------------------------------------------------------------------
# request.json の生成
# ---------------------------------------------------------------------------
def prepare_job_dirs(job_dir: Path) -> dict[str, Path]:
    """ジョブディレクトリの決まった小部屋を作る。"""
    job_dir = Path(job_dir)
    dirs = {
        "job": job_dir,
        "in": job_dir / INPUT_SUBDIR,
        "out": job_dir / OUTPUT_SUBDIR,
        "tmp": job_dir / TMP_SUBDIR,
        "mpl": job_dir / MPL_SUBDIR,
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def build_request(
    job_dir: Path,
    instruction: str,
    inputs: list[tuple[str, str | None, Path, str]],
    config: dict | None = None,
) -> Path:
    """入力を job_dir/in へ複製し、request.json を書いてそのパスを返す。

    inputs は (id, role, 元ファイルパス, 表示ファイル名) のリスト。
    path は job_dir 基準の相対パスで書く(PCの絶対パスを渡さない。台帳 D-5)。
    """
    dirs = prepare_job_dirs(job_dir)
    seen_ids: set[str] = set()
    entries: list[dict] = []
    for input_id, role, src, filename in inputs:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", input_id):
            raise ContractError(f"入力IDが不正です: {input_id!r}")
        if input_id in seen_ids:
            raise ContractError(f"入力IDが重複しています: {input_id}")
        seen_ids.add(input_id)
        stored = safe_stored_name(input_id, filename)
        target = dirs["in"] / stored
        shutil.copy2(src, target)  # 元ファイルは変更しない(複製だけ)
        entries.append({
            "id": input_id,
            "role": role,
            "path": f"{INPUT_SUBDIR}/{stored}",
            "filename": filename,
        })

    request = {
        "contract": CONTRACT_VERSION,
        "instruction": instruction,
        "inputs": entries,
        "outdir": OUTPUT_SUBDIR,
        "config": dict(config or {}),
    }
    request_path = dirs["job"] / REQUEST_NAME
    request_path.write_text(
        json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return request_path


def read_request(request_path: Path) -> dict:
    """追加ツール側が request.json を読むための共通ヘルパ(任意利用)。"""
    data = json.loads(Path(request_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("contract") != CONTRACT_VERSION:
        raise ContractError("request.json の contract が未対応です")
    return data


# ---------------------------------------------------------------------------
# 結果JSONの検証
# ---------------------------------------------------------------------------
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_result(
    stdout_text: str, out_dir: Path, allowed_suffixes: tuple[str, ...] | None = None
) -> ToolResult:
    """標準出力(JSON1行)と成果物を検証する。違反は ContractError。

    ここが「読んで返すだけ」の持ち出し(例: files に ../../data のパス)を止める
    最後の砦になる。書き込み制限だけでは防げないため、必ず通す(台帳 D-9)。
    """
    text = (stdout_text or "").strip()
    if not text:
        raise ContractError("E-EMPTY: 標準出力が空です。JSON1行を出力してください")
    if "\n" in text:
        raise ContractError(
            "E-MULTILINE: 標準出力が複数行です。JSON1行だけにし、"
            "本文や進捗を標準出力へ出さないでください"
        )
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ContractError(f"E-JSON: 標準出力をJSONとして読めません({error})") from error
    if not isinstance(data, dict):
        raise ContractError("E-SHAPE: 結果はオブジェクトにしてください")

    status = data.get("status")
    if status not in VALID_STATUS:
        raise ContractError(f"E-STATUS: status が不正です({status!r})。ok / user_error のいずれか")
    message = data.get("message", "")
    if not isinstance(message, str):
        raise ContractError("E-MESSAGE: message は文字列にしてください")
    for key in ("skipped", "notes"):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ContractError(f"E-LIST: {key} は文字列リストにしてください")

    out_dir = Path(out_dir).resolve()
    raw_files = data.get("files", [])
    if not isinstance(raw_files, list) or not all(isinstance(item, str) for item in raw_files):
        raise ContractError("E-FILES: files は文字列(out相対パス)のリストにしてください")
    if len(raw_files) > MAX_OUTPUT_FILES:
        raise ContractError(
            f"E-COUNT: 成果物が多すぎます({len(raw_files)} > 上限{MAX_OUTPUT_FILES})"
        )

    files: list[Path] = []
    total = 0
    for rel in raw_files:
        candidate = Path(rel)
        if candidate.is_absolute() or ".." in candidate.parts or "\\" in rel:
            raise ContractError(f"E-PATH: files のパスが不正です({rel!r})。out からの相対パスにしてください")
        resolved = (out_dir / candidate).resolve()
        if not _is_within(resolved, out_dir):
            raise ContractError(f"E-ESCAPE: 成果物が out の外を指しています({rel!r})")
        if not resolved.is_file():
            raise ContractError(f"E-MISSING: 成果物が存在しません({rel!r})")
        lstat = resolved.lstat()
        if stat.S_ISLNK(lstat.st_mode):
            raise ContractError(f"E-SYMLINK: 成果物がシンボリックリンクです({rel!r})")
        try:
            if resolved.is_junction():  # Python 3.12+
                raise ContractError(f"E-JUNCTION: 成果物がジャンクションです({rel!r})")
        except AttributeError:  # pragma: no cover - 3.12未満の保険
            pass
        if lstat.st_nlink > 1:
            raise ContractError(f"E-HARDLINK: 成果物がハードリンクです({rel!r})")
        if allowed_suffixes is not None and resolved.suffix.lower() not in allowed_suffixes:
            raise ContractError(
                f"E-SUFFIX: 宣言外の拡張子です({resolved.suffix})。"
                f"tool.json の outputs.produces に合わせてください"
            )
        total += lstat.st_size
        if total > MAX_OUTPUT_TOTAL_BYTES:
            raise ContractError(
                f"E-SIZE: 成果物の合計が大きすぎます(上限{MAX_OUTPUT_TOTAL_BYTES}バイト)"
            )
        files.append(resolved)

    return ToolResult(
        status=status,
        message=message,
        files=files,
        skipped=list(data.get("skipped", [])),
        notes=list(data.get("notes", [])),
    )
