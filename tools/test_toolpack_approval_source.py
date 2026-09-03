"""承認の判定が1か所に集まっているか(Codexレビュー2026-09-01の残り1点)。

**承認は版ごと**(台帳 D-14)。`status` を直接読む場所が残っていると、
D-14 で版ごとへ移したときに**そこだけ違う表示になる**。
判定は `toolpack_store.is_approved` に集める。

ここでは「版ごとの記録がツール単位の status と食い違う」台帳を作り、
表示する側が全部そろって版ごとを見ることを確かめる。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import doctor
import toolpack_gui as gui
import toolpack_manage as manage
import toolpack_name_guard as guard
import toolpack_store as ts

# ツール単位では「承認済み」だが、いま動いている 1.1.0 は未承認。
# **正しい答えは「未承認」**(D-14 で版ごとへ移した後の形)
MIXED = {
    "active_version": "1.1.0",
    "status": "approved",
    "enabled": True,
    "versions": {"1.0.0": {"approved": True}, "1.1.0": {"approved": False}},
}


@pytest.fixture
def store(tmp_path) -> ts.ToolpackStore:
    instance = ts.ToolpackStore(tmp_path / "additional-tools")
    instance.ensure_layout()
    for version in ("1.0.0", "1.1.0"):
        package = instance.package_dir("duty_summary", version)
        package.mkdir(parents=True)
        (package / "tool.json").write_text(
            json.dumps({"id": "duty_summary", "display_name": "当直表集計"},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    instance._write_registry({"schema_version": 1, "tools": {"duty_summary": MIXED}})
    return instance


def test_判定関数が版ごとを優先する() -> None:
    assert ts.is_approved(MIXED) is False
    assert ts.is_approved(MIXED, "1.0.0") is True
    assert ts.is_approved(MIXED, "1.1.0") is False


def test_管理CLIの一覧が版ごとを見る(store, capsys) -> None:
    manage.command_list(store)
    out = capsys.readouterr().out
    assert "未承認" in out
    assert "承認済み" not in out, "ツール単位の status を直接読んでいる"


def test_画面の一覧が版ごとを見る() -> None:
    rows = gui.collect_models({"tools": {"duty_summary": MIXED}}, set(), {}, {})
    assert rows[0].approved is False
    assert rows[0].approval_label == "未承認"
    assert gui.operable(rows[0]) is True  # 未承認なので操作できる


def test_あるべき表示名が版ごとを見る(store) -> None:
    names = guard.expected_names(store)
    assert names["duty_summary"] == ("1.1.0", "【未承認】当直表集計")


def test_doctorのあるべき表示名も版ごとを見る(store, monkeypatch, tmp_path) -> None:
    """表示名の組み立てが版ごとの承認で通ること(そこで落ちないこと)を見る。"""
    monkeypatch.setattr(doctor, "_toolpack_store", lambda root: store)
    findings = doctor.check_toolpack_names(tmp_path)
    titles = [finding.title for finding in findings]
    assert "追加ツールの表示名を確認できません" not in titles, findings


def test_表示名の組み立ては真偽値を受け取る() -> None:
    """`status` の文字列を渡す形だと、呼ぶ側ごとに判定が分かれる。"""
    assert doctor.expected_display_name("当直表集計", False) == "【未承認】当直表集計"
    assert doctor.expected_display_name("当直表集計", True) == "当直表集計"


def test_statusを直接読む場所が残っていない() -> None:
    """承認の判定を散らさない(散ると D-14 でここだけ古いまま残る)。"""
    root = Path(__file__).resolve().parents[1] / "tools"
    offenders = []
    for path in sorted(root.glob("toolpack_*.py")):
        if path.name == "toolpack_store.py":
            continue  # 判定の本体はここ
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if '"status"' in line and "approved" in line:
                offenders.append(f"{path.name}:{number}")
    assert not offenders, f"承認の判定が散っている: {offenders}"
