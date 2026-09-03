"""
分割処理した抽出結果の統合と、重複・報告読み上げの除去。

項目が次第で確定しているため、LLMによる統合パスは不要。
Python側で決定的に結合できる。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_DEDUP_STRIP = re.compile(r"[\s　、。，．・…「」『』（）()]+")


def _dedup_key(text: str) -> str:
    """重複判定用に、句読点と空白を落とした比較用の文字列を作る。"""
    return _DEDUP_STRIP.sub("", text or "")


def _is_duplicate(candidate: str, seen: list[str]) -> bool:
    """
    既出の発言と実質同じかを判定する。

    分割処理では同じ話題が複数ブロックに跨るため、LLMが同趣旨の発言を
    何度も返すことがある。完全一致だけでは取りこぼすので、
    包含関係と先頭一致でも重複とみなす。
    """
    key = _dedup_key(candidate)
    if not key:
        return True
    for previous in seen:
        if key == previous:
            return True
        # 一方が他方を丸ごと含む(語尾違い・途中打ち切り)
        shorter, longer = sorted((key, previous), key=len)
        if len(shorter) >= 20 and shorter in longer:
            return True
        # 先頭が長く一致する(言い回しだけ違う繰り返し)
        if len(shorter) >= 30 and key[:30] == previous[:30]:
            return True
        # 音声認識のゆれで数語だけ違う繰り返しを拾う
        if len(shorter) >= 20 and SequenceMatcher(None, key, previous).ratio() >= 0.85:
            return True
        # 言い換え(語順や言い回しが違う同趣旨)は文字ビグラムの重なりで拾う。
        # 短文は語彙が偶然重なりやすいため、25字以上に限定する。
        if len(shorter) >= 25:
            grams_a = {key[i : i + 2] for i in range(len(key) - 1)}
            grams_b = {previous[i : i + 2] for i in range(len(previous) - 1)}
            union = grams_a | grams_b
            if union and len(grams_a & grams_b) / len(union) >= 0.60:
                return True
    return False


# 質問・意見・依頼を示す語。1つでもあれば議事録に載せる。
KEEP_MARKERS = re.compile(
    r"[かの]。$|か？|のか|ないか|ほしい|もらいた|いただきた|お願い|ください|"
    r"べき|必要|思う|考える|考えて|検討|すべき|だろうか|ますか|ですか|"
    r"方がよい|ほうがよい|ほうがいい|た方が良い|てはどう|"
    r"どうか|いかが|提案|要望|懸念|問題ではないか|してはどうか"
)
# 実績値の羅列。報告の読み上げに特徴的。
REPORT_NUMBERS = re.compile(
    r"[0-9０-９][0-9０-９,，\.]*\s*(?:%|％|件|人|億|万|円|台|名|床|回|時間|日|月)"
)
# 報告調の文末。
REPORT_ENDINGS = re.compile(
    r"(?:となっている|となった|となります|であった|である|でした|ました|"
    r"しています|している|実施した|予定している|計上している|達成している|"
    r"増加|減少|承認されている|記載しています|説明した)[。、]?$"
)


def looks_like_report(text: str, previous: str = "") -> bool:
    """
    報告の読み上げらしい発言かを判定する。

    質問や依頼の語が1つでもあれば議事録に載せる。
    それが無く、かつ実績値の羅列または報告調の文末であれば落とす。
    ただし直前が質問なら、短い平叙文はその回答とみなして残す。
    LLMの分類漏れを拾う保険なので、判定は控えめにしている。
    """
    body = (text or "").strip()
    if not body:
        return True
    if KEEP_MARKERS.search(body):
        return False

    # 質問と回答の対は壊さない。
    # 「近隣病院と取り合いになっているのか」→「診療報酬改定で2,000件のラインが
    # 設定されたため…」のように、回答が数値を含むことは普通にある。
    # 直前が質問なら、報告調に見えても回答とみなして残す。
    if previous and KEEP_MARKERS.search(previous) and len(body) <= 200:
        return False

    return bool(REPORT_NUMBERS.search(body)) or bool(REPORT_ENDINGS.search(body))


def merge_chunk_results(chunks: list[dict[str, dict]]) -> dict[str, dict]:
    """
    分割処理した各ブロックの結果を項目ごとに連結する。
    """
    merged: dict[str, dict] = {}
    for chunk in chunks:
        for key, payload in chunk.items():
            bucket = merged.setdefault(
                key, {"報告者": "", "資料": "", "発言": []}
            )
            if not bucket["報告者"] and payload.get("報告者"):
                bucket["報告者"] = payload["報告者"]
            if not bucket.get("資料") and payload.get("資料"):
                bucket["資料"] = payload["資料"]
            seen = [_dedup_key(r["内容"]) for r in bucket["発言"]]
            for remark in payload.get("発言") or []:
                previous = bucket["発言"][-1]["内容"] if bucket["発言"] else ""
                if looks_like_report(remark.get("内容", ""), previous):
                    continue
                if _is_duplicate(remark.get("内容", ""), seen):
                    continue
                bucket["発言"].append(remark)
                seen.append(_dedup_key(remark["内容"]))

    # 項目をまたいだ重複も除く。同じ発言が複数項目に割り当てられたら、
    # 先に現れた項目にだけ残す。
    global_seen: list[str] = []
    for payload in merged.values():
        kept = []
        for remark in payload.get("発言") or []:
            if _is_duplicate(remark.get("内容", ""), global_seen):
                continue
            kept.append(remark)
            global_seen.append(_dedup_key(remark["内容"]))
        payload["発言"] = kept
    return merged
