"""2名承認(台帳 D-14)のテスト。

**承認は版ごと。人数は運用で変えられる。**

見るのは次:

- 決まった人数がそろって初めて状態が変わる。同じ人が2回押しても1件
- **承認すると表示名が変わる**ので、Pipe の作り直しと貼り直しまで行う。
  そこで失敗したら票ごと元へ戻す
- 更新したら承認はやり直し(版ごとなので新しい版は未承認から)
- 壊れた設定で承認が緩くならない

Open WebUI へのHTTPはすべてモックする。**本番の登録には触れない。**
"""

from __future__ import annotations

import copy
import json

import pytest

import toolpack_install as installer
import toolpack_manage as manage
import toolpack_store as ts
from test_toolpack_install import (  # noqa: F401  fixture をそのまま使う
    build_localtool, deploy_stub, store, tool_json,
)
from test_toolpack_update import add_first, build_update  # noqa: F401


def policy(store, **values) -> None:
    store.policy_path.write_text(
        json.dumps(values, ensure_ascii=False), encoding="utf-8"
    )


def approved(store, tool_id="duty_summary") -> bool:
    entry = store.load_registry()["tools"][tool_id]
    return ts.is_approved(entry)


# ---------------------------------------------------------------------------
# 人数がそろって初めて変わる
# ---------------------------------------------------------------------------
def test_1人では承認済みにならない(tmp_path, store, deploy_stub, capsys) -> None:
    add_first(tmp_path, store)
    assert manage.command_approve(store, "duty_summary", "甲野") == 0
    assert approved(store) is False
    out = capsys.readouterr().out
    assert "1/2 人" in out and "あと 1 人" in out


def test_2人そろえば承認済みになる(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    manage.command_approve(store, "duty_summary", "甲野")
    manage.command_approve(store, "duty_summary", "乙野")
    assert approved(store) is True


def test_同じ人が2回押しても1件(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    manage.command_approve(store, "duty_summary", "甲野")
    with pytest.raises(manage.ManageError) as caught:
        manage.command_approve(store, "duty_summary", "甲野")
    assert "既に入れています" in str(caught.value)
    assert approved(store) is False


def test_人数は変えられる(tmp_path, store, deploy_stub) -> None:
    """運用が回らないなら下げられるようにしておく(依頼者の指示)。"""
    add_first(tmp_path, store)
    policy(store, approvals_required=1)
    manage.command_approve(store, "duty_summary", "甲野")
    assert approved(store) is True


def test_承認できる人を決めておける(tmp_path, store, deploy_stub) -> None:
    """自由入力だと打ち間違いで別人になる。名前を並べておけば選ぶだけで済む。"""
    add_first(tmp_path, store)
    policy(store, approvers=["甲野", "乙野"])
    with pytest.raises(manage.ManageError) as caught:
        manage.command_approve(store, "duty_summary", "誰か")
    assert "承認できる人に 誰か がいません" in str(caught.value)


def test_名前が空なら断る(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    with pytest.raises((manage.ManageError, ts.StoreError)):
        manage.command_approve(store, "duty_summary", "   ")


# ---------------------------------------------------------------------------
# 承認すると表示名が変わる
# ---------------------------------------------------------------------------
def test_承認したら印を外したPipeを貼り直す(tmp_path, store, deploy_stub) -> None:
    """表示名は生成Pipeが持っている。**台帳を変えるだけでは名前は変わらない。**"""
    add_first(tmp_path, store)
    manage.command_approve(store, "duty_summary", "甲野")
    deploy_stub.deployed.clear()
    manage.command_approve(store, "duty_summary", "乙野")

    assert deploy_stub.deployed, "Pipeを貼り直していない"
    pipe = (
        store.generated_dir("duty_summary", "1.0.0")
        / "open_webui_duty_summary_pipe.py"
    ).read_text(encoding="utf-8")
    assert "【未承認】" not in pipe


def test_人数に届くまでPipeを触らない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    deploy_stub.deployed.clear()
    manage.command_approve(store, "duty_summary", "甲野")
    assert deploy_stub.deployed == []


def test_反映に失敗したら票ごと元へ戻す(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    manage.command_approve(store, "duty_summary", "甲野")
    deploy_stub.should_fail = True

    with pytest.raises(manage.ManageError):
        manage.command_approve(store, "duty_summary", "乙野")
    assert approved(store) is False
    state = store.approval_state("duty_summary")
    assert state["approvals"] == ["甲野"], "入れた票が残っている"


def test_確かめられなければ成功にしない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    manage.command_approve(store, "duty_summary", "甲野")
    deploy_stub.hub_unreachable = True

    with pytest.raises(manage.Unconfirmed):
        manage.command_approve(store, "duty_summary", "乙野")
    assert approved(store) is False


# ---------------------------------------------------------------------------
# 取り消し
# ---------------------------------------------------------------------------
def approve_two(store) -> None:
    manage.command_approve(store, "duty_summary", "甲野")
    manage.command_approve(store, "duty_summary", "乙野")


def test_取り消しも決まった人数が要る(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    approve_two(store)
    manage.command_approve(store, "duty_summary", "甲野", revoke=True)
    assert approved(store) is True, "1人で取り消せてしまっている"
    manage.command_approve(store, "duty_summary", "乙野", revoke=True)
    assert approved(store) is False


def test_取り消しの人数も変えられる(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    policy(store, revocations_required=1)
    approve_two(store)
    manage.command_approve(store, "duty_summary", "甲野", revoke=True)
    assert approved(store) is False


def test_取り消したら印つきのPipeへ戻す(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    approve_two(store)
    manage.command_approve(store, "duty_summary", "甲野", revoke=True)
    manage.command_approve(store, "duty_summary", "乙野", revoke=True)
    pipe = (
        store.generated_dir("duty_summary", "1.0.0")
        / "open_webui_duty_summary_pipe.py"
    ).read_text(encoding="utf-8")
    assert "【未承認】" in pipe


def test_承認されていないものは取り消せない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    with pytest.raises(manage.ManageError) as caught:
        manage.command_approve(store, "duty_summary", "甲野", revoke=True)
    assert "承認されていません" in str(caught.value)


def test_承認済みをもう一度承認しない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    approve_two(store)
    with pytest.raises(manage.ManageError) as caught:
        manage.command_approve(store, "duty_summary", "丙野")
    assert "既に承認済み" in str(caught.value)


def test_取り消しのあと承認をやり直せる(tmp_path, store, deploy_stub) -> None:
    """決まったら票は片付ける。次は新しく数え直す。"""
    add_first(tmp_path, store)
    approve_two(store)
    manage.command_approve(store, "duty_summary", "甲野", revoke=True)
    manage.command_approve(store, "duty_summary", "乙野", revoke=True)
    assert store.approval_state("duty_summary")["approvals"] == []
    approve_two(store)
    assert approved(store) is True


# ---------------------------------------------------------------------------
# 承認は版ごと
# ---------------------------------------------------------------------------
def test_更新したら承認はやり直し(tmp_path, store, deploy_stub) -> None:
    """基本理念どおり、新しい版は未承認から始まる(依頼者の判断)。"""
    add_first(tmp_path, store, version="1.0.0")
    approve_two(store)
    # 承認済みは入れ替えられないので、まず取り消す
    manage.command_approve(store, "duty_summary", "甲野", revoke=True)
    manage.command_approve(store, "duty_summary", "乙野", revoke=True)

    report = installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    assert report.ok, report.reason
    assert approved(store) is False
    entry = store.load_registry()["tools"]["duty_summary"]
    assert ts.is_approved(entry, "1.1.0") is False


def test_戻した先の承認状態がそのまま出る(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store, version="1.0.0")
    approve_two(store)
    entry = store.load_registry()["tools"]["duty_summary"]
    assert ts.is_approved(entry, "1.0.0") is True
    # 1.1.0 を足しても 1.0.0 の承認はそのまま
    store.add_version("duty_summary", "1.1.0", package_hash="x")
    entry = store.load_registry()["tools"]["duty_summary"]
    assert ts.is_approved(entry, "1.0.0") is True
    assert ts.is_approved(entry, "1.1.0") is False


def test_有効版を切り替えたら控えのstatusも変わる(tmp_path, store, deploy_stub) -> None:
    """ツール単位の status は有効版から導く控え。ずれたままにしない。"""
    add_first(tmp_path, store, version="1.0.0")
    approve_two(store)
    assert store.load_registry()["tools"]["duty_summary"]["status"] == "approved"
    store.add_version("duty_summary", "1.1.0", package_hash="x")
    store.set_active_version("duty_summary", "1.1.0")
    assert store.load_registry()["tools"]["duty_summary"]["status"] == "unapproved"


# ---------------------------------------------------------------------------
# 記録
# ---------------------------------------------------------------------------
def test_誰がいつ承認したかを残す(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    approve_two(store)
    record = store.load_registry()["tools"]["duty_summary"]["versions"]["1.0.0"]
    history = record["approval_history"]
    assert history[-1]["event"] == "approved"
    assert history[-1]["by"] == ["甲野", "乙野"]
    assert history[-1]["at"]


def test_承認したときの中身も残す(tmp_path, store, deploy_stub) -> None:
    """あとで差し替わったら「承認時と違う」と言えるようにする(台帳 D-14)。"""
    add_first(tmp_path, store)
    approve_two(store)
    record = store.load_registry()["tools"]["duty_summary"]["versions"]["1.0.0"]
    assert record["approval_history"][-1]["package_hash"] == record["package_hash"]


def test_取り消しも記録に残る(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    approve_two(store)
    manage.command_approve(store, "duty_summary", "甲野", revoke=True)
    manage.command_approve(store, "duty_summary", "乙野", revoke=True)
    history = (
        store.load_registry()["tools"]["duty_summary"]["versions"]["1.0.0"]
        ["approval_history"]
    )
    assert [item["event"] for item in history] == ["approved", "revoked"]


# ---------------------------------------------------------------------------
# 設定が壊れていても承認が緩くならない
# ---------------------------------------------------------------------------
def test_設定が無ければ2人2人(tmp_path, store) -> None:
    loaded = store.load_policy()
    assert loaded.approvals_required == 2 and loaded.revocations_required == 2
    assert loaded.approvers == () and loaded.problems == ()


def test_壊れた設定でも既定へ落として知らせる(tmp_path, store) -> None:
    store.policy_path.write_text("これはJSONではない", encoding="utf-8")
    loaded = store.load_policy()
    assert loaded.approvals_required == 2
    assert loaded.problems, "黙って既定へ落としている"


def test_0人や負の数は受け付けない(tmp_path, store) -> None:
    """0人にすると、押さずに全部承認済みになってしまう。"""
    policy(store, approvals_required=0, revocations_required=-1)
    loaded = store.load_policy()
    assert loaded.approvals_required == 2 and loaded.revocations_required == 2
    assert len(loaded.problems) == 2


def test_真偽値を人数として受け取らない(tmp_path, store) -> None:
    policy(store, approvals_required=True)
    assert store.load_policy().approvals_required == 2


# ---------------------------------------------------------------------------
# 承認済みは触らせない(D-15 との繋がり)
# ---------------------------------------------------------------------------
def test_承認済みは無効化も削除もできない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    approve_two(store)
    for command in (manage.command_disable, manage.command_remove):
        with pytest.raises(manage.ManageError) as caught:
            command(store, "duty_summary")
        assert "承認済み" in str(caught.value)


def test_承認済みは入れ替えられない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store, version="1.0.0")
    approve_two(store)
    report = installer.install(build_update(tmp_path), apply=True, update=True, store=store)
    assert not report.ok
    assert "承認済み" in report.reason


def test_一覧に途中経過が出る(tmp_path, store, deploy_stub, capsys) -> None:
    add_first(tmp_path, store)
    manage.command_approve(store, "duty_summary", "甲野")
    capsys.readouterr()
    manage.command_list(store)
    out = capsys.readouterr().out
    assert "未承認(1/2人)" in out
    assert "承認: 甲野" in out


def test_承認済みのときは取消側の人だけ出す(tmp_path, store, deploy_stub, capsys) -> None:
    """決め手になった票は残してあるので、取り違えると
    **承認した人が取消側の人として出てしまう**(実機で気づいた)。"""
    add_first(tmp_path, store)
    approve_two(store)
    manage.command_approve(store, "duty_summary", "丙野", revoke=True)
    capsys.readouterr()
    manage.command_list(store)
    out = capsys.readouterr().out
    assert "取消: 丙野" in out
    assert "取消: 甲野" not in out, "承認した人を取消側として出している"
    assert "承認: 甲野" not in out, "承認済みなのに承認の途中経過を出している"


# ---------------------------------------------------------------------------
# 失敗したら投票前へ丸ごと戻す(Codexレビュー2026-09-02)
# ---------------------------------------------------------------------------
def version_record(store, version="1.0.0", tool_id="duty_summary") -> dict:
    return store.load_registry()["tools"][tool_id]["versions"][version]


def test_取消の反映に失敗したら承認票まで戻す(tmp_path, store, deploy_stub) -> None:
    """決まったときに反対側の票を片付けるので、「入れた1票だけ消す」では
    戻しきれない。承認票が消えたままになっていた(Codex指摘)。"""
    add_first(tmp_path, store)
    approve_two(store)
    before = copy.deepcopy(version_record(store))
    deploy_stub.should_fail = True

    manage.command_approve(store, "duty_summary", "甲野", revoke=True)  # 1票目は届かない
    with pytest.raises(manage.ManageError):
        manage.command_approve(store, "duty_summary", "乙野", revoke=True)

    after = version_record(store)
    assert after["approved"] is True, "承認済みへ戻っていない"
    assert ts._distinct(after.get("approvals") or []) == ["甲野", "乙野"], "承認票が消えている"


def test_巻き戻したら版の記録が完全に一致する(tmp_path, store, deploy_stub) -> None:
    """履歴も票も、投票前とそっくり同じに戻す。"""
    add_first(tmp_path, store)
    manage.command_approve(store, "duty_summary", "甲野")
    before = copy.deepcopy(version_record(store))
    deploy_stub.should_fail = True

    with pytest.raises(manage.ManageError):
        manage.command_approve(store, "duty_summary", "乙野")
    assert version_record(store) == before


def test_承認の反映に失敗しても履歴を残さない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    manage.command_approve(store, "duty_summary", "甲野")
    deploy_stub.should_fail = True
    with pytest.raises(manage.ManageError):
        manage.command_approve(store, "duty_summary", "乙野")
    assert "approval_history" not in version_record(store)


# ---------------------------------------------------------------------------
# 壊れた設定は承認を緩めない(Codexレビュー2026-09-02)
# ---------------------------------------------------------------------------
def test_承認者一覧が壊れていたら承認を断る(tmp_path, store, deploy_stub) -> None:
    """自由入力へ落とすと、名前を絞っていたはずが**誰でも承認できる**。"""
    add_first(tmp_path, store)
    store.policy_path.write_text('{"approvers": "壊れた値"}', encoding="utf-8")
    with pytest.raises(manage.ManageError) as caught:
        manage.command_approve(store, "duty_summary", "誰でも")
    assert "読めない" in str(caught.value)
    assert approved(store) is False


def test_JSONとして壊れていても承認を断る(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    store.policy_path.write_text("これはJSONではない", encoding="utf-8")
    with pytest.raises(manage.ManageError):
        manage.command_approve(store, "duty_summary", "甲野")


def test_人数が壊れていても承認を断る(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    policy(store, approvals_required=0)
    with pytest.raises(manage.ManageError):
        manage.command_approve(store, "duty_summary", "甲野")


def test_設定が無いのは正常(tmp_path, store, deploy_stub) -> None:
    """置いていないのと壊れているのは別。無ければ2人/2人・自由入力で動く。"""
    add_first(tmp_path, store)
    assert store.load_policy().usable is True
    assert manage.command_approve(store, "duty_summary", "甲野") == 0


def test_正しい設定なら使える(tmp_path, store) -> None:
    policy(store, approvals_required=1, approvers=["甲野"])
    loaded = store.load_policy()
    assert loaded.usable is True and loaded.present is True


def test_壊れた設定は使えないと分かる(tmp_path, store) -> None:
    store.policy_path.write_text('{"approvals_required": "二人"}', encoding="utf-8")
    loaded = store.load_policy()
    assert loaded.usable is False
    assert loaded.present is True and loaded.problems


# ---------------------------------------------------------------------------
# 人数を途中で変えても詰まらない(Codexレビュー2026-09-02)
# ---------------------------------------------------------------------------
def test_人数を下げたら届いている票で確定できる(tmp_path, store, deploy_stub) -> None:
    """2人設定で1票入れたあと1人へ下げると、その1票で足りているのに
    未承認のまま止まり、**入れた本人はもう押せない**(Codex指摘)。"""
    add_first(tmp_path, store)
    manage.command_approve(store, "duty_summary", "甲野")
    policy(store, approvals_required=1)

    assert store.approval_state("duty_summary")["ready"] is True
    assert manage.command_approve(store, "duty_summary", "甲野") == 0
    assert approved(store) is True


def test_確定は誰が押しても効く(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    manage.command_approve(store, "duty_summary", "甲野")
    policy(store, approvals_required=1)
    assert manage.command_approve(store, "duty_summary", "乙野") == 0
    assert approved(store) is True
    # 新しい票は入れない(届いていた票で確定させる)
    history = version_record(store)["approval_history"]
    assert history[-1]["by"] == ["甲野"]


def test_確定でも表示名を貼り直す(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    manage.command_approve(store, "duty_summary", "甲野")
    policy(store, approvals_required=1)
    deploy_stub.deployed.clear()
    manage.command_approve(store, "duty_summary", "甲野")
    pipe = (
        store.generated_dir("duty_summary", "1.0.0")
        / "open_webui_duty_summary_pipe.py"
    ).read_text(encoding="utf-8")
    assert deploy_stub.deployed and "【未承認】" not in pipe


def test_確定に失敗したら投票前へ戻す(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    manage.command_approve(store, "duty_summary", "甲野")
    before = copy.deepcopy(version_record(store))
    policy(store, approvals_required=1)
    deploy_stub.should_fail = True
    with pytest.raises(manage.ManageError):
        manage.command_approve(store, "duty_summary", "甲野")
    assert version_record(store) == before


def test_人数を上げても承認済みは覆さない(tmp_path, store, deploy_stub) -> None:
    """黙って承認が外れるほうが危ない。決まったものは決まったまま。"""
    add_first(tmp_path, store)
    approve_two(store)
    policy(store, approvals_required=5)
    assert approved(store) is True
    assert store.approval_state("duty_summary")["ready"] is False


def test_取消の票も人数を下げたら確定できる(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    approve_two(store)
    manage.command_approve(store, "duty_summary", "甲野", revoke=True)
    policy(store, revocations_required=1)
    assert store.approval_state("duty_summary")["ready"] is True
    manage.command_approve(store, "duty_summary", "甲野", revoke=True)
    assert approved(store) is False


def test_届いていなければ確定しない(tmp_path, store, deploy_stub) -> None:
    add_first(tmp_path, store)
    manage.command_approve(store, "duty_summary", "甲野")
    assert store.approval_state("duty_summary")["ready"] is False
    assert store.settle_pending("duty_summary", "1.0.0") is None


def test_票が無ければ確定しない(tmp_path, store, deploy_stub) -> None:
    """1人設定でも、誰も押していないのに承認済みにしない。"""
    add_first(tmp_path, store)
    policy(store, approvals_required=1)
    assert store.settle_pending("duty_summary", "1.0.0") is None
    assert approved(store) is False
