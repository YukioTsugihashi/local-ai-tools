from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / ".kilo" / "plugin" / "stage-guard.ts"


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---", 2)[1])


def test_stage_guard_has_physical_gates() -> None:
    source = PLUGIN.read_text(encoding="utf-8")
    required = [
        '"tool.execute.before"',
        '"tool.execute.after"',
        'action: tool.schema.enum(["plan", "status", "advance", "complete", "reset"])',
        "if (EDIT_TOOLS.has(input.tool) && !state.planned)",
        "if (state.currentStage < state.stages.length - 1)",
        "if (!state.testsPassed)",
        "if (!state.dryRunPassed)",
        "DRY_RUN_COMMAND.test(command)",
        "if (APPLY_COMMAND.test(command)) state.deployed = true",
        "state.stageDirty = true",
        "state.stageDirty = false",
    ]
    for marker in required:
        assert marker in source


def test_stage_guard_has_an_escape_hatch_and_survives_restart() -> None:
    source = PLUGIN.read_text(encoding="utf-8")

    # 詰まったときに作業ごと止まらないための出口
    assert 'if (args.action === "reset")' in source
    # VS Code再起動で工程計画が消えると、途中から再開できなくなる
    assert "restoreSessions()" in source
    assert "persistSessions()" in source


def test_stage_guard_does_not_fire_on_a_single_file_fix() -> None:
    source = PLUGIN.read_text(encoding="utf-8")

    # 「.pyを修正してテスト」程度で段階管理へ落ちない閾値であること
    assert "domains >= 3 || paths >= 4" in source


def test_stage_guard_is_enabled_for_working_agents() -> None:
    for name in ("code.md", "debug.md", "data.md"):
        config = _frontmatter(ROOT / ".kilo" / "agents" / name)
        assert config["tools"]["stage_guard"] is True


def test_staged_change_rule_is_registered_by_filename() -> None:
    rule = ROOT / ".kilo" / "rules" / "07-staged-changes.md"
    text = rule.read_text(encoding="utf-8")
    assert 'stage_guard(action="plan")' in text
    assert 'stage_guard(action="advance"' in text
    assert 'stage_guard(action="complete")' in text
