r"""追加ツールパッケージ(.localtool)の検証器。

使い方:
  text-processing-bridge\.venv\Scripts\python.exe tools\toolpack_verify.py <パッケージ.localtool> [--dest 展開先]

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-2 の検査規約・D-5 の tool.json 宣言項目)。
GitHub Actions と院内インストーラの両方から呼ぶ「検査の唯一の正本」であり、
標準ライブラリだけで動く(jsonschema 等は wheelhouse に無いため使わない)。

- extractall は使わない。全エントリ名を検査してから、1件ずつ自前の結合先へ展開する
- 検査は段階ごとに「どの段階で・何が問題で・どう直すか」を返す
  (オンラインAIへそのまま突き返す文面になる)

【重要な限界】静的検査(AST)は import socket や os.system のような明白な禁止呼び出しを
拒否するだけで、getattr や文字列組み立てによる難読化はすり抜けられる。完全な防御ではない。
実行時の閉じ込めは専用ランナー(台帳 D-9)が担い、この検証器は「善意だが誤るコード」を
早期に突き返すための柵である。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import stat
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 上限(台帳 D-2 の初期値。名前付き定数にしてテストの対象にする)
# ---------------------------------------------------------------------------
MAX_ENTRIES = 500
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_ENTRY_NAME_CHARS = 160

MANIFEST_NAME = "manifest.sha256"
REQUIRED_FILES = ("tool.json", "main.py", MANIFEST_NAME, "README.md")

# 初期版で同梱を禁止する拡張子(台帳 D-9)
FORBIDDEN_SUFFIXES = frozenset(
    {".pyc", ".pyd", ".dll", ".exe", ".bat", ".cmd", ".ps1", ".pth"}
)

# Windows の予約デバイス名(拡張子が付いていても不可)
RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

# エントリ名に許す文字。空白・コロン(ADS)・バックスラッシュ・非ASCIIはここで落ちる
NAME_CHARS = re.compile(r"^[A-Za-z0-9._/-]+$")

ID_PATTERN = re.compile(r"^[a-z0-9_]{3,32}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
EXT_PATTERN = re.compile(r"^\.[a-z0-9]{1,10}$")
ROLE_NAME_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")
TIMEOUT_MIN = 1
TIMEOUT_MAX = 1500

MANIFEST_LINE = re.compile(r"^([0-9a-f]{64})  (\S+)$")

# 静的検査: 常に禁止する import(子プロセス・ネイティブ呼び出し・動的import)
FORBIDDEN_IMPORTS_ALWAYS = frozenset(
    {"subprocess", "ctypes", "_winapi", "multiprocessing", "importlib", "pty"}
)
# 通信系 import。permissions.ollama を宣言したパッケージだけ許す
# (宛先の制限は実行時に専用ランナーが行う)
NETWORK_IMPORTS = frozenset(
    {
        "socket", "http", "urllib", "httpx", "requests", "ftplib",
        "smtplib", "poplib", "imaplib", "telnetlib", "socketserver", "xmlrpc",
    }
)
# 機密パスらしき文字列(参照そのものを拒否する)
SENSITIVE_LITERAL_PATTERNS = (
    (re.compile(r"\.env"), ".env"),
    (re.compile(r"admin_api_key", re.IGNORECASE), "admin_api_key"),
    (re.compile(r"OPEN_WEBUI_ADMIN_API_KEY"), "OPEN_WEBUI_ADMIN_API_KEY"),
    (re.compile(r"(?<![A-Za-z0-9_.-])data[/\\]"), "data/"),
    (re.compile(r"(?<![A-Za-z0-9_.-])templates[/\\]"), "templates/"),
)

STAGE_ZIP = "ZIP検査"
STAGE_EXTRACT = "展開"
STAGE_LAYOUT = "構成"
STAGE_MANIFEST = "manifest"
STAGE_TOOLJSON = "tool.json"
STAGE_STATIC = "静的検査"


@dataclass(frozen=True)
class VerifyError:
    """1件の検査エラー。オンラインAIへそのまま返せる形にする。"""

    stage: str
    problem: str
    fix: str

    def __str__(self) -> str:
        return f"[{self.stage}] {self.problem}\n  対処: {self.fix}"


@dataclass
class VerifyResult:
    ok: bool
    errors: list[VerifyError] = field(default_factory=list)
    tool_json: dict | None = None
    package_dir: Path | None = None


# ---------------------------------------------------------------------------
# 1. ZIPエントリの検査(展開前)
# ---------------------------------------------------------------------------
def _entry_name_errors(name: str) -> list[VerifyError]:
    """1エントリ名の検査。展開前に呼ぶ。"""

    def err(problem: str, fix: str) -> VerifyError:
        return VerifyError(STAGE_ZIP, f"{name!r}: {problem}", fix)

    errors: list[VerifyError] = []
    stripped = name[:-1] if name.endswith("/") else name

    if not stripped:
        return [err("空のエントリ名です", "エントリ名を付けてください")]
    if len(name) > MAX_ENTRY_NAME_CHARS:
        errors.append(err(
            f"パスが長すぎます({len(name)}文字 > 上限{MAX_ENTRY_NAME_CHARS})",
            "ディレクトリ階層とファイル名を短くしてください",
        ))
    if not name.isascii():
        errors.append(err(
            "パスに非ASCII文字が含まれています",
            "パッケージ内のパスはASCII限定です。日本語はファイルの中身にだけ使ってください",
        ))
    if "\\" in name:
        errors.append(err(
            "バックスラッシュが含まれています",
            "区切りは / だけを使ってください",
        ))
    if ":" in name:
        errors.append(err(
            "コロンが含まれています(ドライブ文字または代替データストリーム)",
            "パスからコロンを取り除いてください",
        ))
    if name.startswith("/"):
        errors.append(err("絶対パスです", "パッケージ直下からの相対パスにしてください"))
    if any(ch.isspace() for ch in name):
        errors.append(err(
            "空白文字が含まれています",
            "パスに空白を使わないでください(manifest の曖昧さを避けるため)",
        ))

    for component in stripped.split("/"):
        if component in ("", "."):
            errors.append(err(
                "正規化されていないパスです(空要素または「.」)",
                "『a//b』『./a』のような書き方をやめ、正規化済みのパスにしてください",
            ))
            continue
        if component == "..":
            errors.append(err(
                "「..」による親ディレクトリ参照です",
                "パッケージの外を指すパスは書けません",
            ))
            continue
        if component.endswith((" ", ".")):
            errors.append(err(
                f"末尾が空白またはピリオドの要素があります({component!r})",
                "Windowsでは末尾の空白・ピリオドが消えて別名になります。取り除いてください",
            ))
        base = component.split(".")[0].lower()
        if base in RESERVED_NAMES:
            errors.append(err(
                f"Windowsの予約名です({component!r})",
                "CON・NUL・COM1 等の予約名は拡張子付きでも使えません。改名してください",
            ))

    if not errors and not NAME_CHARS.match(stripped):
        errors.append(err(
            "使用できない文字が含まれています",
            "パスに使えるのは英数と . _ - / だけです",
        ))
    if PurePosixPath(stripped).suffix.lower() in FORBIDDEN_SUFFIXES:
        errors.append(err(
            f"同梱が禁止された種類のファイルです({PurePosixPath(stripped).suffix})",
            "実行可能ファイル・バイナリ(.pyc .pyd .dll .exe .bat .cmd .ps1 .pth)は"
            "初期版パッケージへ入れられません。Pythonソースだけにしてください",
        ))
    return errors


def check_zip_info(info: zipfile.ZipInfo) -> list[VerifyError]:
    """名前以外のエントリ属性(暗号化・特殊ファイル)の検査。"""
    errors: list[VerifyError] = []
    if info.flag_bits & 0x1:
        errors.append(VerifyError(
            STAGE_ZIP,
            f"{info.filename!r}: 暗号化されたエントリです",
            "パスワード付きZIPは受け付けません。暗号化せずに作り直してください",
        ))
    # 種別ビットだけを見る。writestr は許可ビット(0o600等)しか入れないため、
    # mode全体で判定すると通常ファイルまで誤検知する
    file_type = stat.S_IFMT(info.external_attr >> 16)
    if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
        errors.append(VerifyError(
            STAGE_ZIP,
            f"{info.filename!r}: 通常ファイル・ディレクトリ以外のエントリです(シンボリックリンク等)",
            "リンクや特殊ファイルは入れられません。実体ファイルだけにしてください",
        ))
    return errors


def check_zip_entries(zf: zipfile.ZipFile) -> tuple[list[VerifyError], list[zipfile.ZipInfo]]:
    """全エントリを展開前に検査し、(エラー一覧, ファイルエントリ一覧) を返す。"""
    errors: list[VerifyError] = []
    infos = zf.infolist()

    if len(infos) > MAX_ENTRIES:
        errors.append(VerifyError(
            STAGE_ZIP,
            f"エントリ数が多すぎます({len(infos)}件 > 上限{MAX_ENTRIES}件)",
            "パッケージに入れるファイルを減らしてください",
        ))

    total = 0
    seen_exact: set[str] = set()
    seen_folded: dict[str, str] = {}
    for info in infos:
        name = info.filename
        errors.extend(_entry_name_errors(name))

        errors.extend(check_zip_info(info))

        stripped = name[:-1] if name.endswith("/") else name
        if name in seen_exact:
            errors.append(VerifyError(
                STAGE_ZIP,
                f"{name!r}: 重複したエントリです",
                "同じ名前のエントリを複数入れないでください",
            ))
        seen_exact.add(name)
        folded = stripped.lower()
        if folded in seen_folded and seen_folded[folded] != name:
            errors.append(VerifyError(
                STAGE_ZIP,
                f"{name!r}: 大文字小文字だけが違うパスがあります({seen_folded[folded]!r})",
                "Windowsでは同じファイルとして衝突します。どちらかを改名してください",
            ))
        seen_folded.setdefault(folded, name)

        if not info.is_dir():
            if info.file_size > MAX_FILE_BYTES:
                errors.append(VerifyError(
                    STAGE_ZIP,
                    f"{name!r}: 1ファイルが大きすぎます"
                    f"({info.file_size}バイト > 上限{MAX_FILE_BYTES})",
                    "ファイルを小さくするか、パッケージから外してください",
                ))
            total += info.file_size
    if total > MAX_TOTAL_BYTES:
        errors.append(VerifyError(
            STAGE_ZIP,
            f"非圧縮の合計が大きすぎます({total}バイト > 上限{MAX_TOTAL_BYTES})",
            "パッケージ全体を小さくしてください",
        ))

    files = [info for info in infos if not info.is_dir()]
    return errors, files


# ---------------------------------------------------------------------------
# 2. 展開(検査済みエントリを1件ずつ)
# ---------------------------------------------------------------------------
def extract_package(
    zf: zipfile.ZipFile, infos: list[zipfile.ZipInfo], dest: Path
) -> list[VerifyError]:
    """検査済みエントリを1件ずつ dest へ展開する。extractall は使わない。"""
    errors: list[VerifyError] = []
    dest = dest.resolve()
    for info in infos:
        target = dest.joinpath(*PurePosixPath(info.filename).parts)
        # 名前検査を通った後だが、結合結果でも二重に確かめる(D-2 の思想)
        if not target.resolve().is_relative_to(dest):
            errors.append(VerifyError(
                STAGE_EXTRACT,
                f"{info.filename!r}: 展開先の外を指しています",
                "パッケージ直下からの相対パスにしてください",
            ))
            continue
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with zf.open(info) as src, open(target, "wb") as out:
            while chunk := src.read(65536):
                written += len(chunk)
                if written > MAX_FILE_BYTES:
                    errors.append(VerifyError(
                        STAGE_EXTRACT,
                        f"{info.filename!r}: 展開中に上限({MAX_FILE_BYTES}バイト)を超えました",
                        "宣言サイズと実サイズが食い違っています。ZIPを作り直してください",
                    ))
                    break
                out.write(chunk)
        if written != info.file_size and written <= MAX_FILE_BYTES:
            errors.append(VerifyError(
                STAGE_EXTRACT,
                f"{info.filename!r}: ヘッダの宣言サイズと実サイズが一致しません",
                "ZIPが壊れています。作り直して持ち込み直してください",
            ))
    return errors


# ---------------------------------------------------------------------------
# 3. 構成(必須ファイル)
# ---------------------------------------------------------------------------
def check_layout(package_dir: Path) -> list[VerifyError]:
    errors: list[VerifyError] = []
    for required in REQUIRED_FILES:
        if not (package_dir / required).is_file():
            errors.append(VerifyError(
                STAGE_LAYOUT,
                f"必須ファイルがありません: {required}",
                f"パッケージ直下に {required} を置いてください",
            ))
    if not list(package_dir.glob("tests/test_*.py")):
        errors.append(VerifyError(
            STAGE_LAYOUT,
            "tests/ に test_*.py がありません",
            "tests/test_main.py のような単体テストを最低1つ入れてください",
        ))
    return errors


# ---------------------------------------------------------------------------
# 4. manifest.sha256 の突合
# ---------------------------------------------------------------------------
def check_manifest(package_dir: Path) -> list[VerifyError]:
    """全列挙との照合。欠け・余り・ハッシュ不一致は別々のエラーにする。

    manifest.sha256 自身は照合一覧の対象外(自分自身をハッシュ化できない)。
    """
    errors: list[VerifyError] = []
    manifest_path = package_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        return []  # 構成検査が既に報告している

    listed: dict[str, str] = {}
    for line_no, raw in enumerate(
        manifest_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        line = raw.strip("\r")
        if not line:
            continue
        match = MANIFEST_LINE.match(line)
        if not match:
            errors.append(VerifyError(
                STAGE_MANIFEST,
                f"{line_no}行目の形式が不正です: {line[:80]!r}",
                "各行を『<sha256の16進64桁><空白2つ><相対パス>』にしてください",
            ))
            continue
        digest, rel = match.group(1), match.group(2)
        if rel == MANIFEST_NAME:
            errors.append(VerifyError(
                STAGE_MANIFEST,
                f"一覧に自分自身({MANIFEST_NAME})が含まれています",
                "manifest は自分自身をハッシュ化できません。一覧から外してください",
            ))
            continue
        if rel in listed:
            errors.append(VerifyError(
                STAGE_MANIFEST,
                f"一覧に重複があります: {rel}",
                "同じパスを2回列挙しないでください",
            ))
            continue
        listed[rel] = digest

    actual = {
        path.relative_to(package_dir).as_posix(): path
        for path in sorted(package_dir.rglob("*"))
        if path.is_file() and path.name != MANIFEST_NAME
    }

    for rel in sorted(set(listed) - set(actual)):
        errors.append(VerifyError(
            STAGE_MANIFEST,
            f"一覧にあるファイルが存在しません(欠け): {rel}",
            "ファイルを入れ忘れたか、一覧が古いままです。揃えてください",
        ))
    for rel in sorted(set(actual) - set(listed)):
        errors.append(VerifyError(
            STAGE_MANIFEST,
            f"一覧に無いファイルがあります(余り): {rel}",
            f"{MANIFEST_NAME} 以外の全ファイルを一覧へ列挙してください",
        ))
    for rel in sorted(set(listed) & set(actual)):
        digest = hashlib.sha256(actual[rel].read_bytes()).hexdigest()
        if digest != listed[rel]:
            errors.append(VerifyError(
                STAGE_MANIFEST,
                f"ハッシュが一致しません: {rel}",
                "ファイルを変更したら manifest を作り直してください",
            ))
    return errors


# ---------------------------------------------------------------------------
# 5. tool.json の検証(台帳 D-5。標準ライブラリのみで手書き)
# ---------------------------------------------------------------------------
def _canonical(name: str) -> str:
    return name.lower().replace("-", "_")


def load_allowed_packages(repo_root: Path) -> tuple[dict[str, set[str]], set[tuple[str, str]] | None]:
    """requirements 2本と wheelhouse から依存の許可リストを作る(手書きの一覧は持たない)。"""
    requirement_line = re.compile(r"^([A-Za-z0-9._-]+)==([A-Za-z0-9.]+)")
    allowed: dict[str, set[str]] = {}
    bridge = repo_root / "text-processing-bridge"
    for filename in ("requirements.txt", "requirements-office.txt"):
        path = bridge / filename
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            match = requirement_line.match(line)
            if match:
                allowed.setdefault(_canonical(match.group(1)), set()).add(match.group(2))

    wheelhouse = bridge / "wheelhouse-win"
    wheels: set[tuple[str, str]] | None = None
    if wheelhouse.is_dir():
        wheels = set()
        for wheel in wheelhouse.glob("*.whl"):
            parts = wheel.name.split("-")
            if len(parts) >= 2:
                wheels.add((_canonical(parts[0]), parts[1]))
    return allowed, wheels


def check_tool_json(package_dir: Path, repo_root: Path) -> tuple[list[VerifyError], dict | None]:
    errors: list[VerifyError] = []

    def err(problem: str, fix: str) -> None:
        errors.append(VerifyError(STAGE_TOOLJSON, problem, fix))

    path = package_dir / "tool.json"
    if not path.is_file():
        return [], None  # 構成検査が既に報告している
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        err(f"JSONとして読めません({error})", "tool.json をUTF-8の正しいJSONにしてください")
        return errors, None
    if not isinstance(data, dict):
        err("トップレベルがオブジェクトではありません", "tool.json は {…} の形にしてください")
        return errors, None

    allowed_keys = {
        "schema_version", "id", "version", "display_name", "summary",
        "inputs", "outputs", "requires", "permissions", "timeout_seconds",
        "routing", "smoke",
    }
    for key in sorted(set(data) - allowed_keys):
        err(
            f"未知の項目があります: {key}",
            "宣言できる項目は台帳D-5の一覧だけです。承認状態などを書き込まないでください",
        )
    for key in sorted(allowed_keys - set(data)):
        err(f"必須項目がありません: {key}", f"{key} を追加してください")
    if errors and (allowed_keys - set(data)):
        return errors, data  # 必須欠けが多いときは以降の型検査で二重報告しない

    def expect_type(value, types, label: str, fix: str) -> bool:
        if isinstance(value, bool) and bool not in (types if isinstance(types, tuple) else (types,)):
            err(f"{label} の型が不正です", fix)
            return False
        if not isinstance(value, types):
            err(f"{label} の型が不正です", fix)
            return False
        return True

    if expect_type(data["schema_version"], int, "schema_version", "schema_version は数値 1 にしてください"):
        if data["schema_version"] != 1:
            err(f"schema_version が未対応です: {data['schema_version']}", "現在の規格は 1 だけです")
    if expect_type(data["id"], str, "id", "id は文字列にしてください"):
        if not ID_PATTERN.match(data["id"]):
            err(f"id が規約に合いません: {data['id']!r}", "id は [a-z0-9_] の3〜32文字にしてください")
    if expect_type(data["version"], str, "version", "version は文字列にしてください"):
        if not VERSION_PATTERN.match(data["version"]):
            err(f"version が規約に合いません: {data['version']!r}", "『1.0.0』のような3要素の数字にしてください")
    if expect_type(data["display_name"], str, "display_name", "display_name は文字列にしてください"):
        if not data["display_name"].strip():
            err("display_name が空です", "利用者に見せる名前を入れてください")
        if "未承認" in data["display_name"] or "承認済" in data["display_name"]:
            err(
                "display_name に承認状態が書かれています",
                "承認情報はパッケージに書けません(【未承認】の接頭辞はインストーラが付けます)",
            )
    if expect_type(data["summary"], str, "summary", "summary は文字列にしてください"):
        if not data["summary"].strip():
            err("summary が空です", "一覧に出す1行説明を入れてください")

    # inputs
    inputs = data["inputs"]
    if expect_type(inputs, dict, "inputs", "inputs はオブジェクトにしてください"):
        for key in sorted(set(inputs) - {"accepts", "min", "max", "roles"}):
            err(f"inputs に未知の項目があります: {key}", "inputs は accepts / min / max / roles だけです")
        accepts = inputs.get("accepts")
        if not isinstance(accepts, list) or not accepts or not all(
            isinstance(item, str) and EXT_PATTERN.match(item) for item in accepts
        ):
            err(
                "inputs.accepts が不正です",
                "『.xlsx』のような小文字拡張子のリストを1件以上入れてください",
            )
        minimum, maximum = inputs.get("min"), inputs.get("max")
        if (
            not isinstance(minimum, int) or isinstance(minimum, bool)
            or not isinstance(maximum, int) or isinstance(maximum, bool)
            or not (1 <= minimum <= maximum <= 10)
        ):
            err("inputs.min / inputs.max が不正です", "1 <= min <= max <= 10 の整数にしてください")
        roles = inputs.get("roles", [])
        if not isinstance(roles, list):
            err("inputs.roles が不正です", "roles は省略するかリストにしてください")
        else:
            for role in roles:
                if (
                    not isinstance(role, dict)
                    or set(role) != {"name", "label"}
                    or not isinstance(role.get("name"), str)
                    or not ROLE_NAME_PATTERN.match(role.get("name", ""))
                    or not isinstance(role.get("label"), str)
                    or not role.get("label", "").strip()
                ):
                    err(
                        f"inputs.roles の項目が不正です: {role!r}",
                        "各roleは {\"name\": 英小文字と_, \"label\": 表示名} にしてください",
                    )

    # outputs
    outputs = data["outputs"]
    if expect_type(outputs, dict, "outputs", "outputs はオブジェクトにしてください"):
        for key in sorted(set(outputs) - {"produces", "may_be_empty"}):
            err(f"outputs に未知の項目があります: {key}", "outputs は produces / may_be_empty だけです")
        produces = outputs.get("produces")
        if not isinstance(produces, list) or not all(
            isinstance(item, str) and EXT_PATTERN.match(item) for item in produces
        ):
            err("outputs.produces が不正です", "小文字拡張子のリスト(空も可)にしてください")
        if not isinstance(outputs.get("may_be_empty"), bool):
            err("outputs.may_be_empty が不正です", "true / false にしてください")

    # requires
    ollama = False
    requires = data["requires"]
    if expect_type(requires, dict, "requires", "requires はオブジェクトにしてください"):
        for key in sorted(set(requires) - {"core_api", "packages", "models"}):
            err(f"requires に未知の項目があります: {key}", "requires は core_api / packages / models だけです")
        core_api = requires.get("core_api")
        if not isinstance(core_api, str) or not any(ch.isdigit() for ch in core_api):
            err("requires.core_api が不正です", "『>=1,<2』のような版の範囲を入れてください")
        models = requires.get("models")
        if not isinstance(models, list) or not all(
            isinstance(item, str) and item.strip() for item in models
        ):
            err("requires.models が不正です", "モデル名の文字列リスト(空も可)にしてください")
        packages = requires.get("packages")
        if not isinstance(packages, list) or not all(isinstance(item, str) for item in packages):
            err("requires.packages が不正です", "『openpyxl==3.1.5』形式の文字列リストにしてください")
        else:
            allowed, wheels = load_allowed_packages(repo_root)
            spec_pattern = re.compile(r"^([A-Za-z0-9._-]+)==([A-Za-z0-9.]+)$")
            for spec in packages:
                match = spec_pattern.match(spec)
                if not match:
                    err(
                        f"requires.packages の書式が不正です: {spec!r}",
                        "『名前==版』の固定指定だけが使えます",
                    )
                    continue
                canon, version = _canonical(match.group(1)), match.group(2)
                if version not in allowed.get(canon, set()):
                    have = "、".join(sorted(allowed.get(canon, set()))) or "なし"
                    err(
                        f"許可されていない依存です: {spec}",
                        f"院内で使える版は {match.group(1)}: {have}。"
                        "requirements と wheelhouse に無いライブラリは使えません",
                    )
                elif wheels is not None and (canon, version) not in wheels:
                    err(
                        f"wheelhouse に実体がありません: {spec}",
                        "オフライン再構築ができない依存は使えません。版を合わせてください",
                    )

    # permissions
    permissions = data["permissions"]
    if expect_type(permissions, dict, "permissions", "permissions はオブジェクトにしてください"):
        for key in sorted(set(permissions) - {"ollama"}):
            err(f"permissions に未知の項目があります: {key}", "permissions は ollama だけです")
        if not isinstance(permissions.get("ollama"), bool):
            err("permissions.ollama が不正です", "true / false にしてください")
        else:
            ollama = permissions["ollama"]
            models = requires.get("models") if isinstance(requires, dict) else None
            if isinstance(models, list):
                if ollama and not models:
                    err(
                        "permissions.ollama が true なのに requires.models が空です",
                        "使うモデル名を宣言してください(モデル削除に気づくため)",
                    )
                if not ollama and models:
                    err(
                        "permissions.ollama が false なのに requires.models があります",
                        "Ollamaを使わないならモデル宣言を外してください",
                    )

    # timeout
    timeout = data["timeout_seconds"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not (
        TIMEOUT_MIN <= timeout <= TIMEOUT_MAX
    ):
        err(
            f"timeout_seconds が不正です: {timeout!r}",
            f"{TIMEOUT_MIN}〜{TIMEOUT_MAX} の整数にしてください(既定は300)",
        )

    # routing
    routing = data["routing"]
    compiled: list[re.Pattern] = []
    if expect_type(routing, dict, "routing", "routing はオブジェクトにしてください"):
        for key in sorted(set(routing) - {"label", "patterns", "examples", "suggestions"}):
            err(f"routing に未知の項目があります: {key}", "routing は label / patterns / examples / suggestions だけです")
        if not isinstance(routing.get("label"), str) or not routing.get("label", "").strip():
            err("routing.label が不正です", "操作名の短い表示を入れてください")
        patterns = routing.get("patterns")
        if not isinstance(patterns, list) or not patterns or not all(
            isinstance(item, str) for item in patterns
        ):
            err("routing.patterns が不正です", "正規表現の文字列リストを1件以上入れてください")
        else:
            for pattern in patterns:
                try:
                    compiled.append(re.compile(pattern))
                except re.error as error:
                    err(
                        f"routing.patterns の正規表現が不正です: {pattern!r}({error})",
                        "正規表現を修正してください",
                    )
        for label, key in (("examples", "examples"), ("suggestions", "suggestions")):
            items = routing.get(key)
            if not isinstance(items, list) or not items:
                err(f"routing.{label} が不正です", f"{label} を1件以上入れてください")
                continue
            for item in items:
                if key == "examples":
                    text = item if isinstance(item, str) else None
                else:
                    if (
                        not isinstance(item, dict)
                        or not set(item) <= {"title", "subtitle", "content"}
                        or not isinstance(item.get("title"), str)
                        or not isinstance(item.get("content"), str)
                    ):
                        err(
                            f"routing.suggestions の項目が不正です: {item!r}",
                            "各チップは {\"title\": …, \"content\": …} にしてください",
                        )
                        continue
                    text = item["content"]
                if not isinstance(text, str) or not text.strip():
                    err(f"routing.{label} に空の項目があります", "依頼文の例を入れてください")
                elif compiled and not any(p.search(text) for p in compiled):
                    err(
                        f"routing.{label} の『{text}』がどの patterns にも一致しません",
                        "利用者がその文で頼んでもツールへ届きません。patterns か文面を合わせてください",
                    )

    # smoke
    smoke = data["smoke"]
    if expect_type(smoke, dict, "smoke", "smoke はオブジェクトにしてください"):
        for key in sorted(set(smoke) - {"make_sample", "request", "expect"}):
            err(f"smoke に未知の項目があります: {key}", "smoke は make_sample / request / expect だけです")
        make_sample = smoke.get("make_sample")
        if not isinstance(make_sample, str) or not make_sample.endswith(".py"):
            err("smoke.make_sample が不正です", "合成データ生成スクリプト(.py)の相対パスにしてください")
        elif not (package_dir / make_sample).is_file():
            err(
                f"smoke.make_sample のファイルがありません: {make_sample}",
                "パッケージ内に生成スクリプトを入れてください(実データは同梱できません)",
            )
        request = smoke.get("request")
        if not isinstance(request, str):
            err("smoke.request が不正です", "スモーク用 request.json の相対パスにしてください")
        elif not (package_dir / request).is_file():
            err(f"smoke.request のファイルがありません: {request}", "スモーク用の request.json を入れてください")
        else:
            try:
                json.loads((package_dir / request).read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError):
                err(f"smoke.request がJSONとして読めません: {request}", "UTF-8の正しいJSONにしてください")
        expect = smoke.get("expect")
        if not isinstance(expect, dict) or not set(expect) <= {"status", "files_min", "suffixes"}:
            err("smoke.expect が不正です", "expect は status / files_min / suffixes だけです")
        else:
            if expect.get("status", "ok") != "ok":
                err("smoke.expect.status が不正です", "スモークは成功前提です。『ok』にしてください")
            files_min = expect.get("files_min")
            if not isinstance(files_min, int) or isinstance(files_min, bool) or files_min < 0:
                err("smoke.expect.files_min が不正です", "0以上の整数にしてください")
            suffixes = expect.get("suffixes", [])
            if not isinstance(suffixes, list) or not all(
                isinstance(item, str) and EXT_PATTERN.match(item) for item in suffixes
            ):
                err("smoke.expect.suffixes が不正です", "小文字拡張子のリストにしてください")

    return errors, data


# ---------------------------------------------------------------------------
# 6. 静的検査(AST)。完全な防御ではない — docstringの【重要な限界】を参照
# ---------------------------------------------------------------------------
def check_python_sources(package_dir: Path, ollama_allowed: bool) -> list[VerifyError]:
    """実行され得る全Pythonを検査する(main.py・import先・samples・tests を含む全 .py)。"""
    errors: list[VerifyError] = []

    def err(rel: str, line: int, problem: str, fix: str) -> None:
        errors.append(VerifyError(STAGE_STATIC, f"{rel}:{line}: {problem}", fix))

    for path in sorted(package_dir.rglob("*.py")):
        rel = path.relative_to(package_dir).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as error:
            err(rel, error.lineno or 0, f"Pythonの構文エラーです({error.msg})", "構文を直してください")
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                else:
                    names = [node.module or ""] if node.level == 0 else []
                for name in names:
                    root_name = name.split(".")[0]
                    if root_name in FORBIDDEN_IMPORTS_ALWAYS:
                        err(
                            rel, node.lineno,
                            f"禁止されたimportです: {name}",
                            "子プロセス・ネイティブ呼び出し・動的importは使えません(台帳D-9)",
                        )
                    elif root_name in NETWORK_IMPORTS and not ollama_allowed:
                        err(
                            rel, node.lineno,
                            f"通信系のimportです: {name}",
                            "通信は permissions.ollama を true にしてモデルを宣言した"
                            "パッケージだけ、127.0.0.1:11434 に限って許されます",
                        )
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in {"eval", "exec", "compile", "__import__"}:
                    err(rel, node.lineno, f"禁止された呼び出しです: {func.id}", "動的実行は使えません")
                elif (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                    and (
                        func.attr in {"system", "popen", "startfile"}
                        or func.attr.startswith("exec")
                        or func.attr.startswith("spawn")
                    )
                ):
                    err(rel, node.lineno, f"禁止された呼び出しです: os.{func.attr}", "子プロセス起動は使えません")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for pattern, label in SENSITIVE_LITERAL_PATTERNS:
                    if pattern.search(node.value):
                        err(
                            rel, node.lineno,
                            f"機密パスらしき文字列があります: {label}",
                            "data/・templates/・.env・APIキーには触れられません。参照を削除してください",
                        )
                        break
    return errors


# ---------------------------------------------------------------------------
# 全体
# ---------------------------------------------------------------------------
def verify_package(localtool: Path, dest_dir: Path, repo_root: Path = ROOT) -> VerifyResult:
    """`.localtool` を検査し、合格なら dest_dir へ展開した状態で返す。"""
    localtool = Path(localtool)
    dest_dir = Path(dest_dir)

    if not localtool.is_file():
        return VerifyResult(False, [VerifyError(
            STAGE_ZIP, f"ファイルがありません: {localtool}", "パッケージのパスを確認してください",
        )])
    if not zipfile.is_zipfile(localtool):
        return VerifyResult(False, [VerifyError(
            STAGE_ZIP, "ZIPとして読めません",
            ".localtool はフォルダをZIP圧縮して拡張子を変えたものです。作り直してください",
        )])

    dest_dir.mkdir(parents=True, exist_ok=True)
    if any(dest_dir.iterdir()):
        return VerifyResult(False, [VerifyError(
            STAGE_EXTRACT, f"展開先が空ではありません: {dest_dir}", "空のディレクトリを指定してください",
        )])

    with zipfile.ZipFile(localtool) as zf:
        errors, files = check_zip_entries(zf)
        if errors:
            return VerifyResult(False, errors)
        errors = extract_package(zf, zf.infolist(), dest_dir)
        if errors:
            return VerifyResult(False, errors, package_dir=dest_dir)

    # 以降の段階は、直せる点をまとめて返すため全部実行する
    # (オフライン→オンラインの往復回数を減らす。台帳 D-6 の運用)
    errors = check_layout(dest_dir)
    errors += check_manifest(dest_dir)
    tool_errors, tool_json = check_tool_json(dest_dir, repo_root)
    errors += tool_errors
    ollama = bool(
        isinstance(tool_json, dict)
        and isinstance(tool_json.get("permissions"), dict)
        and tool_json["permissions"].get("ollama") is True
    )
    errors += check_python_sources(dest_dir, ollama_allowed=ollama)

    return VerifyResult(not errors, errors, tool_json=tool_json, package_dir=dest_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=".localtool パッケージを検査する")
    parser.add_argument("package", help="検査する .localtool のパス")
    parser.add_argument("--dest", help="展開先(省略時は一時ディレクトリ)")
    args = parser.parse_args()

    dest = Path(args.dest) if args.dest else Path(tempfile.mkdtemp(prefix="toolpack_verify_"))
    result = verify_package(Path(args.package), dest)
    if result.ok:
        info = result.tool_json or {}
        print(f"検査に合格しました: {info.get('id', '?')} {info.get('version', '?')}")
        print(f"展開先: {dest}")
        return 0
    print(f"検査に不合格でした({len(result.errors)}件):")
    for error in result.errors:
        print(str(error))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
