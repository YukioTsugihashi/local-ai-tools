"""open_webui_help_sync.py のネットワーク不要部分のテスト。"""

from pathlib import Path

import open_webui_help_sync as sync


def test_対象は実在するmdだけで重複しない(tmp_path: Path):
    (tmp_path / "HELP_KILO_BEGINNER.md").write_text("x", encoding="utf-8")
    rules = tmp_path / ".kilo" / "rules"
    rules.mkdir(parents=True)
    (rules / "02-map.md").write_text("x", encoding="utf-8")
    (rules / "01-workflow.md").write_text("x", encoding="utf-8")
    targets = sync.collect_targets(tmp_path)
    names = [sync.upload_name(p, tmp_path) for p in targets]
    assert names == [
        "HELP_KILO_BEGINNER.md",
        ".kilo__rules__01-workflow.md",
        ".kilo__rules__02-map.md",
    ]
    assert len(set(names)) == len(names)


def test_multipartは境界とファイル名を含む():
    body, content_type = sync.multipart_body("a.md", "本文".encode("utf-8"))
    boundary = content_type.split("boundary=")[1]
    assert boundary.encode() in body
    assert b'filename="a.md"' in body
    assert "本文".encode("utf-8") in body


def test_同期はモデルを作らない():
    """
    利用者が選ぶモデルは Pipe(hospital_help_pipe)だけにする。

    旧方式のモデルを作り直すと、同じ表示名「使い方ヘルプ」が2つ並び、
    利用者が壊れたほう(回答が空)を選んでしまう。
    """
    assert not hasattr(sync, "model_payload")
    assert sync.HELP_PIPE_ID == "hospital_help_pipe"
    assert sync.OBSOLETE_MODEL_ID == "hospital_help"


# ------------------------------------------------------------------
# 回答が毎回空になっていた原因(2026-08-17)
# ------------------------------------------------------------------


def test_一覧APIはitems包みでも配列でも読める():
    """
    Open WebUIは版によって [...] と {"items": [...]} の両方を返す。

    配列決め打ちだと「0件」と誤読し、同期のたびに重複ナレッジを作る。
    実際に「院内ツールヘルプ」が2つでき、モデルは空のほうを見ていた。
    """
    rows = [{"id": "k1", "name": "院内ツールヘルプ"}]
    assert sync.unwrap_items(rows) == rows
    assert sync.unwrap_items({"items": rows}) == rows
    assert sync.unwrap_items({"data": rows}) == rows
    # 解釈できない形は空扱い(例外で同期全体を落とさない)
    assert sync.unwrap_items(None) == []
    assert sync.unwrap_items({"unexpected": 1}) == []


def test_機密フォルダは対象に入らない(tmp_path: Path):
    """
    globを増やしたときに実データを載せないための最後の砦。

    data/ と templates/ は実在の会議データ・議事録・名簿。.env は認証情報。
    """
    for relative in (
        "data/患者一覧.md",
        "templates/議事録.md",
        "input/帳票.py",
        "output/結果.py",
        "work/一時.py",
        "logs/run.md",
        ".venv/Lib/site-packages/x.py",
        "tools/__pycache__/doctor.py",
        "node_modules/pkg/index.ts",
        ".env",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        assert not sync.is_safe_target(path, tmp_path), relative

    # 書式ひな型の説明だけは例外(合成データのみ)
    allowed = tmp_path / "templates" / "gui" / "README.md"
    allowed.parent.mkdir(parents=True, exist_ok=True)
    allowed.write_text("x", encoding="utf-8")
    assert sync.is_safe_target(allowed, tmp_path)

    # 通常のコード・文書は通る
    for relative in ("tools/doctor.py", "AGENTS.md", ".kilo/plugin/excel-tools.ts"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        assert sync.is_safe_target(path, tmp_path), relative


def test_機密フォルダはglob経由でも拾われない(tmp_path: Path):
    # is_safe_target を通さない経路が生まれたら落とす
    (tmp_path / "AGENTS.md").write_text("x", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "実データ.md").write_text("x", encoding="utf-8")
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "議事録.md").write_text("x", encoding="utf-8")

    names = [sync.upload_name(p, tmp_path) for p in sync.collect_targets(tmp_path)]

    assert names == ["AGENTS.md"]


def test_対象にコード本体が含まれる():
    """
    「どこに何がある」「今できることは何か」に答えるには説明文書だけでは足りない。

    説明文書だけへ戻すと、コードの場所を聞かれても答えられなくなる。
    """
    globs = " ".join(sync.TARGET_GLOBS)
    for needed in ("tools/*.py", "text-processing-bridge/app/*.py", ".kilo/plugin/*.ts"):
        assert needed in globs, f"{needed} が同期対象から外れている"


def test_ファイル一覧はページングをたどる():
    """
    /api/v1/files/ は50件ずつしか返さない。

    1ページ目だけ見ると掃除が途中で終わり、消し残りが積み上がる
    (実際421件まで増えていた)。
    """
    pages = {
        1: {"items": [{"id": f"f{i}", "filename": f"{i}.md"} for i in range(50)], "total": 60},
        2: {"items": [{"id": f"f{i}", "filename": f"{i}.md"} for i in range(50, 60)], "total": 60},
        3: {"items": [], "total": 60},
    }
    asked: list[str] = []

    class FakeClient:
        def request(self, method, path, payload=None, none_on=()):
            asked.append(path)
            page = int(path.split("page=")[1].split("&")[0])
            return pages.get(page, {"items": []})

    files = sync.all_files(FakeClient())

    assert len(files) == 60
    assert len(asked) >= 3, "2ページ目以降を取りに行っていない"
    # 本文まで返させると、変更が無い回でも全ファイルの全文が毎ページ流れる
    assert all("content=false" in path for path in asked)


def test_同じページを返し続けても止まる():
    # page引数が効かない版でも無限ループにしない
    class StuckClient:
        def request(self, method, path, payload=None, none_on=()):
            return {"items": [{"id": "same", "filename": "a.md"}]}

    assert sync.all_files(StuckClient()) == [{"id": "same", "filename": "a.md"}]


# ------------------------------------------------------------------
# 差分同期(2026-08-17)
# 全件やり直すと119件ぶんの埋め込みが毎回走り、こまめに流せなかった
# ------------------------------------------------------------------


def _target(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_中身が同じで登録済みなら飛ばす(tmp_path: Path):
    path = _target(tmp_path, "AGENTS.md", "本文")
    state = {"AGENTS.md": sync.content_digest(path)}

    upload, skipped, stale = sync.plan_sync(
        [path], state, {"AGENTS.md": "f1"}, root=tmp_path
    )

    assert upload == []
    assert skipped == ["AGENTS.md"]
    assert stale == []


def test_中身が変わったものだけ入れ直す(tmp_path: Path):
    same = _target(tmp_path, "AGENTS.md", "本文")
    changed = _target(tmp_path, "DEMO.md", "新しい本文")
    state = {
        "AGENTS.md": sync.content_digest(same),
        "DEMO.md": "0" * 64,
    }
    linked = {"AGENTS.md": "f1", "DEMO.md": "f2"}

    upload, skipped, _ = sync.plan_sync([same, changed], state, linked, root=tmp_path)

    assert [name for _, name, _ in upload] == ["DEMO.md"]
    assert skipped == ["AGENTS.md"]
    # 記録する値は新しい中身のもの
    assert upload[0][2] == sync.content_digest(changed)


def test_サーバから消えていたら入れ直す(tmp_path: Path):
    """
    コンテナを作り直すとナレッジごと消える。

    記録だけを見て飛ばすと、資料ゼロのまま「同期済み」と言い張る状態になる。
    """
    path = _target(tmp_path, "AGENTS.md", "本文")
    state = {"AGENTS.md": sync.content_digest(path)}

    upload, skipped, _ = sync.plan_sync([path], state, {}, root=tmp_path)

    assert [name for _, name, _ in upload] == ["AGENTS.md"]
    assert skipped == []


def test_fullなら変わっていなくても全部入れ直す(tmp_path: Path):
    path = _target(tmp_path, "AGENTS.md", "本文")
    state = {"AGENTS.md": sync.content_digest(path)}

    upload, skipped, _ = sync.plan_sync(
        [path], state, {"AGENTS.md": "f1"}, full=True, root=tmp_path
    )

    assert len(upload) == 1
    assert skipped == []


def test_対象から外れたものはナレッジから消す(tmp_path: Path):
    """
    リポジトリから消した・名前を変えたファイルが古いまま検索に出続けるのを防ぐ。
    利用者が自分で入れたファイルは記録に無いので触らない。
    """
    path = _target(tmp_path, "AGENTS.md", "本文")
    state = {"AGENTS.md": sync.content_digest(path), "消えた.md": "0" * 64}
    linked = {"AGENTS.md": "f1", "消えた.md": "f2", "利用者の資料.pdf": "f3"}

    _, _, stale = sync.plan_sync([path], state, linked, root=tmp_path)

    assert stale == ["消えた.md"]


def test_記録は保存して読み戻せる(tmp_path: Path):
    path = tmp_path / "state.json"
    sync.save_state({"AGENTS.md": "abc"}, path)

    assert sync.load_state(path) == {"AGENTS.md": "abc"}


def test_記録が無い壊れているときは全件やり直す(tmp_path: Path):
    missing = tmp_path / "ない.json"
    broken = tmp_path / "壊れ.json"
    broken.write_text("{ここから壊れている", encoding="utf-8")

    assert sync.load_state(missing) == {}
    assert sync.load_state(broken) == {}


def test_ナレッジに入っているファイルはcollection_nameで見分ける():
    """
    ナレッジ詳細の files は常に null で使えない(この版のOpen WebUI)。

    ファイル一覧側の meta.collection_name がナレッジIDになっているかで判定する。
    利用者が個人で上げたファイルを巻き込まないための条件でもある。
    """
    class FakeClient:
        def request(self, method, path, payload=None, none_on=()):
            if "page=1" not in path:
                return {"items": []}
            return {
                "items": [
                    {"id": "f1", "filename": "AGENTS.md",
                     "meta": {"name": "AGENTS.md", "collection_name": "k1"}},
                    {"id": "f2", "filename": "私物.pdf",
                     "meta": {"name": "私物.pdf", "collection_name": "file-f2"}},
                    {"id": "f3", "filename": "他所.md", "meta": {"collection_name": "k9"}},
                ]
            }

    assert sync.linked_files(FakeClient(), "k1") == {"AGENTS.md": "f1"}


def test_置き去りの記録は同期対象の名前だけ片付ける():
    """
    取り込みに失敗した回のアップロードは記録だけ残り、同じ名前で溜まっていく
    (実際451件まで増えた)。ナレッジに入っている今の1件だけを残す。

    利用者が自分で上げたファイルは名前が違うので触らない。
    """
    records = [
        {"id": "new", "filename": "AGENTS.md",
         "meta": {"name": "AGENTS.md", "collection_name": "k1"}},
        {"id": "old1", "filename": "AGENTS.md",
         "meta": {"name": "AGENTS.md", "collection_name": "file-old1"}},
        {"id": "old2", "filename": "AGENTS.md",
         "meta": {"name": "AGENTS.md", "collection_name": "file-old2"}},
        {"id": "mine", "filename": "私の資料.pdf",
         "meta": {"name": "私の資料.pdf", "collection_name": "file-mine"}},
    ]
    deleted: list[str] = []

    class FakeClient:
        def request(self, method, path, payload=None, none_on=()):
            if method == "DELETE":
                deleted.append(path.rsplit("/", 1)[-1])
                return {}
            return {"items": records} if "page=1" in path else {"items": []}

    removed = sync.purge_orphans(FakeClient(), "k1", {"AGENTS.md"})

    assert removed == 2
    assert sorted(deleted) == ["old1", "old2"]
    assert "mine" not in deleted, "利用者のファイルを消している"
    assert "new" not in deleted, "ナレッジに入っている今の1件を消している"


def test_入れたばかりの資料は片付けの対象にしない():
    """
    追加した直後は meta.collection_name がまだナレッジIDになっておらず、
    置き去りに見える。除外しないと、たった今入れた資料を自分で消す
    (実際に121件中1件が毎回抜けていた)。
    """
    records = [
        {"id": "just", "filename": "AGENTS.md",
         "meta": {"name": "AGENTS.md", "collection_name": "file-just"}},
    ]
    deleted: list[str] = []

    class FakeClient:
        def request(self, method, path, payload=None, none_on=()):
            if method == "DELETE":
                deleted.append(path.rsplit("/", 1)[-1])
                return {}
            return {"items": records} if "page=1" in path else {"items": []}

    assert sync.purge_orphans(FakeClient(), "k1", {"AGENTS.md"}, {"just"}) == 0
    assert deleted == []


def test_旧モデルは見つけたら消す():
    """削除の口はPOSTの版とDELETEの版がある。片方が405で返っても消し切る。"""
    calls: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, exists: bool, accepts: str = "DELETE") -> None:
            self.exists = exists
            self.accepts = accepts

        def request(self, method, path, payload=None, none_on=()):
            calls.append((method, path))
            if method == "GET":
                return {"id": sync.OBSOLETE_MODEL_ID} if self.exists else None
            if method == self.accepts:
                self.exists = False
            return {}

    assert sync.delete_obsolete_model(FakeClient(True)) is True
    assert ("DELETE", f"/api/v1/models/model/delete?id={sync.OBSOLETE_MODEL_ID}") in calls

    # DELETEが405で素通りする版でもPOSTで消せる
    calls.clear()
    assert sync.delete_obsolete_model(FakeClient(True, accepts="POST")) is True
    assert ("POST", f"/api/v1/models/model/delete?id={sync.OBSOLETE_MODEL_ID}") in calls

    calls.clear()
    assert sync.delete_obsolete_model(FakeClient(False)) is False
    assert not [c for c in calls if c[0] != "GET"], "無いのに消しに行っている"
