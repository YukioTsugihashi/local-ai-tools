"""
Excelの左表から検証済みコメントを組み立て、ブックへ一括書込みするツール。

  python tools\\excel_comment_build.py work\\imports\\対象.xlsm --instruction "全シートへコメント"

事実の抽出・書込み・確認用レポートは既存ツールへ委譲し、ここは
「検証済み下書きの組み立て」と「ローカルLLMによる文体整形」だけを持つ。
結果はJSON1行で返す(呼び出し元がプログラムであるため)。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# PowerShell経由(既定cp932)だと日本語出力が化けるため、常にUTF-8で出す
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / "text-processing-bridge" / ".venv" / "Scripts" / "python.exe"

# コメントの文体整形に使うモデル(利用者指定: 文章化はgemma)。
# 事実・数値は下書き側で確定済みで、ここは言い回しを整えるだけ。
# 実在しないモデル名を書くと整形が無言で無効化されるため、
# doctorの必須モデル一覧と揃えておくこと。
COMMENT_POLISH_MODEL = "gemma4:26b"

# コメント欄の書式(フォント・サイズ・色・太さ・配置)の正本。
# 利用者がExcelで開いて書式を変えると、次の生成から出力へ反映される。
# 無ければ従来どおり書き込み先セルの書式をそのまま使う。
STYLE_TEMPLATE = Path("templates") / "gui" / "excel_comment_gui_template.xlsx"


def _record(context: dict[str, Any], kind: str, section: str, pattern: str) -> dict[str, Any] | None:
    matcher = re.compile(pattern)
    return next(
        (
            item
            for item in context.get("fact_records", [])
            if item.get("kind") == kind
            and item.get("section") == section
            and matcher.search(str(item.get("metric", "")))
        ),
        None,
    )


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _number(value: Any, digits: int = 1) -> str:
    number = abs(float(value or 0))
    return f"{int(number):,}" if number.is_integer() else f"{number:.{digits}f}"


def _direction(value: float, increase: str = "増加", decrease: str = "減少") -> str:
    if abs(value) < 0.051:
        return "横ばい"
    return increase if value > 0 else decrease


def _year(record: dict[str, Any] | None) -> str:
    return str((record or {}).get("previous_year") or "前年度")


def _percent_change(record: dict[str, Any] | None) -> str:
    value = (record or {}).get("change")
    if not _finite(value):
        return "比較不能"
    if abs(value) < 0.051:
        return "横ばい"
    return f"{_number(value)}％{'増' if value > 0 else '減'}"


def _count_sentence(label: str, record: dict[str, Any] | None, unit: str) -> str:
    absolute = (record or {}).get("absolute_change")
    change = (record or {}).get("change")
    if not _finite(absolute):
        return f"{label}は比較可能な直近年度値がない。"
    if abs(absolute) < 0.051:
        return f"{label}は{_year(record)}と同数である。"
    if not _finite(change):
        return f"{label}は比較可能な直近年度値がない。"
    return f"{label}は{_year(record)}と比較し、{_number(absolute)}{unit}（{_number(change)}％）{_direction(absolute)}している。"


def build_verified_comment(context: dict[str, Any]) -> str:
    def comparison(section: str, pattern: str) -> dict[str, Any] | None:
        return _record(context, "comparison", section, pattern)

    def trend(section: str, pattern: str) -> dict[str, Any] | None:
        return _record(context, "trend_comparison", section, pattern)

    def long_comparison(section: str, pattern: str) -> dict[str, Any] | None:
        return _record(context, "long_comparison", section, pattern)

    def target(section: str, metric: str) -> dict[str, Any] | None:
        return _record(context, "target", section, f"^{re.escape(metric)}$")

    def revenue_sentence(section: str, label: str) -> str:
        total = comparison(section, r"^(?:①)?収入（百万円）$")
        technique = comparison(section, r"手技料?収入")
        medicine = comparison(section, r"薬剤収入")
        material = comparison(section, r"材料収入")
        unit_price = comparison(section, r"単価")
        if not total:
            return f"Ⅰ．{label}収入は比較可能な直近年度値がない。"
        change = total.get("change") or 0
        earlier = trend(section, r"^(?:①)?収入（百万円）$")
        steadily_rising = (
            change > 0
            and earlier is not None
            and _finite(earlier.get("previous_value"))
            and float(earlier["previous_value"]) < float(total.get("previous_value") or 0) < float(total.get("latest_value") or 0)
        )
        description = (
            "横ばいである"
            if abs(change) < 0.051
            else f"{_number(change)}％{_direction(change)}しており、右肩上がりで推移している"
            if steadily_rising
            else f"{_number(change)}％{_direction(change)}している"
        )
        return "".join(
            (
                f"Ⅰ．{label}収入は{_year(total)}と比較し、{description}。",
                f"（内訳：手技料収入{_percent_change(technique)}、薬剤収入{_percent_change(medicine)}、材料収入{_percent_change(material)}）",
                f"また、{label}単価は{_percent_change(unit_price)}である。" if unit_price else "",
            )
        )

    def patient_sentence(section: str, label: str, pattern: str) -> str:
        recent = comparison(section, pattern)
        long = long_comparison(section, pattern)
        sentence = _count_sentence(label, recent, "人")
        absolute = (long or {}).get("absolute_change")
        if _finite(absolute):
            if abs(absolute) < 0.051:
                sentence += f"{_year(long)}と同数である。"
            else:
                sentence += (
                    f"{_year(long)}との比較では{_number(absolute)}人"
                    f"{_direction(absolute)}している。"
                )
        return sentence

    def surgery_sentence() -> str:
        recent = comparison("入院", r"手術件数")
        earlier = trend("入院", r"手術件数")
        if not earlier:
            return _count_sentence("手術件数", recent, "件")
        absolute = earlier.get("absolute_change")
        change = earlier.get("change")
        if not _finite(absolute) or not _finite(change):
            return _count_sentence("手術件数", recent, "件")
        first = (
            f"手術件数は{_year(earlier)}と比較し、{_number(absolute)}件"
            f"（{_number(change)}％）{_direction(absolute)}している。"
        )
        if recent:
            current_change = recent.get("change")
            if _finite(current_change) and abs(current_change) < 1:
                first += f"{_year(recent)}からはほぼ横ばいで推移している。"
            else:
                first += _count_sentence("直近年度の手術件数", recent, "件")
        return first

    occupancy = target("入院", "稼働率")
    occupancy_comparison = comparison("入院", r"稼働率")
    # 値が取れないシートで「0％である」と書くと、実在しない実績を報告することになる
    occupancy_latest = (occupancy or {}).get("latest_value")
    occupancy_parts = [
        f"Ⅳ．稼働率は{_number(occupancy_latest, 2)}％である。"
        if _finite(occupancy_latest)
        else "Ⅳ．稼働率は比較可能な値がない。"
    ]
    occupancy_change = (occupancy_comparison or {}).get("change")
    if _finite(occupancy_change):
        occupancy_parts.append(
            f"{_year(occupancy_comparison)}と同水準である。"
            if abs(occupancy_change) < 0.051
            else f"{_year(occupancy_comparison)}より{_number(occupancy_change)}ポイント{_direction(occupancy_change, '上昇', '低下')}している。"
        )
    occupancy_gap = (occupancy or {}).get("gap")
    if _finite(occupancy_gap):
        occupancy_parts.append(
            f"目標の{occupancy.get('target')}％以上を達成している。"
            if occupancy_gap >= 0
            else f"目標の{occupancy.get('target')}％まで{_number(occupancy_gap)}ポイント届いていない。"
        )

    reverse_rate = comparison("外来", r"逆紹介率")
    reverse_target = target("外来", "逆紹介率")
    reverse_parts: list[str] = []
    reverse_change = (reverse_rate or {}).get("change")
    if _finite(reverse_change):
        reverse_parts.append(
            f"逆紹介率は{_number(reverse_rate.get('latest_value'))}‰で、{_year(reverse_rate)}より{_number(reverse_change)}‰{_direction(reverse_change, '上昇', '低下')}しており、"
        )
    elif reverse_target:
        reverse_parts.append(f"逆紹介率は{_number(reverse_target.get('latest_value'))}‰で、")
    reverse_gap = (reverse_target or {}).get("gap")
    if _finite(reverse_gap):
        reverse_parts.append(
            f"比較基準{_number(reverse_target.get('target'))}‰を{_number(reverse_gap)}‰{'上回っている' if reverse_gap >= 0 else '下回っている'}。"
        )

    combined = comparison("入外合計", r"^収入（百万円）$")
    combined_change = (combined or {}).get("change") or 0
    combined_summary = (
        f"・入外合計の収入は{_year(combined)}と比較し、"
        f"{'横ばいである' if abs(combined_change) < 0.051 else f'{_number(combined_change)}％{_direction(combined_change)}している'}。"
        f"（内訳：手技料収入{_percent_change(comparison('入外合計', r'手技料?収入'))}、"
        f"薬剤収入{_percent_change(comparison('入外合計', r'薬剤収入'))}、"
        f"材料収入{_percent_change(comparison('入外合計', r'材料収入'))}）"
        if combined
        else "・入外合計の収入は比較可能な直近年度値がない。"
    )
    target_summary = "一方、".join(
        part
        for part in (
            (
                f"稼働率は目標の{occupancy.get('target')}％以上を達成している"
                if occupancy and (occupancy.get("gap") or 0) >= 0
                else f"稼働率は目標まで{_number(occupancy.get('gap'))}ポイント届いていない" if occupancy else ""
            ),
            (
                f"逆紹介率は比較基準{_number(reverse_target.get('target'))}‰を上回っている"
                if reverse_target and (reverse_target.get("gap") or 0) >= 0
                else f"逆紹介率は比較基準{_number(reverse_target.get('target'))}‰を下回っている" if reverse_target else ""
            ),
        )
        if part
    )
    return "\n".join(
        (
            "《入院》",
            revenue_sentence("入院", "入院"),
            f"Ⅱ．{patient_sentence('入院', '入院患者数', r'患者数の推移')}{_count_sentence('新入院患者数', comparison('入院', r'^新入院患者数$'), '人')}",
            f"Ⅲ．{surgery_sentence()}",
            "".join(occupancy_parts),
            "《外来》",
            revenue_sentence("外来", "外来"),
            f"Ⅴ．{patient_sentence('外来', '外来患者数', r'患者数の推移')}{patient_sentence('外来', '1日あたりの初診患者数', r'[1１]日あたり.*初診患者数')}{''.join(reverse_parts)}",
            "《まとめ》",
            combined_summary,
            f"・{target_summary or '目標と比較できる指標がない'}。",
        )
    )


def _numeric_claims(text: str) -> list[tuple[float, str]]:
    """
    本文中の「数値+単位」を、比較できる形で取り出す。

    文字列のまま比べると表記の揺れだけで不一致になる。実際
    下書きの「4％増」と整形結果の「+4.0％」、「94.10％」と「94.1％」が
    別物と判定され、整形結果が毎回捨てられていた(2026-08-14に判明)。
    値が同じなら同じとみなすため、符号を外した数値で比較する
    (増減の向きは数値ではなく「増加/減少」の語で表され、この検査の対象外)。
    """
    claims = []
    for match in re.finditer(
        r"([+-]?\d[\d,]*(?:\.\d+)?)\s*(百万円|ポイント|[％%‰]|人|件|円)", text
    ):
        try:
            value = abs(float(match.group(1).replace(",", "")))
        except ValueError:
            continue
        claims.append((round(value, 4), match.group(2).replace("％", "%")))
    return claims


def _mask_numbers(text: str) -> str:
    """
    参考用の文章から数字だけを伏せる。言い回しは残る。

    整形役へ「この数値はコピーしないで」と頼んでも守られない。
    そもそも渡さないほうが確実である。
    """
    return re.sub(r"\d[\d,]*(?:\.\d+)?", "◯", text)


def refine_comment_with_ollama(
    context: dict[str, Any],
    verified: str,
    request: str,
    style_template: str = "",
    notes: list[str] | None = None,
) -> str:
    """
    検証済み下書きを文体だけ整える。失敗したら下書きをそのまま返す。

    落ちても処理は止めないが、**理由を notes へ残す**。
    黙って下書きへ戻ると、モデルが消えていても気づけない
    (実際にqwen3-coder削除後、整形が無言で無効化されていた)。
    """
    # 整形に渡す材料からは数字を取り除く。
    #
    # 以前は「正式なひな形」「検証済み事実」「ブック内のひな形」も生のまま渡し、
    # 「その数値はコピーしないでください」と注意書きを添えていた。実測すると
    # プロンプトに数字が229個あり、使ってよいのは下書きの33個だけだった
    # (検証済み事実だけで170個)。モデルは当然のように取り違え、8シート中7シートで
    # 数値ガードに引っかかって下書きへ退避していた(2026-08-18に実測)。
    #
    # 検証済み事実は下書きに織り込み済みなので渡さない。言い回しの参考は
    # 数字を伏せて渡す。下書きだけを渡すと数値は完全一致した(実測)。
    reference = _mask_numbers(style_template)
    book_style = _mask_numbers(str(context.get("template", "")))
    prompt = "\n\n".join(
        part
        for part in (
            "次の検証済み下書きを、Excelへ記入する自然な日本語の分析コメントに整えてください。",
            "数値、単位、比較年度、増減方向、達成・未達の判定、章立ては一切変更・省略・追加しないでください。",
            "表にない原因、利益、費用、改善効果を推測しないでください。Markdownや説明は付けず本文だけを返してください。",
            ("言い回しの参考(数値は伏せてあります。◯のまま書かないでください):\n" + reference)
            if reference.strip()
            else "",
            ("ブック内の文章ひな形(数値は伏せてあります):\n" + book_style) if book_style.strip() else "",
            f"ユーザー要件: {request or '全対象シートへコメントを生成する'}",
            f"シート: {context.get('sheet', '')}",
            "検証済み下書き:\n" + verified,
        )
        if part
    )
    payload = json.dumps(
        {
            "model": COMMENT_POLISH_MODEL,
            "stream": False,
            "keep_alive": "5m",
            # gemma4は思考(thinking)を出すモデル。抑制しないと、下書きを言い換える
            # だけのこの用途でも num_ctx の残り全部を思考に使い切り、
            # done_reason=length で本文が0文字のまま返る(2026-08-17に実測)。
            # そうなると下の構造チェックが必ず落ち、毎回下書きへ退避していた。
            "think": False,
            "options": {"temperature": 0, "num_ctx": 16384},
            "messages": [{"role": "user", "content": prompt}],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request_object = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    sheet = context.get("sheet", "")

    def note(reason: str) -> str:
        if notes is not None:
            notes.append(f"{sheet}: {reason}")
        return verified

    try:
        with urllib.request.urlopen(request_object, timeout=600) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        return note(f"文体整形を省略しました({COMMENT_POLISH_MODEL} / {error})")
    generated = str((result.get("message") or {}).get("content") or "").strip()
    generated = re.sub(r"^```[^\n]*\n?|```$", "", generated).strip()
    required = ("《入院》", "Ⅰ．", "Ⅱ．", "Ⅲ．", "Ⅳ．", "《外来》", "Ⅴ．", "《まとめ》")
    if not generated or len(generated) > 32767 or not all(part in generated for part in required):
        return note("文体整形の出力が必須構造を満たさないため下書きを使いました")
    # LLMが数値を捏造・欠落した場合は、必ず検証済み下書きへ戻す。
    generated_claims = sorted(_numeric_claims(generated))
    verified_claims = sorted(_numeric_claims(verified))
    if generated_claims != verified_claims:
        # 「変わった」だけでは調べようがないので、差分そのものを残す
        added = sorted(set(generated_claims) - set(verified_claims))
        lost = sorted(set(verified_claims) - set(generated_claims))
        def show(claims):
            return "、".join(f"{value:g}{unit}" for value, unit in claims[:6]) or "なし"
        return note(
            "文体整形で数値が変わったため下書きを使いました"
            f"(増えた: {show(added)} / 消えた: {show(lost)})"
        )
    return generated


def _run_python(python: Path, root: Path, script: str, args: list[str]) -> str:
    result = subprocess.run(
        [str(python), str(root / script), *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        check=False,
        # 呼び出し元がpythonw(コンソール無し)のとき、既定だと孫プロセスごとに
        # 黒いコンソール窓が開いて利用者の画面を邪魔する
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # 成功時はstdoutだけを返す。呼び出し元はこれをJSONとして読むため、
    # stderrを混ぜると壊れる。openpyxlはグラフを含むブックで
    # 「Unable to read chart rIdN ... A data source must be provided」等の
    # UserWarningをstderrへ出すため、混ぜるとJSONの後ろに警告が付いて
    # json.loads が「Extra data」で落ちていた(2026-08-17に実発生)。
    if result.returncode:
        detail = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        raise RuntimeError(detail[-4000:])
    return (result.stdout or "").strip()


@dataclass
class CommentResult:
    """コメント処理の結果一式。呼び出し元(ハブ・Kiloプラグイン)へJSONで返す。"""

    workbook: Path
    report: Path | None
    skipped: list[str]
    comments: list[dict[str, str]]
    notes: list[str]

    def as_json(self) -> dict:
        return {
            "workbook": str(self.workbook),
            "report": str(self.report) if self.report else None,
            "skipped": self.skipped,
            "notes": self.notes,
            "comments": self.comments,
        }


def process_excel_comment(
    root: Path,
    python: Path,
    source: Path,
    request: str = "",
    sheet_name: str | None = None,
    use_llm: bool = True,
    make_report: bool = False,
) -> CommentResult:
    """
    処理済みブック、失敗したシート、生成したコメント本文を返す。

    確認用レポート(コメント一覧)は **既定で作らない**。利用者が要らないと
    判断したため(2026-08-18)。作るぶんだけ待ち時間が延び、返るファイルも
    2つになる。必要なときだけ make_report=True(CLIは --report)。
    レポート単体は tools/excel_comment_report.py で後から作れる。
    """
    context_args = (
        [str(source), "--sheet", sheet_name]
        if sheet_name
        else [str(source), "--all"]
    )
    raw = _run_python(python, root, "tools/excel_comment_context.py", context_args)
    parsed = json.loads(raw)
    contexts = [parsed] if sheet_name else parsed.get("contexts", [])
    # 抽出できなかったシートは失敗として持ち回り、書ける分の処理は続ける
    skipped = [] if sheet_name else [
        f"{item.get('sheet', '')}: {item.get('reason', '')}"
        for item in parsed.get("skipped", [])
    ]
    if not contexts:
        raise RuntimeError("コメント対象シートが見つかりません")
    updates = []
    style_path = root / ".kilo" / "plugin" / "resources" / "excel-comment-example.txt"
    style_template = style_path.read_text(encoding="utf-8") if style_path.exists() else ""
    # 文体整形を省いた理由。書込み自体は成功しているので skipped とは分ける
    notes: list[str] = []
    for context in contexts:
        try:
            verified = build_verified_comment(context)
            value = (
                refine_comment_with_ollama(
                    context, verified, request, style_template, notes
                )
                if use_llm
                else verified
            )
        except Exception as error:
            skipped.append(f"{context.get('sheet', '')}: {error}")
            continue
        updates.append(
            {
                "sheet": context["sheet"],
                "cell": context["target"],
                "value": value,
                "template": context.get("template", ""),
            }
        )
    if not updates:
        raise RuntimeError(
            "コメントを生成できたシートがありません:\n  - " + "\n  - ".join(skipped)
        )
    manifest_dir = root / "work"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", dir=manifest_dir, delete=False) as handle:
        json.dump(updates, handle, ensure_ascii=False)
        manifest = Path(handle.name)
    try:
        write_args = ["set-batch", str(source), "--updates-file", str(manifest)]
        if (root / STYLE_TEMPLATE).exists():
            write_args.extend(["--style-template", str(root / STYLE_TEMPLATE)])
        write_output = _run_python(python, root, "tools/office_excel.py", write_args)
        # 書式テンプレートの適用結果(適用・不適用の理由)を利用者へ届ける
        notes.extend(
            line.strip()
            for line in write_output.splitlines()
            if line.strip().startswith("書式テンプレート")
        )
        match = re.search(
            r"保存しました(?:\([^\r\n]*\))?:\s*(.+\.(?:xlsx|xlsm))",
            write_output,
            re.IGNORECASE,
        )
        if not match:
            raise RuntimeError(f"処理済みExcelの保存先を確認できませんでした: {write_output[-1000:]}")
        updated = Path(match.group(1).strip())
        if not updated.is_absolute():
            updated = root / updated
        report: Path | None = None
        if make_report and len({item["sheet"] for item in updates}) > 1:
            report_output = _run_python(
                python,
                root,
                "tools/excel_comment_report.py",
                [str(updated), "--records-file", str(manifest)],
            )
            report_match = re.search(r"保存しました:\s*(.+\.xlsx)", report_output, re.IGNORECASE)
            if report_match:
                report = Path(report_match.group(1).strip())
                if not report.is_absolute():
                    report = root / report
        return CommentResult(
            workbook=updated,
            report=report,
            skipped=skipped,
            notes=notes,
            comments=[
                {"sheet": item["sheet"], "cell": item["cell"], "text": item["value"]}
                for item in updates
            ],
        )
    finally:
        manifest.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Excelへ検証済みコメントを生成して書き込む")
    parser.add_argument("workbook", help="input/またはwork/内のExcelパス")
    parser.add_argument("--instruction", default="", help="コメントの条件や観点")
    parser.add_argument("--sheet", help="試用時に1シートだけ処理する")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="LLMの文体整形を省き、検証済み下書きをそのまま書く",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="コメント一覧の確認用Excelも作る(既定は作らない)",
    )
    args = parser.parse_args()

    result = process_excel_comment(
        ROOT,
        VENV_PYTHON,
        Path(args.workbook),
        args.instruction,
        sheet_name=args.sheet,
        use_llm=not args.no_llm,
        make_report=args.report,
    )
    print(json.dumps(result.as_json(), ensure_ascii=False))


if __name__ == "__main__":
    main()
