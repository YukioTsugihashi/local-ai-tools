"""
成果物と受け取り物の控えを、リポジトリのフォルダへ残す。

Open WebUI経由の処理は、その場でファイルを返すだけで手元に何も残らなかった。
Excel経路(ハブ)が output/ へ残しているのと揃える。

置き場所:
  成果物   -> output/
  受け取り -> work/imports/   (safe_file_import.py と同じ置き場)

保存に失敗しても処理は続ける。控えが取れないことは、利用者の作業を
止める理由にならない。失敗は None を返して呼び出し側へ知らせる。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "output"
IMPORT_DIR = ROOT / "work" / "imports"

# ファイル名に使えない文字。Windowsで弾かれるものと、パスを飛び越える記号
UNSAFE_NAME = re.compile(r'[<>:"/\|?*\x00-\x1f]+')
MAX_STEM = 60


def _stamped_name(name: str, suffix: str, when: Optional[datetime] = None) -> str:
    """日時を頭に付けた安全なファイル名。同じ名前でも上書きしない。"""
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    stem = UNSAFE_NAME.sub("_", Path(name or "").stem).strip("._ ") or "無題"
    return f"{stamp}_{stem[:MAX_STEM]}{suffix}"


def _write(directory: Path, name: str, data: bytes) -> Optional[Path]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(data)
        return path
    except OSError:
        return None


def save_output(name: str, data: bytes, when: Optional[datetime] = None) -> Optional[Path]:
    """出来上がったファイルを output/ へ控える。"""
    suffix = Path(name or "").suffix or ".bin"
    return _write(OUTPUT_DIR, _stamped_name(name, suffix, when), data)


def save_import(name: str, text: str, when: Optional[datetime] = None) -> Optional[Path]:
    """受け取った中身を work/imports/ へ控える。原本ではなく抽出済みの文。"""
    if not text.strip():
        return None
    return _write(IMPORT_DIR, _stamped_name(name, ".txt", when), text.encode("utf-8"))
