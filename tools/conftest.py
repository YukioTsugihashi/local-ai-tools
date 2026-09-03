"""
tools/ のテストを、実行ディレクトリに関係なく動くようにする。

pre-commitフックは text-processing-bridge から pytest を起動するため、
リポジトリ直下がsys.pathに入らず `local_tool_bridge` を読めなかった。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
