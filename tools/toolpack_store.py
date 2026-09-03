r"""追加ツールの専用保存領域(additional-tools/)と registry の管理。

設計の正本は TOOL_PACKAGE_DECISIONS.md(D-3・D-4)。

- **registry.json だけが有効版の正本。** ディレクトリが置いてあることは有効化を意味しない
- staging → installed の移動は同一ボリューム内の `os.rename`(原子的)。
  途中で落ちても registry 未更新なら不活性なゴミが残るだけで、既存環境は壊れない
- registry の書き換えは一時ファイルへ書いて `os.replace`(NTFS同一ボリュームで原子的)
- **installed/ 内で registry の有効版になっていないディレクトリは自動削除しない**
  (将来のロールバック用の版まで消えるため)。自動掃除は staging の未完了物だけ。
  孤立版は `orphan_versions()` で列挙して doctor が報告する(削除は D-10 確定後)
- **rejected/ には個人情報を含まない診断結果だけを残す。** 失敗した .localtool の
  院内PC側コピーは削除する(原本は持ち込み元のUSBに残っている)

このモジュールは領域とregistryの管理だけを持つ。検証は toolpack_verify、
実行は専用ランナー(工程3以降)、接続はハブ側(工程5)の仕事で、ここには書かない。
テストのためにルートディレクトリを注入できる(実際の additional-tools/ に触れずに試験する)。
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORE_ROOT = ROOT / "additional-tools"

REGISTRY_SCHEMA_VERSION = 1
# 診断ログの保持日数(台帳 D-9)
LOG_RETENTION_DAYS = 14
# staging の未完了物を掃除してよいと見なす経過時間
STAGING_MAX_AGE_SECONDS = 24 * 3600

# 管理操作(追加・更新・無効化・削除・戻す)を1件ずつにするプロセス間ロック。
# registry の書き込み自体は原子的だが、「読む→変える→書く」の全体は原子的でない。
# 画面を2つ開いたり、画面とコマンドを同時に使ったりすると、
# **一方の変更がもう一方に消される**(台帳 D-10)。
# ツール実行のロック(minutes-pipeline-toolpack-run)とは**別にする**。
# 管理操作が利用者のジョブの終わりを待つ必要はないため
ADMIN_MUTEX_NAME = r"Local\minutes-pipeline-toolpack-admin"
ADMIN_LOCK_WAIT_SECONDS = 120

# installed/<id>/<version>/ のパスへそのまま使うため、ここでも書式を強制する
# (検証器 D-5 と同じ規約。パスとして安全な値以外を受け付けない)
ID_PATTERN = re.compile(r"^[a-z0-9_]{3,32}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

# 版ディレクトリの中の決まった構成(台帳 D-3)
SOURCE_DIR = "source"
SOURCE_NAME = "package.localtool"
PACKAGE_DIR = "package"
GENERATED_DIR = "generated"
INSTALL_JSON = "install.json"

# 承認の決まり(台帳 D-14)。**何人で承認・取消するかは変えられる。**
# 運用が回らないなら人数を下げられるようにしておく。
# ファイルが無い・壊れているときは既定へ落とす。
# **壊れたファイルで承認が緩くなってはいけない**ので、下限は1人
POLICY_FILE = "approval_policy.json"
DEFAULT_APPROVALS_REQUIRED = 2
DEFAULT_REVOCATIONS_REQUIRED = 2


class StoreError(RuntimeError):
    """保存領域・registry 操作の失敗。"""


class AdminBusy(StoreError):
    """他の管理操作が動いているため実行できない。"""


@contextlib.contextmanager
def admin_lock(wait_seconds: int | None = None):
    """管理操作を1件ずつにする。**registry を変える処理はこの中で行う。**

    「読む→変える→書く」の途中で別の操作が割り込むと、
    片方の変更が消える。画面を2つ開いた場合や、画面とコマンドを
    同時に使った場合に起きる(台帳 D-10)。
    """
    from local_tool_bridge.job_lock import JobLock

    # 待ち時間は**呼び出し時**に読む(既定値を定義時に固定すると差し替えられない)
    wait = ADMIN_LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds
    lock = JobLock(ADMIN_MUTEX_NAME)
    if not lock.acquire(timeout=wait):
        lock.close()
        raise AdminBusy(
            "他の管理操作が実行中です。終わるのを待ってからやり直してください"
            "(管理画面を2つ開いていないか確かめてください)"
        )
    try:
        yield lock
    finally:
        lock.release()
        lock.close()


@dataclass(frozen=True)
class ApprovalPolicy:
    """承認の決まり。**人数は運用で変えられる**(台帳 D-14)。"""

    approvals_required: int = DEFAULT_APPROVALS_REQUIRED
    revocations_required: int = DEFAULT_REVOCATIONS_REQUIRED
    # 承認できる人の一覧。**空なら自由入力**(打ち間違いで別人になりうる)。
    # 名前を並べておけば選ぶだけで済み、事故が減る
    approvers: tuple[str, ...] = ()
    # 読めなかった・直した点。**黙って既定へ落とさない**
    problems: tuple[str, ...] = ()
    # 設定ファイルが置いてあるか(無いのは正常。**壊れているのとは別**)
    present: bool = False

    @property
    def usable(self) -> bool:
        """この決まりで承認してよいか。

        **壊れた設定を「自由入力」として扱わない。**
        `{"approvers": "壊れた値"}` のとき既定へ落とすと、
        名前を絞っていたはずが**誰でも承認できる**状態になる。
        置いてある設定が読めないなら、承認そのものを断るのが安全側。
        """
        return not (self.present and self.problems)


def load_policy(path: Path) -> ApprovalPolicy:
    """承認の決まりを読む。読めなければ既定(2人/2人・自由入力)。

    **壊れたファイルで承認が緩くなってはいけない。** 人数の下限は1人とし、
    直した点は `problems` で持ち帰る(表示する側が黙って捨てない)。
    """
    if not Path(path).is_file():
        return ApprovalPolicy()   # 置いていないのは正常(2人/2人・自由入力)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("トップレベルがオブジェクトではありません")
    except (ValueError, OSError, UnicodeDecodeError) as error:
        return ApprovalPolicy(
            present=True, problems=(f"{POLICY_FILE} を読めません({error})",)
        )

    problems: list[str] = []

    def count(key: str, default: int) -> int:
        value = data.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            problems.append(f"{key} が不正です({value!r})")
            return default
        return value

    approvers = data.get("approvers", [])
    if not isinstance(approvers, list) or not all(isinstance(x, str) for x in approvers):
        # **自由入力へ落とさない。** 落とすと、絞っていたはずが誰でも承認できる
        problems.append(f"approvers が不正です({approvers!r})")
        approvers = []
    return ApprovalPolicy(
        approvals_required=count("approvals_required", DEFAULT_APPROVALS_REQUIRED),
        revocations_required=count("revocations_required", DEFAULT_REVOCATIONS_REQUIRED),
        approvers=tuple(name.strip() for name in approvers if name.strip()),
        problems=tuple(problems),
        present=True,
    )


def _distinct(votes: list[dict]) -> list[str]:
    """賛成した人の名前(重複を除く)。**同じ人が2回押しても1件**(台帳 D-14)。"""
    seen: list[str] = []
    for vote in votes or []:
        name = str((vote or {}).get("name") or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _apply_decision(record: dict, revoke: bool) -> None:
    """票が人数に届いたときに、決まった状態を書き込む。

    決まったら**反対側の票だけ**片付ける。決め手になった票は残す
    (誰で決まったかを見せる・巻き戻せるようにするため)。
    """
    key = "revocations" if revoke else "approvals"
    record["approved"] = not revoke
    history = list(record.get("approval_history") or [])
    history.append({
        "event": "revoked" if revoke else "approved",
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "by": _distinct(record.get(key) or []),
        "package_hash": str(record.get("package_hash") or ""),
    })
    record["approval_history"] = history
    record.pop("approvals" if revoke else "revocations", None)


def pending_decision(record: dict, approved: bool, policy: "ApprovalPolicy") -> bool:
    """**もう人数に届いているのに、まだ確定していない**状態か(台帳 D-14)。

    人数を途中で下げると起きる。2人必要なときに1票入れたあと1人へ下げると、
    その1票で足りているのに未承認のまま止まり、
    **入れた本人はもう押せない**(同じ人は1件として数えるため)。
    確定は投票のときにしか起きないので、ここで見つけて別に確定させる。

    逆に、**決まったものを設定変更で覆さない**(人数を上げても承認済みは承認済み)。
    黙って承認が外れるほうが危ない。
    """
    if approved:
        votes = record.get("revocations") or []
        required = policy.revocations_required
    else:
        votes = record.get("approvals") or []
        required = policy.approvals_required
    return bool(votes) and len(_distinct(votes)) >= required


def is_approved(entry: dict, version: str | None = None) -> bool:
    """その版が承認済みか(台帳 D-14)。

    **承認は版ごとに持つ**のが正しい(1.0.0 は承認済み・1.1.0 は未承認、が起こる)。
    いまの registry はツール単位の `status` しか持っていないため、
    版ごとの記録があればそちらを優先し、無ければ `status` を見る。
    D-14 を実装するときに版ごとへ寄せる(移行のための入口をここに1本だけ置く)。
    """
    versions = entry.get("versions") or {}
    if not isinstance(versions, dict):
        return entry.get("status") == "approved"
    target = version or entry.get("active_version")
    record = versions.get(target)
    if isinstance(record, dict) and "approved" in record:
        return bool(record["approved"])
    # この項目が版ごとの承認を使っているなら、**印の無い版は未承認**。
    # ここでツール単位の status へ落ちると、承認済みのツールへ足した
    # 新しい版が、承認されていないのに承認済みとして扱われる
    if any(isinstance(item, dict) and "approved" in item for item in versions.values()):
        return False
    return entry.get("status") == "approved"


@dataclass(frozen=True)
class OrphanVersion:
    """registry の有効版になっていない installed 内ディレクトリ(報告用)。"""

    tool_id: str
    version: str
    path: Path


class ToolpackStore:
    """additional-tools/ 一式の操作。root を差し替えればテストは実領域に触れない。"""

    def __init__(self, root: Path = DEFAULT_STORE_ROOT) -> None:
        self.root = Path(root)
        self.incoming = self.root / "incoming"
        self.staging = self.root / "staging"
        self.installed = self.root / "installed"
        self.rejected = self.root / "rejected"
        self.logs = self.root / "logs"
        self.registry_dir = self.root / "registry"
        self.registry_path = self.registry_dir / "registry.json"

    # ------------------------------------------------------------------
    # レイアウト
    # ------------------------------------------------------------------
    def ensure_layout(self) -> None:
        """構成ディレクトリを作る(何度呼んでも安全)。"""
        for directory in (
            self.incoming, self.staging, self.installed,
            self.rejected, self.logs, self.registry_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def version_dir(self, tool_id: str, version: str) -> Path:
        self._require_safe(tool_id, version)
        return self.installed / tool_id / version

    def package_dir(self, tool_id: str, version: str) -> Path:
        return self.version_dir(tool_id, version) / PACKAGE_DIR

    def generated_dir(self, tool_id: str, version: str) -> Path:
        return self.version_dir(tool_id, version) / GENERATED_DIR

    def source_path(self, tool_id: str, version: str) -> Path:
        return self.version_dir(tool_id, version) / SOURCE_DIR / SOURCE_NAME

    def install_json_path(self, tool_id: str, version: str) -> Path:
        return self.version_dir(tool_id, version) / INSTALL_JSON

    @staticmethod
    def _require_safe(tool_id: str, version: str) -> None:
        if not ID_PATTERN.match(tool_id):
            raise StoreError(f"ツールIDが規約に合いません: {tool_id!r}")
        if not VERSION_PATTERN.match(version):
            raise StoreError(f"版の書式が不正です: {version!r}")

    # ------------------------------------------------------------------
    # registry(唯一の有効化スイッチ)
    # ------------------------------------------------------------------
    def load_registry(self) -> dict:
        """registry を読む。無ければ空の骨組みを返す(ファイルは作らない)。"""
        if not self.registry_path.is_file():
            return {"schema_version": REGISTRY_SCHEMA_VERSION, "tools": {}}
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise StoreError(f"registry.json が読めません: {error}") from error
        if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
            raise StoreError("registry.json の形式が不正です(tools がありません)")
        return data

    def _write_registry(self, data: dict) -> None:
        """一時ファイルへ書いて os.replace(同一ボリュームで原子的)。"""
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        temp = self.registry_dir / f".registry-{uuid.uuid4().hex}.tmp"
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        try:
            os.replace(temp, self.registry_path)
        except OSError:
            temp.unlink(missing_ok=True)
            raise

    def register(
        self, tool_id: str, version: str, *, package_hash: str, extra: dict | None = None
    ) -> None:
        """新しいツールを未承認として登録する(**追加のみ**)。

        registry 内の id 重複は登録前に拒否する(台帳 D-4)。
        同じ id の新しい版を入れるのは**更新**であり、別の道を通る
        (`add_version` → `set_active_version`。台帳 D-10)。
        """
        self._require_safe(tool_id, version)
        data = self.load_registry()
        if tool_id in data["tools"]:
            raise StoreError(
                f"同じIDのツールが登録済みです: {tool_id}"
                "(入れ替えるなら更新として実行してください)"
            )
        entry = {
            "active_version": version,
            "status": "unapproved",  # 新規は必ず未承認から始まる(台帳 D-12)
            "versions": {
                version: {
                    "package_hash": package_hash,
                    "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "approved": False,  # 新規は必ず未承認から始まる(D-12・D-14)
                }
            },
        }
        if extra:
            entry["versions"][version].update(extra)
        data["tools"][tool_id] = entry
        self._write_registry(data)

    def add_version(
        self, tool_id: str, version: str, *, package_hash: str, extra: dict | None = None
    ) -> None:
        """既存ツールへ**版だけ**を足す(有効版は切り替えない。台帳 D-10)。

        更新は「新しい版を用意し終えてから、最後に1回だけ切り替える」形にする。
        ここで切り替えないので、途中で失敗しても旧版が動いたまま残る。
        """
        self._require_safe(tool_id, version)
        data = self.load_registry()
        entry = data["tools"].get(tool_id)
        if entry is None:
            raise StoreError(f"登録されていないツールです: {tool_id}")
        versions = entry.setdefault("versions", {})
        if version in versions:
            raise StoreError(f"同じ版が既にあります: {tool_id} {version}")
        record = {
            "package_hash": package_hash,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "approved": False,   # 承認は版ごと。**新しい版は必ず未承認から**(D-14)
        }
        if extra:
            record.update(extra)
        versions[version] = record
        self._write_registry(data)

    def drop_version(self, tool_id: str, version: str) -> None:
        """版の登録だけを取り消す(**更新失敗時のロールバック用**)。

        有効版は落とさない。ファイル実体には触れない。
        """
        data = self.load_registry()
        entry = data["tools"].get(tool_id)
        if entry is None:
            return
        if entry.get("active_version") == version:
            raise StoreError(f"有効な版は外せません: {tool_id} {version}")
        entry.get("versions", {}).pop(version, None)
        self._write_registry(data)

    def set_active_version(self, tool_id: str, version: str) -> str:
        """有効版を切り替え、**切り替える前の版**を返す(台帳 D-10)。

        これが更新・前の版へ戻すの**唯一の切り替え点**である。
        一時ファイル + os.replace で書くので、途中の状態が残らない。
        """
        self._require_safe(tool_id, version)
        data = self.load_registry()
        entry = data["tools"].get(tool_id)
        if entry is None:
            raise StoreError(f"登録されていないツールです: {tool_id}")
        if version not in entry.get("versions", {}):
            have = "、".join(sorted(entry.get("versions", {}))) or "なし"
            raise StoreError(f"その版は登録されていません: {version}(ある版: {have})")
        previous = entry.get("active_version", "")
        entry["active_version"] = version
        # 承認は版ごと。**有効版が変われば、ツール単位の控えも変わる**
        entry["status"] = "approved" if is_approved(entry, version) else "unapproved"
        self._write_registry(data)
        return previous

    # ------------------------------------------------------------------
    # 承認(台帳 D-14)。**版ごとに持つ**
    # ------------------------------------------------------------------
    @property
    def policy_path(self) -> Path:
        return self.registry_dir / POLICY_FILE

    def load_policy(self) -> ApprovalPolicy:
        return load_policy(self.policy_path)

    def approval_state(self, tool_id: str, version: str | None = None) -> dict:
        """その版の承認の様子。**画面にもコマンドにも同じものを見せる。**"""
        entry = self.load_registry()["tools"].get(tool_id) or {}
        target = version or str(entry.get("active_version") or "")
        record = (entry.get("versions") or {}).get(target) or {}
        policy = self.load_policy()
        return {
            "version": target,
            "approved": is_approved(entry, target),
            "approvals": _distinct(record.get("approvals") or []),
            "revocations": _distinct(record.get("revocations") or []),
            "approvals_required": policy.approvals_required,
            "revocations_required": policy.revocations_required,
            "history": list(record.get("approval_history") or []),
            # 人数を下げたあとなど、**もう届いているのに未確定**の状態
            "ready": pending_decision(record, is_approved(entry, target), policy),
        }

    def add_vote(self, tool_id: str, version: str, name: str, *, revoke: bool) -> dict:
        """承認(または取消)へ1票入れる。**決まった人数に届いたら状態を変える。**

        返すのは変えたあとの様子。`decided` が True なら状態が変わったので、
        呼んだ側は**表示名の作り直しと貼り直し**まで行う必要がある。
        """
        name = str(name or "").strip()
        if not name:
            raise StoreError("承認する人の名前が要ります")
        self._require_safe(tool_id, version)

        data = self.load_registry()
        entry = data["tools"].get(tool_id)
        if entry is None:
            raise StoreError(f"登録されていないツールです: {tool_id}")
        record = (entry.get("versions") or {}).get(version)
        if record is None:
            raise StoreError(f"その版は登録されていません: {tool_id} {version}")

        # **投票前の版の記録を丸ごと退避する。**
        # 決まったときに反対側の票を片付けるので、「入れた1票だけ消す」方式では
        # 元へ戻しきれない(消した票が復活しない)。まるごと書き戻すほうが確実
        snapshot = copy.deepcopy(record)

        policy = self.load_policy()
        if not policy.usable:
            raise StoreError(
                f"承認の設定({POLICY_FILE})が読めないため、承認も取り消しもできません。\n"
                + "\n".join(f"  - {item}" for item in policy.problems)
                + "\n設定を直すか、ファイルごと消してください(消せば2人/2人・自由入力に戻ります)"
            )
        if policy.approvers and name not in policy.approvers:
            raise StoreError(
                f"承認できる人に {name} がいません"
                f"(登録されている人: {'、'.join(policy.approvers)})"
            )

        approved_now = is_approved(entry, version)
        if revoke and not approved_now:
            raise StoreError(f"{tool_id} {version} は承認されていません(取り消せません)")
        if not revoke and approved_now:
            raise StoreError(f"{tool_id} {version} は既に承認済みです")

        key = "revocations" if revoke else "approvals"
        required = (
            policy.revocations_required if revoke else policy.approvals_required
        )
        votes = list(record.get(key) or [])
        if name in _distinct(votes):
            raise StoreError(f"{name} は既に入れています(同じ人は1件として数えます)")
        vote = {"name": name, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if not revoke:
            # **承認したときの中身を残す。** あとで差し替わったら気づける
            vote["package_hash"] = str(record.get("package_hash") or "")
        votes.append(vote)
        record[key] = votes

        if len(_distinct(votes)) >= required:
            _apply_decision(record, revoke)

        # ツール単位の status は有効版から導く控え。既存の読み手のために合わせる
        active = str(entry.get("active_version") or "")
        entry["status"] = "approved" if is_approved(entry, active) else "unapproved"
        self._write_registry(data)
        state = self.approval_state(tool_id, version)
        state["snapshot"] = snapshot   # 失敗したときはこれを丸ごと書き戻す
        return state

    def settle_pending(self, tool_id: str, version: str) -> dict | None:
        """**もう人数に届いている票**を確定させる(台帳 D-14)。

        人数を途中で下げると、確定しないまま止まることがある。
        確定は投票のときにしか起きず、入れた本人はもう押せないため、
        新しい票なしで確定できる道をここに用意する。
        確定するものが無ければ None。
        """
        self._require_safe(tool_id, version)
        data = self.load_registry()
        entry = data["tools"].get(tool_id)
        if entry is None:
            raise StoreError(f"登録されていないツールです: {tool_id}")
        record = (entry.get("versions") or {}).get(version)
        if record is None:
            raise StoreError(f"その版は登録されていません: {tool_id} {version}")

        policy = self.load_policy()
        if not policy.usable:
            raise StoreError(f"承認の設定({POLICY_FILE})が読めません")
        approved_now = is_approved(entry, version)
        if not pending_decision(record, approved_now, policy):
            return None

        snapshot = copy.deepcopy(record)
        _apply_decision(record, revoke=approved_now)
        active = str(entry.get("active_version") or "")
        entry["status"] = "approved" if is_approved(entry, active) else "unapproved"
        self._write_registry(data)
        state = self.approval_state(tool_id, version)
        state["snapshot"] = snapshot
        return state

    def restore_record(self, tool_id: str, version: str, snapshot: dict) -> None:
        """版の記録を退避したものへ丸ごと戻す(**失敗時の巻き戻し用**)。

        票を入れたあとで反映に失敗したときに使う。
        「入れた1票だけ消す」のでは、決まったときに片付けた反対側の票が
        戻らない。**投票前の姿へそのまま書き戻す**のが確実である。
        """
        data = self.load_registry()
        entry = data["tools"].get(tool_id)
        if entry is None:
            return
        versions = entry.get("versions") or {}
        if version not in versions:
            return
        versions[version] = copy.deepcopy(snapshot)
        active = str(entry.get("active_version") or "")
        entry["status"] = "approved" if is_approved(entry, active) else "unapproved"
        self._write_registry(data)

    def set_enabled(self, tool_id: str, enabled: bool) -> None:
        """一時的な無効化・有効化(台帳 D-10)。**版もファイルも残す。**"""
        data = self.load_registry()
        entry = data["tools"].get(tool_id)
        if entry is None:
            raise StoreError(f"登録されていないツールです: {tool_id}")
        entry["enabled"] = bool(enabled)
        self._write_registry(data)

    def unregister(self, tool_id: str) -> None:
        """登録を取り消す。**ファイル実体は消さない**(台帳 D-10)。

        残ったファイルは doctor が「登録の無い残置」として報告する。
        実体の削除は別操作(`purge_files`)にしてある。外部バックアップ(D-17)が
        未決のうちに消すと戻せないため。
        """
        data = self.load_registry()
        if tool_id in data["tools"]:
            del data["tools"][tool_id]
            self._write_registry(data)

    def purge_files(self, tool_id: str, version: str | None = None) -> list[Path]:
        """ファイル実体を消す(**明示的に呼ばれたときだけ**。台帳 D-10)。

        登録が残っているものは消さない。消した場所を返す。
        """
        data = self.load_registry()
        entry = data["tools"].get(tool_id)
        if entry is not None:
            if version is None or version in entry.get("versions", {}):
                raise StoreError(
                    f"登録が残っています: {tool_id}"
                    "(先に登録を外してください。動いているものは消しません)"
                )
        removed: list[Path] = []
        base = self.installed / tool_id
        targets = [base] if version is None else [base / version]
        for target in targets:
            self._require_safe(tool_id, version or "0.0.0")
            if target.is_dir():
                shutil.rmtree(target)
                removed.append(target)
        # 版を消して空になったら、id のディレクトリも片付ける
        if version is not None and base.is_dir() and not any(base.iterdir()):
            base.rmdir()
            removed.append(base)
        return removed

    def active_tools(self) -> dict[str, dict]:
        """有効なツール一覧。{tool_id: {"version": 版, "entry": registry項目}}。

        **無効化されたツールは含めない**(台帳 D-10)。
        `enabled` が無い古い項目は有効として扱う(後から足した項目のため)。
        """
        result: dict[str, dict] = {}
        for tool_id, entry in self.load_registry()["tools"].items():
            if entry.get("enabled", True) is False:
                continue
            version = entry.get("active_version")
            if isinstance(version, str) and VERSION_PATTERN.match(version):
                result[tool_id] = {"version": version, "entry": entry}
        return result

    # ------------------------------------------------------------------
    # staging → installed(原子的な配置)
    # ------------------------------------------------------------------
    def new_staging_dir(self) -> Path:
        """展開・検査用の一時領域を staging/ 配下へ作る。"""
        self.staging.mkdir(parents=True, exist_ok=True)
        path = self.staging / uuid.uuid4().hex
        path.mkdir()
        return path

    def promote(self, staging_dir: Path, tool_id: str, version: str) -> Path:
        """検査済みの staging ディレクトリを installed/<id>/<version>/ へ移す。

        同一ボリューム内の os.rename なので原子的。ここで落ちても registry が
        未更新なら不活性(有効化は register が行う)。
        """
        self._require_safe(tool_id, version)
        staging_dir = Path(staging_dir)
        if not staging_dir.is_dir():
            raise StoreError(f"stagingディレクトリがありません: {staging_dir}")
        target = self.version_dir(tool_id, version)
        if target.exists():
            raise StoreError(
                f"同じ版が配置済みです: {tool_id} {version}"
                "(上書きしません。版番号を上げてください)"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staging_dir, target)
        return target

    # ------------------------------------------------------------------
    # 後始末(成功時・失敗時)
    # ------------------------------------------------------------------
    def finish_success(self, incoming_path: Path | None, staging_dir: Path | None) -> None:
        """成功後は incoming と staging を削除する(台帳 D-3)。"""
        if incoming_path is not None:
            Path(incoming_path).unlink(missing_ok=True)
        if staging_dir is not None and Path(staging_dir).is_dir():
            shutil.rmtree(staging_dir, ignore_errors=True)

    def reject(
        self,
        localtool_path: Path | None,
        staging_dir: Path | None,
        *,
        tool_hint: str,
        diagnosis: str,
    ) -> Path:
        """失敗を記録して後始末する。

        rejected/ には診断結果のテキストだけを残す。呼び出し側は diagnosis に
        本文・氏名・元ファイル名を入れてはいけない(台帳 D-9 の記録禁止項目)。
        失敗した .localtool の院内コピーと展開ツリーは削除する。
        """
        self.rejected.mkdir(parents=True, exist_ok=True)
        hint = re.sub(r"[^a-z0-9_-]", "_", tool_hint.lower())[:40] or "unknown"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = self.rejected / f"{stamp}_{hint}_{uuid.uuid4().hex[:8]}.txt"
        path.write_text(diagnosis, encoding="utf-8")
        if localtool_path is not None:
            Path(localtool_path).unlink(missing_ok=True)
        if staging_dir is not None and Path(staging_dir).is_dir():
            shutil.rmtree(staging_dir, ignore_errors=True)
        return path

    # ------------------------------------------------------------------
    # 掃除と報告
    # ------------------------------------------------------------------
    def sweep_staging(self, older_than_seconds: int = STAGING_MAX_AGE_SECONDS) -> list[Path]:
        """staging の未完了物だけを自動掃除する(installed には触らない)。"""
        removed: list[Path] = []
        if not self.staging.is_dir():
            return removed
        limit = time.time() - older_than_seconds
        for child in self.staging.iterdir():
            try:
                if child.stat().st_mtime <= limit:
                    shutil.rmtree(child, ignore_errors=True)
                    removed.append(child)
            except OSError:
                continue
        return removed

    def sweep_logs(self, retention_days: int = LOG_RETENTION_DAYS) -> list[Path]:
        """診断ログを保持期限(既定14日)で削除する。"""
        removed: list[Path] = []
        if not self.logs.is_dir():
            return removed
        limit = time.time() - retention_days * 24 * 3600
        for child in self.logs.rglob("*"):
            try:
                if child.is_file() and child.stat().st_mtime <= limit:
                    child.unlink()
                    removed.append(child)
            except OSError:
                continue
        return removed

    def orphan_versions(self) -> list[OrphanVersion]:
        """**台帳に載っていない** installed 内ディレクトリを列挙する。

        更新で残した旧版や、無効化中のツールは台帳に載っているので**含めない**。
        それらは意図して残しているものであり、「残置」として報告すると
        本当の取りこぼしが埋もれる(台帳 D-10)。

        **削除はしない。** doctor が報告するための材料(台帳 D-3)。
        """
        orphans: list[OrphanVersion] = []
        if not self.installed.is_dir():
            return orphans
        tools = self.load_registry()["tools"]
        for tool_dir in sorted(self.installed.iterdir()):
            if not tool_dir.is_dir():
                continue
            known = set((tools.get(tool_dir.name) or {}).get("versions", {}))
            for version_dir in sorted(tool_dir.iterdir()):
                if not version_dir.is_dir():
                    continue
                if version_dir.name not in known:
                    orphans.append(
                        OrphanVersion(tool_dir.name, version_dir.name, version_dir)
                    )
        return orphans

    def retained_versions(self) -> list[OrphanVersion]:
        """台帳には載っているが、いま動いていない版(更新で残した旧版など)。

        戻せる版の一覧であり、**取りこぼしではない**。
        """
        kept: list[OrphanVersion] = []
        tools = self.load_registry()["tools"]
        for tool_id, entry in sorted(tools.items()):
            active = entry.get("active_version")
            for version in sorted(entry.get("versions", {})):
                if version == active:
                    continue
                kept.append(OrphanVersion(tool_id, version, self.version_dir(tool_id, version)))
        return kept
