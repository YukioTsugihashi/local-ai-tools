from __future__ import annotations

from app.merge import looks_like_report, merge_chunk_results


def test_questions_and_requests_are_kept() -> None:
    assert looks_like_report("ニュースレターはどこに配布しているのか。") is False
    assert looks_like_report("できる限り早く対応してほしい。") is False


def test_report_recitals_are_dropped() -> None:
    assert looks_like_report("稼働率は79.1%と大きく落ち込む結果となった。") is True
    assert looks_like_report("医薬品の新規採用が13件であり、承認されている。") is True


def test_answer_after_question_is_kept_even_with_numbers() -> None:
    # 質問と回答の対は壊さない。回答が数値を含むことは普通にある。
    assert (
        looks_like_report(
            "診療報酬改定で2,000件のラインが設定されたためである。",
            previous="近隣病院と取り合いになっているのか。",
        )
        is False
    )


def test_merge_dedupes_across_chunks_and_keeps_first_reporter() -> None:
    chunk1 = {
        "1-1": {
            "報告者": "山田課長補佐",
            "資料": "資料1",
            "発言": [{"発言者": "乙野教授", "内容": "予算は足りるのか。"}],
        }
    }
    # 分割の重ね部分で同じ発言が二重抽出されたケース
    chunk2 = {
        "1-1": {
            "報告者": "別人",
            "資料": "",
            "発言": [{"発言者": "乙野教授", "内容": "予算は足りるのか。"}],
        }
    }

    merged = merge_chunk_results([chunk1, chunk2])

    assert merged["1-1"]["報告者"] == "山田課長補佐"
    assert merged["1-1"]["資料"] == "資料1"
    assert merged["1-1"]["発言"] == [
        {"発言者": "乙野教授", "内容": "予算は足りるのか。"}
    ]


def test_merge_drops_report_recital_remarks() -> None:
    # LLMが分類し損ねた報告読み上げは、統合時のルールで確定的に落とす
    chunks = [
        {
            "2-1": {
                "報告者": "",
                "資料": "",
                "発言": [
                    {"発言者": "", "内容": "医薬品の新規採用が13件であり、いずれも承認されている。"}
                ],
            }
        }
    ]

    merged = merge_chunk_results(chunks)

    assert merged["2-1"]["発言"] == []


def test_merge_removes_duplicates_across_items() -> None:
    # 同じ発言が複数項目に割り当てられたら、先に現れた項目にだけ残す
    chunks = [
        {
            "1-1": {"報告者": "", "資料": "", "発言": [
                {"発言者": "", "内容": "共用ベッドの差配についてはできる限り早くお願いしたい。"}
            ]},
            "1-2": {"報告者": "", "資料": "", "発言": [
                {"発言者": "", "内容": "共用ベッドの差配についてはできる限り早くお願いしたい。"}
            ]},
        }
    ]

    merged = merge_chunk_results(chunks)

    assert len(merged["1-1"]["発言"]) == 1
    assert merged["1-2"]["発言"] == []
