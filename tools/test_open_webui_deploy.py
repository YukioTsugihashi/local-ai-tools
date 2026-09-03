"""open_webui_deploy の柵(対象・種別・ID・接続先の検査)の振る舞いテスト。

ネットワークには接続しない(純関数のみ)。
実行: text-processing-bridge\\.venv\\Scripts\\python.exe -m pytest tools -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from open_webui_deploy import (
    DeployError,
    build_diff,
    check_suggestion_routing,
    detect_plugin_type,
    ensure_local_url,
    extract_operation_patterns,
    parse_frontmatter,
    parse_suggestions,
    resolve_plugin_id,
    validate_container_imports,
    validate_pipe_model_requirements,
    validate_target_path,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

PIPE_SOURCE = '"""\ntitle: t\nid: sample_pipe\n"""\nclass Pipe:\n    pass\n'
TOOL_SOURCE = '"""\ntitle: t\nid: sample_tool\n"""\nclass Tools:\n    pass\n'


# ------------------------------------------------------------------
# 対象ファイルの柵
# ------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "tools").mkdir()
    return tmp_path


def test_tools_directory_open_webui_file_is_accepted(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    target = repo / "tools" / "open_webui_sample_pipe.py"
    target.write_text(PIPE_SOURCE, encoding="utf-8")
    assert validate_target_path(str(target), repo) == target.resolve()
    # openwebui_(アンダースコアなし)の既存命名も許す
    legacy = repo / "tools" / "openwebui_sample_v2.py"
    legacy.write_text(TOOL_SOURCE, encoding="utf-8")
    assert validate_target_path(str(legacy), repo) == legacy.resolve()


def test_files_outside_tools_or_with_other_names_are_rejected(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    outside = repo / "open_webui_evil.py"
    outside.write_text(PIPE_SOURCE, encoding="utf-8")
    with pytest.raises(DeployError):
        validate_target_path(str(outside), repo)

    wrong_name = repo / "tools" / "safe_task_runner.py"
    wrong_name.write_text(PIPE_SOURCE, encoding="utf-8")
    with pytest.raises(DeployError):
        validate_target_path(str(wrong_name), repo)

    with pytest.raises(DeployError):
        validate_target_path(str(repo / "tools" / "open_webui_missing.py"), repo)


def test_path_traversal_cannot_escape_tools(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    sneaky = repo / "open_webui_evil.py"
    sneaky.write_text(PIPE_SOURCE, encoding="utf-8")
    with pytest.raises(DeployError):
        validate_target_path("tools/../open_webui_evil.py", repo)


# ------------------------------------------------------------------
# 種別判定(Pipe / Tool)
# ------------------------------------------------------------------


def test_pipe_and_tool_classes_are_distinguished() -> None:
    assert detect_plugin_type(PIPE_SOURCE) == "pipe"
    assert detect_plugin_type(TOOL_SOURCE) == "tool"


def test_missing_both_classes_is_rejected_with_guidance() -> None:
    with pytest.raises(DeployError) as excinfo:
        detect_plugin_type("def analyze():\n    pass\n")
    assert "03-newtool" in str(excinfo.value)


def test_having_both_classes_is_rejected() -> None:
    with pytest.raises(DeployError):
        detect_plugin_type("class Pipe:\n    pass\nclass Tools:\n    pass\n")


def test_syntax_error_is_rejected_before_upload() -> None:
    with pytest.raises(DeployError):
        detect_plugin_type("class Pipe:\n    def broken(:\n")


def test_aliased_or_inherited_pipe_class_is_named_in_the_error() -> None:
    # ローカルLLMがやりがちな間違い(別名+継承)は、クラス名を名指しで直させる
    source = (
        "from tools.open_webui_text_processing_pipe import Pipe\n"
        "class LineCountPipe(Pipe):\n    pass\n"
    )
    with pytest.raises(DeployError) as excinfo:
        detect_plugin_type(source)
    assert "LineCountPipe" in str(excinfo.value)
    assert "class Pipe" in str(excinfo.value)


def test_repo_internal_imports_are_rejected() -> None:
    # コンテナ内に存在しないリポジトリ内モジュールは登録前に拒否する
    for bad_import in (
        "from tools.open_webui_text_processing_pipe import Pipe",
        "import tools.excel_reader",
        "import open_webui_text_processing_pipe",
        "from openwebui_universal_file_export_v2 import Tools",
    ):
        with pytest.raises(DeployError):
            validate_container_imports(bad_import + "\nclass Pipe:\n    pass\n")
    # Open WebUI本体・標準ライブラリ・外部ライブラリは当然OK
    validate_container_imports(
        "import re\nfrom open_webui.models.files import Files\nimport openpyxl\n"
    )


# ------------------------------------------------------------------
# frontmatter と ID
# ------------------------------------------------------------------


def test_frontmatter_fields_are_parsed() -> None:
    fields = parse_frontmatter(PIPE_SOURCE)
    assert fields["id"] == "sample_pipe"
    assert fields["title"] == "t"
    assert parse_frontmatter("x = 1\n") == {}


def test_suggestion_lines_become_prompt_chips() -> None:
    source = (
        '"""\n'
        "title: t\n"
        "suggestion: シート一覧 | 構成を確認 | シート一覧を見せて\n"
        "suggestion: 検索 | 『◯◯』を検索して\n"
        "suggestion: そのまま\n"
        '"""\n'
        "class Pipe:\n    pass\n"
    )
    assert parse_suggestions(source) == [
        {"title": ["シート一覧", "構成を確認"], "content": "シート一覧を見せて"},
        {"title": ["検索", ""], "content": "『◯◯』を検索して"},
        {"title": ["そのまま", ""], "content": "そのまま"},
    ]
    # suggestion行が無ければ空(既存のモデル設定に触らない判定に使う)
    assert parse_suggestions(PIPE_SOURCE) == []


def test_pipes_without_chips_or_description_are_rejected() -> None:
    chips = [{"title": ["a", "b"], "content": "c"}]
    described = {"model_description": "説明"}
    # 両方そろって初めて通る(モデル画面に何ができるか必ず出す運用ルール)
    validate_pipe_model_requirements("pipe", described, chips)
    with pytest.raises(DeployError):
        validate_pipe_model_requirements("pipe", described, [])
    with pytest.raises(DeployError):
        validate_pipe_model_requirements("pipe", {}, chips)
    # Toolはモデルとして画面に出ないため対象外
    validate_pipe_model_requirements("tool", {}, [])


ROUTED_PIPE_SOURCE = (
    '"""\ntitle: t\n"""\n'
    "import re\n"
    "OPERATION_PATTERNS = [\n"
    '    ("find", re.compile(r"検索|探して")),\n'
    '    ("index", re.compile(r"一覧")),\n'
    "]\n"
    "class Pipe:\n    pass\n"
)


def test_operation_patterns_are_extracted_statically() -> None:
    assert extract_operation_patterns(ROUTED_PIPE_SOURCE) == [
        ("find", "検索|探して"),
        ("index", "一覧"),
    ]
    # 規約に従わないファイルでは空(照合スキップ)
    assert extract_operation_patterns(PIPE_SOURCE) == []


def test_suggestion_routing_maps_chips_to_operations() -> None:
    chips = [
        {"title": ["検索", ""], "content": "『合計』を検索して"},
        {"title": ["一覧", ""], "content": "一覧を見せて"},
    ]
    mapping, uncovered = check_suggestion_routing(ROUTED_PIPE_SOURCE, chips)
    assert mapping == [("検索", ["find"]), ("一覧", ["index"])]
    assert uncovered == []


def test_suggestion_routing_detects_dead_chip_and_uncovered_operation() -> None:
    chips = [{"title": ["死にボタン", ""], "content": "なにかいい感じにして"}]
    mapping, uncovered = check_suggestion_routing(ROUTED_PIPE_SOURCE, chips)
    assert mapping == [("死にボタン", [])]
    assert uncovered == ["find", "index"]


def test_repo_pipe_chips_all_route_and_cover_operations() -> None:
    # 実ファイルの柵: 死にチップなし・チップの無い操作なし(ドリフト防止)
    for filename in (
        "open_webui_excel_analysis_pipe.py",
        "open_webui_text_processing_pipe.py",
    ):
        source = (REPO_ROOT / "tools" / filename).read_text(encoding="utf-8")
        mapping, uncovered = check_suggestion_routing(
            source, parse_suggestions(source)
        )
        assert mapping, filename
        for title, matched in mapping:
            assert matched, f"{filename}: チップ「{title}」がどの操作にも一致しない"
        assert uncovered == [], f"{filename}: チップの無い操作 {uncovered}"


def test_repo_pipes_define_suggestion_chips() -> None:
    expected_counts = {
        "open_webui_excel_analysis_pipe.py": 5,
        "open_webui_text_processing_pipe.py": 4,
    }
    for filename, expected_count in expected_counts.items():
        source = (REPO_ROOT / "tools" / filename).read_text(encoding="utf-8")
        suggestions = parse_suggestions(source)
        assert len(suggestions) == expected_count, filename
        assert all(s["content"] for s in suggestions), filename


def test_plugin_id_requires_frontmatter_or_override() -> None:
    assert resolve_plugin_id({"id": "abc_1"}, None) == "abc_1"
    assert resolve_plugin_id({"id": "abc_1"}, "over_ride") == "over_ride"
    with pytest.raises(DeployError):
        resolve_plugin_id({}, None)
    with pytest.raises(DeployError):
        resolve_plugin_id({"id": "大文字とNG"}, None)


# ------------------------------------------------------------------
# 接続先の柵と差分表示
# ------------------------------------------------------------------


def test_only_localhost_urls_are_allowed() -> None:
    assert ensure_local_url("http://localhost:3000/") == "http://localhost:3000"
    assert ensure_local_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    for bad in (
        "http://192.168.1.10:3000",
        "https://example.com",
        "http://open-webui.internal",
    ):
        with pytest.raises(DeployError):
            ensure_local_url(bad)


def test_diff_shows_added_lines() -> None:
    diff = build_diff("a\nb\n", "a\nb\nc\n", "sample")
    assert "+c" in diff
    assert build_diff("same\n", "same\n", "sample") == ""


# ------------------------------------------------------------------
# 実ファイルとの整合(登録対象がデプロイの柵を通ること)
# ------------------------------------------------------------------


def test_repo_pipe_and_tool_files_pass_the_gate() -> None:
    expectations = {
        "open_webui_excel_analysis_pipe.py": ("pipe", "excel_analysis"),
        "open_webui_text_processing_pipe.py": ("pipe", "text_processing"),
        "open_webui_dify_text_processing_tool_v0.6.0.py": ("tool", "dify_bridge"),
        "openwebui_universal_file_export_v2.py": ("tool", "universal_file_export"),
    }
    for filename, (expected_type, expected_id) in expectations.items():
        path = validate_target_path(f"tools/{filename}", REPO_ROOT)
        source = path.read_text(encoding="utf-8")
        assert detect_plugin_type(source) == expected_type, filename
        fields = parse_frontmatter(source)
        assert resolve_plugin_id(fields, None) == expected_id, filename
