from __future__ import annotations

import importlib
import pkgutil

import app


def test_all_app_modules_import_cleanly() -> None:
    """
    app配下の全モジュールを実際にimportする。

    モジュール分割後の取り残し(存在しない名前のimport・移動漏れ)を、
    どのテストからも参照されないモジュールも含めて検出するための保険。
    """
    for module_info in pkgutil.iter_modules(app.__path__):
        importlib.import_module(f"app.{module_info.name}")
