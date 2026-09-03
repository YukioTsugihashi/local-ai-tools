"""Open WebUI のナレッジ「院内ツールヘルプ」へ運用ドキュメントとコードを同期する。

リポジトリのヘルプ・ルール・マップ(md)に加えて**プログラム本体**をナレッジへ登録する。
利用者はモデル「使い方ヘルプ」(tools/open_webui_help_pipe.py)を選んで聞くだけ。
答えられるようにしたい質問は3種類:

  1. 「議事録を作りたい」        → 手順
  2. 「こういうことしたいけどできる?」→ 既にある機能を探して可否を答える
  3. 「どこに何がある?」        → ファイル・関数の場所(コードを載せているため答えられる)

**中身が変わったファイルだけ入れ直す。** 前回同期した中身のsha256を
`logs/help_sync_state.json` に持ち、一致するものは飛ばす。全件やり直すと
119件ぶんの埋め込みが毎回走って重く、こまめに流せないため(2026-08-17)。

使い方:
  python tools\\open_webui_help_sync.py            差分の表示(dry-run。変更なし)
  python tools\\open_webui_help_sync.py --apply    変わったファイルだけ同期する
  python tools\\open_webui_help_sync.py --apply --full   変わっていなくても全件やり直す
  python tools\\open_webui_help_sync.py --apply --reset
      既存のナレッジと同期済みファイルを消してから作り直す。
      紐付けが壊れて "Duplicate content detected" が出るときはこれ。
  python tools\\open_webui_help_sync.py --ask "議事録を作るには?"   動作確認の質問

認証・接続先は open_webui_deploy.py と同じ(localhost限定・キー非表示)。
埋め込みがOllama以外(HFダウンロード型)だとオフラインで死ぬため、必ず検査する。

**埋め込みはOllama(bge-m3)を通る。** Ollamaの更新・再起動中に --apply すると
途中で失敗して半端な状態が残るので、Ollamaが安定してから実行すること。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from open_webui_deploy import (  # noqa: E402
    ApiClient,
    DeployError,
    load_api_key,
    resolve_base_url,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_NAME = "院内ツールヘルプ"
# 利用者が選ぶモデル。中身は tools/open_webui_help_pipe.py(検索は自前で行う)
HELP_PIPE_ID = "hospital_help_pipe"
# 旧方式(ナレッジをモデルへ紐付ける)の残骸。同じ表示名で並ぶと利用者が
# 壊れたほうを選んでしまうため、見つけたら消す
OBSOLETE_MODEL_ID = "hospital_help"
# 前回同期した中身のsha256。gitに載せない場所(logs/)へ置く
STATE_PATH = REPO_ROOT / "logs" / "help_sync_state.json"
# 定期実行(画面が無い)の記録
LOG_PATH = REPO_ROOT / "logs" / "help_sync.log"
LOG_MAX_BYTES = 1_000_000

# 同期対象(存在するものだけ登録する)。
# コード本体も載せる。「どのファイルにあるか」「今できることは何か」に
# 答えるには、説明文書だけでは足りないため(2026-08-17に対象を拡張)。
TARGET_GLOBS = [
    # 使い方・運用ルール
    "*.md",
    ".kilo/rules/*.md",
    ".kilo/agents/*.md",
    "templates/gui/README.md",
    # プログラム本体
    "tools/*.py",
    "text-processing-bridge/app/*.py",
    "text-processing-bridge/tests/*.py",
    "local_tool_bridge/*.py",
    "local_tool_bridge/connectors/*.py",
    ".kilo/plugin/*.ts",
    ".kilo/plugin/*.mjs",
    "kilo.jsonc",
]

# 何があっても載せないもの。globを増やしたときの事故を止める最後の砦。
# data/・templates/ は実在の会議データ・議事録・名簿。.env は認証情報。
FORBIDDEN_DIRS = {
    "data",
    "input",
    "output",
    "work",
    "logs",
    ".venv",
    ".git",
    "node_modules",
    "__pycache__",
    "offline-docs",
    "wheelhouse-win",
    "wheelhouse-linux",
}
FORBIDDEN_NAMES = {".env"}
# templates/ 配下で唯一許すもの(書式ひな型の説明。合成データのみ)
TEMPLATES_ALLOWLIST = {"templates/gui/README.md"}

def unwrap_items(payload: object) -> list:
    """
    一覧APIの応答を配列にそろえる。

    Open WebUIは版によって ``[...]`` と ``{"items": [...]}`` の両方を返す。
    決め打ちすると「0件」と誤読し、**同期のたびに重複を作る**。
    実際に「院内ツールヘルプ」が2つでき、モデルは空のほうを見ていた
    (2026-08-17に判明。回答が毎回空になっていた原因)。
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def is_safe_target(path: Path, root: Path = REPO_ROOT) -> bool:
    """ナレッジへ載せてよいファイルか。機密・巨大なものを確実に外す。"""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if relative.name in FORBIDDEN_NAMES:
        return False
    posix = relative.as_posix()
    parts = relative.parts[:-1]
    if any(part in FORBIDDEN_DIRS for part in parts):
        return False
    if parts and parts[0] == "templates" and posix not in TEMPLATES_ALLOWLIST:
        return False
    return True


def collect_targets(root: Path = REPO_ROOT) -> list[Path]:
    """対象の実在リスト(重複なし・glob順)。機密は is_safe_target が落とす。"""
    found: list[Path] = []
    for pattern in TARGET_GLOBS:
        found.extend(sorted(root.glob(pattern)))
    unique: list[Path] = []
    for path in found:
        if path.is_file() and path not in unique and is_safe_target(path, root):
            unique.append(path)
    return unique


def upload_name(path: Path, root: Path = REPO_ROOT) -> str:
    """ナレッジ内で一意になるファイル名(相対パスを__で平坦化)。"""
    return str(path.relative_to(root)).replace("\\", "/").replace("/", "__")


def multipart_body(filename: str, content: bytes) -> tuple[bytes, str]:
    """urllib用のmultipart/form-data本文とContent-Type。"""
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: text/markdown\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, f"multipart/form-data; boundary={boundary}"


def upload_file(client: ApiClient, filename: str, content: bytes) -> dict:
    import json as _json
    import urllib.error
    import urllib.request

    body, content_type = multipart_body(filename, content)
    request = urllib.request.Request(
        f"{client.base_url}/api/v1/files/?process=true",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {client.api_key}",
            "Content-Type": content_type,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return _json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise DeployError(
            f"ファイルのアップロードに失敗: {filename} (HTTP {error.code})\n"
            f"詳細: {error.read().decode('utf-8', errors='replace')[:300]}"
        )


def all_knowledge(client: ApiClient) -> list[dict]:
    return [k for k in unwrap_items(client.request("GET", "/api/v1/knowledge/")) if isinstance(k, dict)]


def all_files(client: ApiClient, max_pages: int = 60) -> list[dict]:
    """
    アップロード済みファイルを全ページ集める。

    /api/v1/files/ は**50件ずつ**しか返さない。1ページ目だけ見ると
    掃除が途中で終わり、消し残りが積み上がる(実際421件まで増えていた)。
    content=false を付けて本文を落とす。付けないと全ファイルの全文が
    毎ページ流れてきて、変更が無い回でも1分近くかかる。
    """
    collected: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        items = unwrap_items(
            client.request(
                "GET",
                f"/api/v1/files/?page={page}&content=false",
                none_on=(400, 404, 422),
            )
        )
        if not items:
            break
        fresh = [f for f in items if isinstance(f, dict) and f.get("id") not in seen]
        if not fresh:
            break
        for item in fresh:
            seen.add(item["id"])
        collected.extend(fresh)
    return collected


def find_knowledge(client: ApiClient) -> dict | None:
    """名前が一致するナレッジの詳細(ファイル一覧つき)。無ければNone。"""
    for entry in all_knowledge(client):
        if entry.get("name") == KNOWLEDGE_NAME and entry.get("id"):
            detail = client.request(
                "GET", f"/api/v1/knowledge/{entry['id']}", none_on=(400, 401, 404)
            )
            return detail if isinstance(detail, dict) else entry
    return None


def embedding_status(client: ApiClient) -> str:
    for path in ("/api/v1/retrieval/embedding", "/api/v1/retrieval/config"):
        try:
            config = client.request("GET", path, none_on=(404, 405))
        except DeployError:
            continue
        if isinstance(config, dict):
            engine = str(
                config.get("RAG_EMBEDDING_ENGINE")
                or config.get("embedding_engine")
                or config.get("EMBEDDING_ENGINE")
                or "?"
            )
            model = str(
                config.get("RAG_EMBEDDING_MODEL")
                or config.get("embedding_model")
                or config.get("EMBEDDING_MODEL")
                or "?"
            )
            if engine != "?" or model != "?":
                return f"{engine} / {model}"
    return "不明(取得失敗。Open WebUI管理画面の ドキュメント設定 で直接確認してください)"


def record_name(item: dict) -> str:
    return str((item.get("meta") or {}).get("name") or item.get("filename") or "")


def linked_from(records: list[dict], knowledge_id: str) -> dict[str, str]:
    """
    ナレッジへ実際に入っているファイルの {登録名: ファイルID}。

    ナレッジ詳細(`GET /api/v1/knowledge/{id}`)の files は常にnullで使えないため、
    ファイル一覧側の `meta.collection_name` がナレッジIDになっているもので判定する。
    """
    found: dict[str, str] = {}
    for item in records:
        if (item.get("meta") or {}).get("collection_name") != knowledge_id:
            continue
        name = record_name(item)
        if name and item.get("id"):
            found[name] = str(item["id"])
    return found


def ids_by_name(records: list[dict]) -> dict[str, list[str]]:
    """
    登録名ごとの、サーバに残っている全ファイルID。

    ナレッジから外れても実体は残る。Open WebUIの重複判定は利用者の全ファイルを
    見るため、この置き去りが残っていると同じ内容を入れ直せない
    ("Duplicate content detected"。2026-08-17に6件が入らなくなった)。
    """
    found: dict[str, list[str]] = {}
    for item in records:
        name = record_name(item)
        if name and item.get("id"):
            found.setdefault(name, []).append(str(item["id"]))
    return found


def linked_files(client: ApiClient, knowledge_id: str) -> dict[str, str]:
    return linked_from(all_files(client), knowledge_id)


def content_digest(path: Path) -> str:
    """ファイルの中身のsha256。改行やBOMも含めた生バイトで見る。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_state(path: Path = STATE_PATH) -> dict[str, str]:
    """前回同期した {登録名: sha256}。無い・壊れている場合は空(=全件やり直し)。"""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = loaded.get("files") if isinstance(loaded, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {str(k): str(v) for k, v in entries.items() if isinstance(v, str)}


def save_state(entries: dict[str, str], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"knowledge": KNOWLEDGE_NAME, "files": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def plan_sync(
    targets: list[Path],
    state: dict[str, str],
    linked: dict[str, str],
    full: bool = False,
    root: Path = REPO_ROOT,
) -> tuple[list[tuple[Path, str, str]], list[str], list[str]]:
    """
    (入れ直すもの, 飛ばすもの, ナレッジから消すもの) を決める。

    飛ばす条件は2つとも満たすときだけ:
      - 前回同期したときと中身のsha256が同じ
      - サーバ側にもその名前で残っている(コンテナを作り直したら入れ直す)
    消すのは「前回同期したが、もう対象に無い」もの。リポジトリから消えた・
    名前が変わったファイルが古いまま検索に出続けるのを防ぐ。利用者が自分で
    入れたファイルは state に無いので触らない。
    """
    upload: list[tuple[Path, str, str]] = []
    skipped: list[str] = []
    for path in targets:
        name = upload_name(path, root)
        digest = content_digest(path)
        if not full and state.get(name) == digest and name in linked:
            skipped.append(name)
        else:
            upload.append((path, name, digest))
    current = {upload_name(path, root) for path in targets}
    stale = sorted(name for name in state if name not in current and name in linked)
    return upload, skipped, stale


def add_to_knowledge(client: ApiClient, knowledge_id: str, file_id: str) -> None:
    client.request(
        "POST", f"/api/v1/knowledge/{knowledge_id}/file/add", {"file_id": file_id}
    )


def purge_orphans(
    client: ApiClient, knowledge_id: str, names: set[str], keep_ids: set[str] | None = None
) -> int:
    """
    同期対象と同じ名前で、ナレッジに入っていないファイル記録を消す。

    取り込みに失敗した回のアップロードは記録だけ残る。放っておくと同じ名前で
    何件も溜まる(実際451件まで増えていた)。名前が一致するものだけを見るので、
    利用者が自分で上げたファイルは触らない。

    keep_ids には**この回に入れたばかりのID**を渡す。入れた直後は
    meta.collection_name がまだナレッジIDになっておらず、置き去りに
    見えてしまう。渡さないと、たった今入れた資料を自分で消す。
    """
    keep_ids = keep_ids or set()
    records = all_files(client)
    keep = linked_from(records, knowledge_id)
    removed = 0
    for item in records:
        name = record_name(item)
        file_id = item.get("id")
        if not file_id or name not in names or file_id in keep_ids:
            continue
        if file_id != keep.get(name):
            client.request("DELETE", f"/api/v1/files/{file_id}", none_on=(400, 404))
            removed += 1
    return removed


def drop_file(client: ApiClient, knowledge_id: str, file_id: str | None) -> None:
    """古いファイルをナレッジから外し、実体も消す。無ければ何もしない。"""
    if not file_id:
        return
    client.request(
        "POST",
        f"/api/v1/knowledge/{knowledge_id}/file/remove",
        {"file_id": file_id},
        none_on=(400, 404),
    )
    client.request("DELETE", f"/api/v1/files/{file_id}", none_on=(400, 404))


def delete_obsolete_model(client: ApiClient) -> bool:
    """
    旧方式のモデルが残っていたら消す。無ければ何もしない。

    削除の口はPOSTの版とDELETEの版があるため両方試す(片方は405で返る)。
    """
    found = client.request(
        "GET", f"/api/v1/models/model?id={OBSOLETE_MODEL_ID}", none_on=(401, 404)
    )
    if not isinstance(found, dict) or not found.get("id"):
        return False
    path = f"/api/v1/models/model/delete?id={OBSOLETE_MODEL_ID}"
    for method, payload in (("DELETE", None), ("POST", {"id": OBSOLETE_MODEL_ID})):
        client.request(method, path, payload, none_on=(400, 404, 405, 422))
        after = client.request(
            "GET", f"/api/v1/models/model?id={OBSOLETE_MODEL_ID}", none_on=(401, 404)
        )
        if not isinstance(after, dict) or not after.get("id"):
            return True
    raise DeployError(
        f"旧モデル({OBSOLETE_MODEL_ID})を消せませんでした。"
        "管理画面 → ワークスペース → モデル から手で削除してください"
    )


def reset(client: ApiClient, targets: list[Path]) -> None:
    """
    壊れたナレッジと、過去に同期したファイルを消す。

    紐付けが外れたまま再登録しようとすると
    "Duplicate content detected" で弾かれるため、作り直す道を用意する。
    **利用者が自分で作ったナレッジ・ファイルは消さない**(名前で厳密に絞る)。
    """
    for entry in all_knowledge(client):
        if entry.get("name") == KNOWLEDGE_NAME and entry.get("id"):
            client.request(
                "DELETE", f"/api/v1/knowledge/{entry['id']}/delete", none_on=(400, 404)
            )
            print(f"  削除: ナレッジ {entry['id'][:8]}…")

    ours = {upload_name(path) for path in targets}
    removed = 0
    for item in all_files(client):
        if item.get("filename") in ours and item.get("id"):
            client.request("DELETE", f"/api/v1/files/{item['id']}", none_on=(400, 404))
            removed += 1
    print(f"  削除: 同期済みファイル {removed}件")
    try:
        STATE_PATH.unlink()
    except OSError:
        pass


def sync(apply: bool, do_reset: bool = False, full: bool = False) -> None:
    targets = collect_targets()
    print(f"対象ドキュメント: {len(targets)}件")
    client = ApiClient(resolve_base_url(), load_api_key())
    print(f"埋め込み設定: {embedding_status(client)} (ollama系でなければオフラインで停止する。要確認)")
    duplicates = [k for k in all_knowledge(client) if k.get("name") == KNOWLEDGE_NAME]
    knowledge = find_knowledge(client)
    linked = linked_from(all_files(client), knowledge["id"]) if knowledge else {}
    print(f"ナレッジ「{KNOWLEDGE_NAME}」: {len(duplicates)}件 / 入っているファイル {len(linked)}件")
    if len(duplicates) > 1:
        print("  ※ 重複しています。--reset を付けて作り直してください")

    state = {} if (do_reset or full) else load_state()
    upload, skipped, stale = plan_sync(targets, state, linked, full=full)
    total = sum(path.stat().st_size for path, _, _ in upload)
    print(f"入れ直す: {len(upload)}件 ({total:,} バイト) / 変更なし: {len(skipped)}件")
    if stale:
        print(f"ナレッジから消す(対象から外れた): {len(stale)}件")
        for name in stale[:10]:
            print(f"  - {name}")
    if not apply:
        for _, name, _ in upload[:20]:
            print(f"  + {name}")
        if len(upload) > 20:
            print(f"  … 他 {len(upload) - 20}件")
        print("\n(dry-run: 何も変更していません。実行は --apply)")
        return

    # オフラインで生きられる埋め込み(ローカルOllamaのbge-m3)へ寄せる。
    # 既に合っているなら触らない(更新の口は版によって必須項目が違い、
    # 正しい設定のまま422で弾かれることがある)。
    # 失敗しても同期は続け、管理画面での手動設定を案内する
    if embedding_status(client) != "ollama / bge-m3:latest":
        try:
            client.request(
                "POST",
                "/api/v1/retrieval/embedding/update",
                {"embedding_engine": "ollama", "embedding_model": "bge-m3:latest"},
            )
            print("埋め込みを ollama / bge-m3:latest に設定しました")
        except DeployError as error:
            print(
                "注意: 埋め込みの自動設定に失敗。管理者設定→ドキュメントで "
                f"embedding を ollama / bge-m3:latest にしてください\n  ({str(error).splitlines()[0]})"
            )

    if do_reset:
        print("既存のナレッジと同期済みファイルを削除します")
        reset(client, targets)
        knowledge = None
        linked = {}

    if not knowledge:
        created = client.request(
            "POST",
            "/api/v1/knowledge/create",
            {
                "name": KNOWLEDGE_NAME,
                "description": "院内ツールのヘルプ・運用ルール・コードマップ・プログラム本体",
                "access_control": None,
            },
        )
        if not isinstance(created, dict) or not created.get("id"):
            raise DeployError(f"ナレッジを作成できません: {str(created)[:200]}")
        knowledge = created
    knowledge_id = knowledge["id"]

    # 対象から外れたファイルを先に落とす(古い内容が検索に出続けるのを防ぐ)
    for name in stale:
        drop_file(client, knowledge_id, linked.pop(name, None))
        state.pop(name, None)
        print(f"  除外: {name}")

    failed: list[str] = []
    fresh_ids: set[str] = set()
    for index, (path, name, digest) in enumerate(upload, start=1):
        try:
            # 先に新しいほうを入れてから古いほうを落とす。逆にすると、
            # Ollamaが止まっている回は「消しただけ」で終わり、資料が
            # ナレッジから抜け落ちる。定期実行だと誰も気づけない
            uploaded = upload_file(client, name, path.read_bytes())
            old_id = linked.pop(name, None)
            try:
                add_to_knowledge(client, knowledge_id, uploaded["id"])
            except DeployError as error:
                # 古い写しが同じ中身としてナレッジに残っていると弾かれる。
                # 判定は取り込み後の本文で行われるため、改行コードだけ
                # 変わった回でもぶつかる。古いほうを落としてから入れ直す
                if "duplicate" not in str(error).lower():
                    raise
                drop_file(client, knowledge_id, old_id)
                old_id = None
                add_to_knowledge(client, knowledge_id, uploaded["id"])
        except DeployError as error:
            # 1件の失敗で全部を捨てない。理由を残して続ける(次回また入れ直す)
            failed.append(f"{name}: {str(error).splitlines()[-1][:120]}")
            state.pop(name, None)
            print(f"  [{index}/{len(upload)}] 失敗: {name}")
            continue
        drop_file(client, knowledge_id, old_id)
        fresh_ids.add(str(uploaded["id"]))
        state[name] = digest
        print(f"  [{index}/{len(upload)}] 同期: {name}")

    save_state(state)
    # 何も入れ直していない回は置き去りも生まれない。一覧の取得は
    # ページを何度もたどるので、変更が無い回はそこまでで終える
    if upload or stale:
        orphans = purge_orphans(
            client, knowledge_id, {upload_name(p) for p in targets}, fresh_ids
        )
        if orphans:
            print(f"置き去りのファイル記録を {orphans}件 片付けました")

    linked = linked_files(client, knowledge_id)
    print(f"\nナレッジに入っているファイル: {len(linked)}件 / 対象 {len(targets)}件")
    if failed:
        print(f"取り込めなかったファイル ({len(failed)}件):")
        for reason in failed:
            print(f"  - {reason}")
    # 失敗の中身を出し切ってから、止まりうる後片付けへ進む
    if delete_obsolete_model(client):
        print(f"旧モデル({OBSOLETE_MODEL_ID})を削除しました。使うのは「使い方ヘルプ」1つだけです")
    if not linked:
        raise DeployError(
            "ファイルが1件も紐付いていません。回答は空になります。\n"
            "対処: --reset を付けて作り直してください"
        )
    print("完了。Open WebUIのモデル一覧に「使い方ヘルプ」が出ます")
    print("確認: python tools\\open_webui_help_sync.py --ask \"議事録を作るには?\"")


def ask(question: str) -> None:
    """
    ヘルプPipeへ実際に質問して応答を出す。

    検索とgemma4の生成でかかるため、登録系(30秒)より長く待つ。
    モデルの読み込みから始まると数分かかることがある。
    """
    import json as _json
    import urllib.error
    import urllib.request

    client = ApiClient(resolve_base_url(), load_api_key())
    request = urllib.request.Request(
        f"{client.base_url}/api/chat/completions",
        data=_json.dumps(
            {
                "model": HELP_PIPE_ID,
                "messages": [{"role": "user", "content": question}],
                "stream": False,
            }
        ).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {client.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            result = _json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise DeployError(
            f"質問に失敗 (HTTP {error.code})\n"
            f"詳細: {error.read().decode('utf-8', errors='replace')[:300]}"
        )
    except OSError as error:
        raise DeployError(f"質問に失敗: {error}")
    try:
        print(result["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        raise DeployError(f"応答を解釈できません: {str(result)[:300]}")


def start_log(path: Path = LOG_PATH) -> None:
    """
    画面の代わりにログファイルへ書く(タスクスケジューラから走らせるため)。

    pythonw.exe には画面が無く、そのままだと成否が誰にも分からない。
    見に行くのは失敗したときだけなので、古い分は切り捨てて溜め込まない。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        tail = path.read_text(encoding="utf-8", errors="replace")[-LOG_MAX_BYTES // 2 :]
        path.write_text(f"(古い分を切り捨てました)\n{tail}", encoding="utf-8")
    stream = path.open("a", encoding="utf-8", buffering=1)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stream.write(f"\n===== {stamp} 開始\n")
    sys.stdout = stream
    sys.stderr = stream


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="実際に作成・更新する")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="既存のナレッジと同期済みファイルを消してから作り直す(--applyと併用)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="中身が変わっていなくても全件入れ直す(--applyと併用)",
    )
    parser.add_argument("--ask", metavar="質問", help="ヘルプへ試験質問して応答を表示")
    parser.add_argument(
        "--log",
        action="store_true",
        help=f"画面ではなく {LOG_PATH.name} へ書く(タスクスケジューラ用)",
    )
    args = parser.parse_args()
    if args.log:
        start_log()
    try:
        if args.ask:
            ask(args.ask)
        else:
            sync(apply=args.apply, do_reset=args.reset, full=args.full)
    except DeployError as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
