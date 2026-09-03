from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = Path(os.getenv("BRIDGE_CONFIG_DIR", PROJECT_ROOT / "config"))


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name}は整数で指定してください。") from error
    if value <= 0:
        raise ValueError(f"{name}は1以上にしてください。")
    return value


def _non_negative_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name}は整数で指定してください。") from error
    if value < 0:
        raise ValueError(f"{name}は0以上にしてください。")
    return value


def long_text_threshold_chars() -> int:
    """この文字数を超えるLLM処理は分割して実行する。"""
    return _positive_int_env("LONG_TEXT_THRESHOLD_CHARS", 8000)


def long_text_chunk_chars() -> int:
    """分割時の1ブロックあたりの文字数上限。"""
    return _positive_int_env("LONG_TEXT_CHUNK_CHARS", 6000)


def long_text_overlap_chars() -> int:
    """議事録抽出の分割時に、直前ブロック末尾を重ねる文字数。0で無効。"""
    return _non_negative_int_env("LONG_TEXT_OVERLAP_CHARS", 500)


def ollama_base_url() -> str:
    """ローカルLLMの接続先。ブリッジもOllamaも同じWindows上で動く。"""
    return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip()


def llm_timeout_seconds() -> float:
    raw_value = os.getenv("LLM_TIMEOUT_SECONDS", "").strip() or os.getenv(
        "DIFY_TIMEOUT_SECONDS", "300"
    )
    try:
        value = float(raw_value)
    except ValueError as error:
        raise ValueError("LLM_TIMEOUT_SECONDSは数値で指定してください。") from error
    if value <= 0:
        raise ValueError("LLM_TIMEOUT_SECONDSは0より大きくしてください。")
    return value

