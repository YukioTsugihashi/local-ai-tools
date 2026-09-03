import type { Part, Plugin } from "@kilocode/plugin"

type Route = {
  agent: "code" | "debug" | "docs-writer" | "vision"
  reason: string
  model?: string
}

const textFrom = (parts: Part[]) =>
  parts
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("\n")
    .trim()

const hasImage = (parts: Part[]) =>
  parts.some((part) => part.type === "file" && part.mime.startsWith("image/"))

const isOfficeRequest = (text: string) =>
  /(?:\.xlsx|\.xlsm|\.xls\b|Excel|エクセル|Word|ワード|PowerPoint)/i.test(text)

const appendSystemInstruction = (current: string | undefined, instruction: string) =>
  [current, instruction].filter(Boolean).join("\n\n")

const selectRoute = (text: string, imageAttached: boolean): Route => {
  // Excel/Officeの加工はcodeに公開した専用ツールを使うため、他モードへ送らない。
  if (isOfficeRequest(text)) {
    return { agent: "code", reason: "Officeファイル処理" }
  }

  const changeRequested = /(?:直して|修正|変更|実装|作成|追加|削除|反映)/.test(text)
  const imageExplanation = /(?:画像|スクリーンショット|写真|画面).*(?:説明|読ん|確認|文字起こし|何が|内容)/s.test(text)
  if (imageAttached && imageExplanation && !changeRequested) {
    return {
      agent: "vision",
      reason: "画像の読取り専用依頼",
      model: "qwen3-vl-64k:latest",
    }
  }

  const debugSignal = /(?:不具合|バグ|エラー|例外|失敗|動かない|ログ)/.test(text)
  const debugAction = /(?:原因|調査|診断|解析|直して|修正|解決|対応)/.test(text)
  if (debugSignal && debugAction) {
    return { agent: "debug", reason: "不具合の調査・修正" }
  }

  const docsSignal = /(?:README|AGENTS\.md|Markdown|マークダウン|ドキュメント|\.md\b)/i.test(text)
  const docsAction = /(?:作成|更新|修正|書いて|追記|整理|変更)/.test(text)
  const codeSignal = /(?:Python|TypeScript|JavaScript|コード|実装|API|テスト|\.py\b|\.ts\b)/i.test(text)
  if (docsSignal && docsAction && !codeSignal) {
    return { agent: "docs-writer", reason: "Markdown文書だけの編集" }
  }

  return { agent: "code", reason: "通常の開発・ファイル処理" }
}

const server: Plugin = async () => ({
  "chat.message": async (input, output) => {
    const text = textFrom(output.parts)
    // 専門モードの明示選択は尊重する。ただしOffice依頼だけは、専用の
    // excel_comment等を持つcodeへ戻さないと処理不能になるため例外とする。
    if (input.agent && input.agent !== "code" && !isOfficeRequest(text)) return

    const route = selectRoute(text, hasImage(output.parts))
    output.message.agent = route.agent
    if (route.model) {
      output.message.model = { providerID: "ollama", modelID: route.model }
    }

    output.message.system = appendSystemInstruction(
      output.message.system,
      `内部ルーティング: ${route.agent}（${route.reason}）。この情報を利用者向け回答へ書かない。`,
    )
  },
})

export default { id: "automatic-context-router", server }
