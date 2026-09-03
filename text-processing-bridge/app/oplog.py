"""
PIIを含まない運用ログ。

障害調査のため「いつ・どのAPIが・どれだけの規模で・成功したか」だけを
JSON Linesで記録する。文字起こし本文・次第・名簿・LLM入出力・APIキーは
どのフィールドにも入れてはならない(数値・識別子・状態のみ)。
ログ書込の失敗(ディスク満杯等)は本処理へ波及させない。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ログ側の例外で本処理を止めない(標準loggingの安全弁)
logging.raiseExceptions = False


@lru_cache
def _build_logger(log_dir: str, max_bytes: int, backup_count: int) -> logging.Logger:
    # 保存先ごとに独立したloggerにする(テストや設定変更で衝突させない)
    logger = logging.getLogger(f"bridge.ops[{log_dir}]")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        directory / "operations.jsonl",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def log_event(**fields) -> None:
    """1リクエスト分の運用情報を1行のJSONで追記する(ベストエフォート)。"""
    try:
        logger = _build_logger(
            os.getenv("BRIDGE_LOG_DIR", "logs"),
            int(os.getenv("BRIDGE_LOG_MAX_BYTES", str(5 * 1024 * 1024))),
            int(os.getenv("BRIDGE_LOG_BACKUP_COUNT", "5")),
        )
        payload = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **fields,
        }
        logger.info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        # 運用ログは本処理より重要ではない。失敗しても静かに続行する
        pass
