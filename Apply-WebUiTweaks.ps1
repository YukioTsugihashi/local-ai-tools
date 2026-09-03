# Open WebUI コンテナへローカル調整を再適用する。
#
# open-webui コンテナは docker run 起動でボリューム管理外のため、
# コンテナを作り直すと custom.css と index.html の変更が消える。
# そのときはこのスクリプトを実行する。
#
#   powershell -ExecutionPolicy Bypass -File <このスクリプトの場所>\Apply-WebUiTweaks.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Container = "open-webui"
$CssSource = Join-Path $PSScriptRoot "custom.css"
$CssTarget = "/app/backend/open_webui/static/custom.css"
$ExcelGuardSource = Join-Path $PSScriptRoot "open_webui_excel_upload_guard.js"
$ExcelGuardTarget = "/app/backend/open_webui/static/excel-upload-guard.js"
$IndexPath = "/app/build/index.html"
$Docker = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"

if (-not (Test-Path -LiteralPath $CssSource)) {
    throw "custom.css が見つかりません: $CssSource"
}
if (-not (Test-Path -LiteralPath $ExcelGuardSource)) {
    throw "Excelアップロードガードが見つかりません: $ExcelGuardSource"
}

& $Docker inspect $Container *> $null
if ($LASTEXITCODE -ne 0) {
    throw "コンテナ $Container が見つかりません。"
}

Write-Host "==> custom.css を配置しています" -ForegroundColor Cyan
& $Docker cp $CssSource "${Container}:${CssTarget}"
if ($LASTEXITCODE -ne 0) { throw "custom.css のコピーに失敗しました。" }

Write-Host "==> ExcelのRAG処理防止ガードを配置しています" -ForegroundColor Cyan
& $Docker cp $ExcelGuardSource "${Container}:${ExcelGuardTarget}"
if ($LASTEXITCODE -ne 0) { throw "Excelアップロードガードのコピーに失敗しました。" }

# link タグへ毎回新しいバージョンを付け、ブラウザのキャッシュを確実に外す。
# バージョン有無のどちらにも一度で対応できるよう単一のsedで書き換える。
Write-Host "==> index.html のリンクを更新しています" -ForegroundColor Cyan
# PowerShellはネイティブexeへ渡す引数内のダブルクォートを落とすため、
# sedのパターンにクォートを含めない。custom.cssはindex.html内で1箇所だけ。
$version = (Get-Date).ToString("yyyyMMddHHmmss")
$sed = 's|custom\.css(\?v=[0-9]+)?|custom.css?v=' + $version + '|'
& $Docker exec $Container sed -i -E $sed $IndexPath
if ($LASTEXITCODE -ne 0) { throw "index.html の更新に失敗しました。" }

# ExcelガードはExcelアップロードだけをprocess=falseへ変更する。重複挿入を避け、
# 毎回のバージョン値でブラウザキャッシュを外す。
$guardTag = '<script src="/static/excel-upload-guard.js?v=' + $version + '"></script>'
$removeOldGuard = 's|<script src="/static/excel-upload-guard\.js\?v=[0-9]+"></script>||g'
& $Docker exec $Container sed -i -E $removeOldGuard $IndexPath
if ($LASTEXITCODE -ne 0) { throw "古いExcelアップロードガードの除去に失敗しました。" }
$insertGuard = 's|</head>|' + $guardTag + '</head>|'
& $Docker exec $Container sed -i $insertGuard $IndexPath
if ($LASTEXITCODE -ne 0) { throw "Excelアップロードガードの挿入に失敗しました。" }

# 角括弧を含むパターンはPowerShellの引数解釈で壊れるため使わない。
$result = & $Docker exec $Container grep -n custom.css $IndexPath
if ($LASTEXITCODE -ne 0) { throw "index.html の確認に失敗しました。" }
$guardResult = & $Docker exec $Container grep -n excel-upload-guard.js $IndexPath
if ($LASTEXITCODE -ne 0) { throw "Excelアップロードガードの確認に失敗しました。" }

Write-Host ""
Write-Host "適用しました:" -ForegroundColor Green
Write-Host ($result | Select-Object -First 1).Trim()
Write-Host ($guardResult | Select-Object -First 1).Trim()
Write-Host "ブラウザで Ctrl+Shift+R を1回押してください。"
