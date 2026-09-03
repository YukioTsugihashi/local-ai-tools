// Kiloプラグインの判定ロジックの実行時テスト。
// 実行: node --experimental-strip-types --test .kilo/plugin/*.test.mjs
// (pytest からは tools/test_kilo_plugin_runtime.py が呼ぶ)

import assert from "node:assert/strict"
import path from "node:path"
import test from "node:test"

import {
  isClearlyReadOnlyCommentRequest,
  isExcelBashCommand,
  isExcelCommentContext,
  officeBinaryReadTarget,
  rangeCellCount,
  workbookPath,
} from "./excel-tools.ts"

test("Excel語とコメント語が揃ったときだけコメント文脈になる", () => {
  assert.equal(isExcelCommentContext("この.xlsmにコメントを入れて"), true)
  assert.equal(isExcelCommentContext("エクセルの分析内容を書いて"), true)
  // 片方だけでは発火させない(無関係な依頼を巻き込むため)
  assert.equal(isExcelCommentContext("エクセルのシート一覧を見せて"), false)
  assert.equal(isExcelCommentContext("この文章にコメントして"), false)
})

test("書込み不要の言い回しで柵から抜けられる", () => {
  // 誤判定で締め出されたときの逃げ道。壊すと利用者が詰む
  for (const text of [
    "このExcelのコメント、変更しないで確認だけして",
    "エクセルのコメント、調べるだけでいい",
    "Excelのコメント処理は不要です",
    "このExcelにどんなコメントが入っているか教えて",
  ]) {
    assert.equal(isClearlyReadOnlyCommentRequest(text), true, text)
  }
  assert.equal(isClearlyReadOnlyCommentRequest("Excelにコメントを入れて"), false)
})

test("範囲のセル数を列記号込みで数える", () => {
  assert.equal(rangeCellCount("A1:B2"), 4)
  assert.equal(rangeCellCount("A1:AA10"), 270) // 27列 × 10行
  assert.equal(rangeCellCount("こわれた範囲"), 0)
})

test("input と work の外のExcelは拒否する", () => {
  const root = path.resolve("D:/repo")
  assert.equal(
    workbookPath(root, "input/a.xlsx"),
    path.join(root, "input", "a.xlsx"),
  )
  assert.equal(
    workbookPath(root, "work/imports/a.xlsm"),
    path.join(root, "work", "imports", "a.xlsm"),
  )
  // 原本を直接触らせない柵
  assert.throws(() => workbookPath(root, "D:/Download/a.xlsx"), /input\/ または work\//)
  assert.throws(() => workbookPath(root, "data/a.xlsx"), /input\/ または work\//)
  // 対応形式の柵
  assert.throws(() => workbookPath(root, "input/a.txt"), /\.xlsx/)
})

test("Officeバイナリへのreadだけを検出する", () => {
  // .kilocodeignoreの拒否文には脱出手順を書けないため、プラグインが先回りして
  // 代替ツールへ誘導する。検出漏れはthinkingモデルの長考詰まりに直結する
  assert.equal(
    officeBinaryReadTarget("read", { filePath: "work/imports/R8第1四半期 科別分析.xlsm" }),
    "work/imports/R8第1四半期 科別分析.xlsm",
  )
  assert.equal(officeBinaryReadTarget("read", { filePath: "D:/x/報告書.DOCX" }), "D:/x/報告書.DOCX")
  // 通常ファイルのreadと、read以外のツールは素通し(調査手段を塞がない原則)
  assert.equal(officeBinaryReadTarget("read", { filePath: "tools/doctor.py" }), null)
  assert.equal(officeBinaryReadTarget("read", {}), null)
  assert.equal(officeBinaryReadTarget("excel_read", { workbook_path: "input/a.xlsx" }), null)
})

test("コメント本文の組み立てはPython側へ委譲されている", async () => {
  const source = await import("node:fs/promises").then((fs) =>
    fs.readFile(new URL("./excel-tools.ts", import.meta.url), "utf8"),
  )
  assert.match(source, /tools\/excel_comment_build\.py/)
  for (const reimplemented of ["buildVerifiedComment", "numericFactMismatch", "11434"]) {
    assert.equal(
      source.includes(reimplemented),
      false,
      `${reimplemented} がプラグインへ復活している`,
    )
  }
})

test("bashの柵は名前が出てくるだけでは発火しない", () => {
  // 2026-08-18: git add tools/office_excel.py が遮断され、コミットが2度止まった
  for (const command of [
    "git add tools/office_excel.py tools/test_office_excel.py",
    "git diff tools/office_excel.py",
    "git commit -m \"office_excel.py の書式テンプレート対応\"",
    "cat tools/office_excel.py",
    "python.exe -m pytest tools/test_office_excel.py",
    "git add templates/gui/excel_comment_gui_template.xlsx",
  ]) {
    assert.equal(isExcelBashCommand(command), false, command)
  }
})

test("bashからExcelを操作する形は止める", () => {
  for (const command of [
    "text-processing-bridge\\.venv\\Scripts\\python.exe tools\\office_excel.py set input\\a.xlsm",
    "python tools/excel_reader.py input/a.xlsx",
    "python tools\\safe_file_import.py D:\\外部\\b.xlsx",
    "copy input\\a.xlsx output\\b.xlsx",
    // gitで始まっても、続けてpythonを起動する形は抜けさせない
    "git status; python tools\\office_excel.py set a.xlsm",
  ]) {
    assert.equal(isExcelBashCommand(command), true, command)
  }
})
