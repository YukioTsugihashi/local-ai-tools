@echo off
rem 追加ツールの管理画面を開く。
rem pythonw.exe を使うので黒い窓は出ない(台帳 5-17 の罠を踏まないよう、
rem toolpack_gui.py は import の前に標準出力の捨て場を用意している)。
setlocal
cd /d "%~dp0"
start "" "%~dp0text-processing-bridge\.venv\Scripts\pythonw.exe" "%~dp0tools\toolpack_gui.py"
endlocal
