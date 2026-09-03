import { execFile } from "node:child_process"
import path from "node:path"
import { promisify } from "node:util"

import { tool, type Plugin } from "@kilocode/plugin"

const execFileAsync = promisify(execFile)
const EXCEL_SUFFIXES = new Set([".xlsx", ".xlsm", ".xls"])
const OFFICE_BINARY_SUFFIXES = new Set([".xlsx", ".xlsm", ".xls", ".docx", ".pptx"])

// readツールが指すOfficeバイナリのパスを返す(該当しなければnull)。
// .kilocodeignoreの拡張子ブロックと対象を揃えること。
export const officeBinaryReadTarget = (
  toolName: string,
  args: Record<string, unknown> | undefined,
) => {
  if (toolName !== "read") return null
  const target = String(args?.filePath ?? args?.path ?? args?.file_path ?? "")
  if (!target) return null
  return OFFICE_BINARY_SUFFIXES.has(path.extname(target).toLowerCase()) ? target : null
}
const excelCommentWriteSessions = new Set<string>()
const excelCommentContextSessions = new Set<string>()
const excelCommentRequests = new Map<string, string>()
const excelCommentResults = new Map<string, string>()

const messageText = (parts: Array<{ type: string; text?: string }>) =>
  parts
    .filter((part) => part.type === "text")
    .map((part) => part.text ?? "")
    .join("\n")

export const isExcelCommentContext = (text: string) => {
  const hasExcel = /(?:\.xlsx|\.xlsm|\.xls\b|Excel|エクセル)/i.test(text)
  const hasComment = /(?:コメント|所見|評価文|分析文|分析内容)/.test(text)
  return hasExcel && hasComment
}

export const isClearlyReadOnlyCommentRequest = (text: string) => {
  // 誤判定で締め出されたとき、利用者が短い一言で抜けられる逃げ道でもある
  const explicitlyNoChange = /(?:変更|書き込|記入|追記|更新|編集|生成|作成).{0,8}(?:しない|しなくてい|不要|なし)|(?:確認|見る|読む|評価|調査|調べ).{0,4}だけ|コメント(?:処理)?は(?:不要|なし|しない)/.test(text)
  const asksAboutExistingContent = /(?:入って|書かれて|記載されて|既存|内容|仕組み).{0,20}(?:どう|確認|見て|教えて|[?？])/.test(text)
  const shortInspectionQuestion = /(?:コメント|所見|評価文).{0,16}(?:ちゃんと|正しく|適切|どう).{0,12}[?？]/.test(text)
  return explicitlyNoChange || asksAboutExistingContent || shortInspectionQuestion
}

const appendSystemInstruction = (current: string | undefined, instruction: string) =>
  [current, instruction].filter(Boolean).join("\n\n")

const columnNumber = (letters: string) =>
  [...letters.toUpperCase()].reduce((value, character) => value * 26 + character.charCodeAt(0) - 64, 0)

// bashのコマンドがExcel操作にあたるか。
//
// 以前はコマンド文字列に名前が出てくるだけで遮断していたため、
// git add tools/office_excel.py や cat tools/office_excel.py まで止まっていた
// (2026-08-18に1日2回、コミットが進まなくなった)。柵の狙いは
// 「Excelをbash経由で操作させない」であって「その名前を口にさせない」ではない。
// 判定を「名前が出てくるか」から「実際に起動しているか」へ変える。
export const isExcelBashCommand = (command: string) => {
  // pythonがExcel系スクリプトを起動している形。gitなどと連結されていても捕まえる
  if (/python\S*\s+\S*(excel_reader|office_excel|safe_file_import)\.py/i.test(command)) {
    return true
  }
  // ブック自体を引数に取るコマンド。gitはファイル名を並べるだけなので除く
  return /(\.xlsx|\.xlsm|\.xls\b)/i.test(command) && !/^\s*git\s/i.test(command)
}

export const rangeCellCount = (cellRange: string) => {
  const match = /^([A-Z]+)(\d+):([A-Z]+)(\d+)$/i.exec(cellRange.trim())
  if (!match) return 0
  const [, startColumn, startRow, endColumn, endRow] = match
  return (
    (columnNumber(endColumn) - columnNumber(startColumn) + 1) *
    (Number(endRow) - Number(startRow) + 1)
  )
}

const runPython = async (directory: string, script: string, args: string[]) => {
  const python = path.join(
    directory,
    "text-processing-bridge",
    ".venv",
    "Scripts",
    "python.exe",
  )
  const result = await execFileAsync(python, [path.join(directory, script), ...args], {
    cwd: directory,
    encoding: "utf8",
    maxBuffer: 4 * 1024 * 1024,
    windowsHide: true,
  })
  return result.stdout.trim()
}

export const workbookPath = (directory: string, supplied: string) => {
  const resolved = path.resolve(directory, supplied)
  const allowedRoots = [path.resolve(directory, "input"), path.resolve(directory, "work")]
  const allowed = allowedRoots.some(
    (root) => resolved === root || resolved.startsWith(`${root}${path.sep}`),
  )
  if (!allowed) {
    throw new Error("Excelファイルは input/ または work/ 内の作業コピーを指定してください")
  }
  if (!EXCEL_SUFFIXES.has(path.extname(resolved).toLowerCase())) {
    throw new Error("対応するExcel形式は .xlsx、.xlsm、.xls です")
  }
  return resolved
}

// コメント本文の組み立て・検証・LLM整形は tools/excel_comment_build.py が唯一の実装。
// 以前はここへ同じ処理を書き写していたため、Kilo経由とOpen WebUI経由で
// 結果が食い違っていた(2026-08-14 に一本化)。

type CommentBuildResult = {
  workbook: string
  report: string | null
  skipped: string[]
  comments: Array<{ sheet: string; cell: string; text: string }>
}

export const processExcelComment = async (
  directory: string,
  sessionID: string,
  source: string,
  fallbackRequest: string,
) => {
  const request = excelCommentRequests.get(sessionID) ?? fallbackRequest
  const raw = await runPython(directory, "tools/excel_comment_build.py", [
    source,
    "--instruction",
    request,
  ])
  const lastLine = raw.split("\n").filter(Boolean).at(-1) ?? ""
  let parsed: CommentBuildResult
  try {
    parsed = JSON.parse(lastLine) as CommentBuildResult
  } catch {
    throw new Error(`コメント処理の結果を解釈できませんでした: ${raw.slice(-1000)}`)
  }

  const comments = parsed.comments
    .map((item) => `【${item.sheet}!${item.cell}】\n${item.text}`)
    .join("\n\n")
  const tally = parsed.skipped.length
    ? `${parsed.comments.length}シート成功 / ${parsed.skipped.length}シート失敗`
    : `${parsed.comments.length}シート`
  // 何が落ちたかを必ず返す。黙って減らすと「全部できた」と誤報告される
  const notes = parsed.skipped.length
    ? `処理できなかったシート (${parsed.skipped.length}件):\n  - ${parsed.skipped.join("\n  - ")}`
    : ""
  const outputs = [
    `保存しました(元ファイルは無変更): ${parsed.workbook}`,
    parsed.report ? `確認用レポート: ${parsed.report}` : "",
  ].filter(Boolean).join("\n")

  const result = [outputs, `生成コメント (${tally}):\n${comments}`, notes]
    .filter(Boolean)
    .join("\n\n")
  excelCommentRequests.delete(sessionID)
  excelCommentResults.set(sessionID, result)
  return result
}

const server: Plugin = async () => ({
  "chat.message": async (input, output) => {
    excelCommentResults.delete(input.sessionID)
    const text = messageText(output.parts)
    const inCommentContext = isExcelCommentContext(text)
    const writeRequested = inCommentContext && !isClearlyReadOnlyCommentRequest(text)
    if (inCommentContext) excelCommentContextSessions.add(input.sessionID)
    else excelCommentContextSessions.delete(input.sessionID)
    if (writeRequested) {
      excelCommentWriteSessions.add(input.sessionID)
      excelCommentRequests.set(input.sessionID, text)
    } else {
      excelCommentWriteSessions.delete(input.sessionID)
      excelCommentRequests.delete(input.sessionID)
    }
    if (!inCommentContext) return

    output.message.tools = {
      ...output.message.tools,
      ...(writeRequested
        ? {
            excel_import: true,
            excel_comment: true,
            excel_read: false,
            excel_find: false,
            excel_write: false,
            agent_manager: false,
            background_process: false,
            bash: false,
            task: false,
            write: false,
            edit: false,
            apply_patch: false,
          }
        : {
            excel_import: true,
            excel_read: true,
            excel_find: true,
            excel_comment: false,
            excel_write: false,
            write: false,
            edit: false,
            apply_patch: false,
          }),
    }

    output.message.system = appendSystemInstruction(
      output.message.system,
      writeRequested
        ? "この依頼はExcelへのコメント作成・書込み依頼である。外部Excelはexcel_importを1回使えば、作業コピー作成からコメント生成・書込み・再検証まで完了する。input/またはwork/内だけexcel_commentを使う。事前のexcel_read、taskへの委譲、独自Python作成は行わない。この内部判断情報を利用者向け回答へ書かない。"
        : "この依頼は既存のExcelコメントに関する確認・評価・相談であり、ファイルを変更しない。必要な読取りだけを行う。この内部判断情報を利用者向け回答へ書かない。",
    )
  },

  tool: {
    excel_import: tool({
      description:
        "ユーザーが明示した外部Excelファイルを、原本を変更せずwork/importsへ安全にコピーする。外部パスのExcelを読む前に必ず使う。",
      args: {
        source_path: tool.schema
          .string()
          .describe("ユーザーが@指定または明示したExcelファイルの絶対パス"),
      },
      async execute(args, context) {
        context.metadata({
          title: excelCommentWriteSessions.has(context.sessionID)
            ? "Excelコメントを生成して書込み"
            : "Excel作業コピーを作成",
        })
        const sourcePath = args.source_path.trim().replace(/^@(?=[A-Za-z]:[\\/])/, "")
        const importOutput = await runPython(
          context.directory,
          "tools/safe_file_import.py",
          [sourcePath],
        )
        if (!excelCommentWriteSessions.has(context.sessionID)) return importOutput

        const importedPath = importOutput.match(/作業コピーを作成しました:\s*(.+)/)?.[1]?.trim()
        if (!importedPath) throw new Error("作業コピーの保存先を取得できませんでした")
        const result = await processExcelComment(
          context.directory,
          context.sessionID,
          importedPath,
          excelCommentRequests.get(context.sessionID) ?? "Excelへコメントを書き込む",
        )
        return `${importOutput}\n\n${result}`
      },
    }),

    excel_read: tool({
      description:
        "input/またはwork/内のExcelを実際に読む。indexでシートと使用範囲、findで全セル検索、rangeで座標付きの指定範囲を返す。架空のセル内容を推測してはいけない。",
      args: {
        workbook_path: tool.schema.string().describe("input/またはwork/内のExcelパス"),
        operation: tool.schema
          .enum(["index", "find", "range", "compact_range"])
          .describe("読取操作。大きく疎な表にはcompact_rangeを使う"),
        query: tool.schema.string().optional().describe("find操作の検索語"),
        sheet: tool.schema.string().optional().describe("range操作のシート名"),
        cell_range: tool.schema.string().optional().describe("range操作の範囲。例 A1:F20"),
      },
      async execute(args, context) {
        const source = workbookPath(context.directory, args.workbook_path)
        const commandArgs = [source]
        if (args.operation === "index") commandArgs.push("--index-only", "--no-csv")
        if (args.operation === "find") {
          if (!args.query) throw new Error("find操作にはqueryが必要です")
          commandArgs.push("--find", args.query, "--context-rows", "2", "--no-csv")
        }
        if (args.operation === "range" || args.operation === "compact_range") {
          if (!args.sheet || !args.cell_range) {
            throw new Error("range操作にはsheetとcell_rangeが必要です")
          }
          commandArgs.push("--sheet", args.sheet, "--range", args.cell_range, "--no-csv")
          if (
            args.operation === "compact_range" ||
            (args.operation === "range" && rangeCellCount(args.cell_range) > 500)
          ) {
            commandArgs.push("--compact", "--max-cells", "5000")
          }
        }
        context.metadata({ title: `Excel読取: ${args.operation}` })
        return runPython(context.directory, "tools/excel_reader.py", commandArgs)
      },
    }),

    excel_find: tool({
      description:
        "Excelの全セルから見出しや語句を検索する。各一致セルと同じ列の直下2行も返すため、本文や記入例の確認に使う。",
      args: {
        workbook_path: tool.schema.string().describe("input/またはwork/内のExcelパス"),
        query: tool.schema.string().describe("検索する見出しまたは語句"),
      },
      async execute(args, context) {
        const source = workbookPath(context.directory, args.workbook_path)
        context.metadata({ title: `Excel検索: ${args.query}` })
        return runPython(context.directory, "tools/excel_reader.py", [
          source,
          "--find",
          args.query,
          "--context-rows",
          "2",
          "--no-csv",
        ])
      },
    }),

    excel_comment: tool({
      description:
        "codeエージェントのままExcelの左表を読み、ブック内の記入例に沿うコメントをローカルLLMで生成し、上段コメント本文欄へ書込み、output保存と再読込検証まで一括実行する。表示中のコメント対象シートを必ずすべて処理し、複数シートならコメント全文と参照表を縦積みした別の確認用xlsxも作る。読取・生成・書込をまとめて依頼された場合はこのツールを最初に1回使う。",
      args: {
        workbook_path: tool.schema.string().describe("input/またはwork/内のExcelパス"),
        request: tool.schema.string().describe("コメントの条件や観点を含むユーザーの依頼"),
      },
      async execute(args, toolContext) {
        const completed = excelCommentResults.get(toolContext.sessionID)
        if (completed) return `この依頼のコメント処理は完了済みです。\n\n${completed}`
        const source = workbookPath(toolContext.directory, args.workbook_path)
        toolContext.metadata({ title: "Excelコメントを生成して書込み" })
        return processExcelComment(
          toolContext.directory,
          toolContext.sessionID,
          source,
          args.request,
        )
      },
    }),

    excel_write: tool({
      description:
        "Excelの指定セルへ値を書き、原本を変更せずoutput/へ別名保存する。結合セル内の座標は書込可能な左上セルへ自動解決し、保存後に再読込検証する。",
      args: {
        workbook_path: tool.schema.string().describe("input/またはwork/内のExcelパス"),
        sheet: tool.schema.string().describe("書込先シート名"),
        cell: tool.schema.string().describe("書込先セル。結合範囲内でも指定可能"),
        value: tool.schema.string().optional().describe("セルへ書く文字列"),
        value_file: tool.schema
          .string()
          .optional()
          .describe("本文を読むwork/またはoutput/内のUTF-8ファイル"),
      },
      async execute(args, context) {
        const source = workbookPath(context.directory, args.workbook_path)
        if (Boolean(args.value) === Boolean(args.value_file)) {
          throw new Error("valueまたはvalue_fileのどちらか一方を指定してください")
        }
        const commandArgs = [
          "set",
          source,
          "--sheet",
          args.sheet,
          "--cell",
          args.cell,
        ]
        if (args.value_file) commandArgs.push("--value-file", args.value_file)
        else commandArgs.push("--value", args.value ?? "")
        context.metadata({ title: `Excel書込: ${args.sheet}!${args.cell}` })
        return runPython(context.directory, "tools/office_excel.py", commandArgs)
      },
    }),
  },

  "tool.execute.before": async (input, output) => {
    // Officeバイナリのreadは.kilocodeignoreでも拒否されるが、あちらの拒否文には
    // 脱出手順を書けず、モデルが次の一手を出せずに長考で詰まる(2026-08-14実測)。
    // ここで先回りして拒否し、代替ツールへの脱出手順を必ず添える。
    const officeBinary = officeBinaryReadTarget(input.tool, output.args)
    if (officeBinary) {
      throw new Error(
        "Officeバイナリ(.xlsx/.xlsm/.xls/.docx/.pptx)はreadツールで読めません(実データを直接コンテキストへ入れない柵)。" +
          "readを再試行せず、直ちに切り替えてください: " +
          "Excelは excel_read(構造・値)/excel_find(検索)/excel_comment(コメント生成)/excel_write(書込)。" +
          "Word/PowerPointは office_word.py show / office_ppt.py show。",
      )
    }
    // コメント文脈では手動の書込み・委譲だけを物理的に拒否する。
    // 読取り(read/glob/grep等)は誤判定時の調査手段として常に許可する。
    // 書込み/相談の意味分類はツール表示とsystem指示の出し分けにだけ使い、
    // 物理層は文脈判定のみに依存させる(分類の誤爆で調査不能に陥らないため)。
    // コメント処理が済んだ後まで編集を止めると、同じセッションで
    // 他の作業が一切できなくなる。完了した時点で柵を解除する。
    if (
      excelCommentContextSessions.has(input.sessionID) &&
      !excelCommentResults.has(input.sessionID) &&
      ["excel_write", "write", "edit", "apply_patch", "notebook_edit", "notebook_execute", "task"].includes(input.tool)
    ) {
      throw new Error(
        "Excelコメント文脈では手動書込み・独自編集・taskへの委譲は禁止です。" +
          "コメントの作成・更新はexcel_comment(外部ファイルはexcel_import)を1回使ってください。" +
          "この柵はexcel_commentが1回終われば自動で外れます。" +
          "そもそも書込みが不要な依頼だった場合は、依頼文へ「変更しない」「調べるだけ」のどれかを添えて送り直してください。",
      )
    }
    if (input.tool !== "bash") return
    if (!isExcelBashCommand(String(output.args?.command ?? ""))) return
    throw new Error(
      "Excel操作をbashで実行してはいけません。外部ファイルはexcel_import、検索はexcel_find、読取はexcel_read、書込はexcel_writeを使用してください。",
    )
  },
})

export default { id: "local-excel-tools", server }
