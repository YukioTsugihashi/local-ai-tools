import { mkdirSync, readFileSync, writeFileSync } from "node:fs"
import path from "node:path"

import { tool, type Plugin } from "@kilocode/plugin"
import type { Part } from "@kilocode/sdk"

type Stage = { name: string; change: string; verify: string }
type SessionState = {
  objective: string
  deployRequested: boolean
  planned: boolean
  edited: boolean
  testsPassed: boolean
  dryRunPassed: boolean
  deployed: boolean
  stages: Stage[]
  currentStage: number
  stageDirty: boolean
  evidence: string[]
}

const sessions = new Map<string, SessionState>()

// 工程計画がVS Codeの再起動で消えると、途中から再開できず作り直しになる。
// 保存も復元も失敗して構わない性質なので、例外は全て握って進行を止めない。
const STATE_FILE = path.join(process.cwd(), ".kilo", "stage-guard-state.json")
let restored = false

const restoreSessions = () => {
  if (restored) return
  restored = true
  try {
    const stored = JSON.parse(readFileSync(STATE_FILE, "utf8")) as Record<string, SessionState>
    for (const [sessionID, state] of Object.entries(stored)) sessions.set(sessionID, state)
  } catch {
    // 状態ファイルが無い・壊れている場合は何も復元しない
  }
}

const persistSessions = () => {
  try {
    // 計画前の状態は次のメッセージで作り直せるため保存しない
    const planned = [...sessions].filter(([, state]) => state.planned)
    mkdirSync(path.dirname(STATE_FILE), { recursive: true })
    writeFileSync(STATE_FILE, JSON.stringify(Object.fromEntries(planned)), "utf8")
  } catch {
    // 保存できなくても進行は止めない(再起動時に計画が消えるだけ)
  }
}
const EDIT_TOOLS = new Set(["edit", "write", "apply_patch", "notebook_edit"])
const TEST_COMMAND = /(?:pytest|unittest|npm\s+(?:test|run\s+test)|pnpm\s+test|cargo\s+test|go\s+test)/i
const DEPLOY_COMMAND = /open_webui_deploy\.py/i
const APPLY_COMMAND = /open_webui_deploy\.py[^\r\n]*\s--apply(?:\s|$)/i
const DRY_RUN_COMMAND = /open_webui_deploy\.py\s+[^\s]+\.py(?:\s|$)/i

const messageText = (parts: Part[]) =>
  parts
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("\n")
    .trim()

export const isComplexChange = (text: string) => {
  const change = /(?:実装|追加|変更|修正|統合|移植|構築|登録|デプロイ|作り直)/.test(text)
  if (!change) return false
  const domains = [
    /(?:API|エンドポイント|bridge|ブリッジ)/i,
    /(?:Pipe|Tool|Open\s*WebUI)/i,
    /(?:Docker|コンテナ|compose)/i,
    /(?:テスト|pytest|E2E|回帰)/i,
    /(?:登録|デプロイ|反映|--apply)/i,
    /(?:TypeScript|Python|\.ts\b|\.py\b)/i,
  ].filter((pattern) => pattern.test(text)).length
  const paths = text.match(/[\w.-]+(?:[\\/][\w.-]+)+/g)?.length ?? 0
  // 「.pyを修正してテスト」だけで2ドメインに達してしまい、単純な修正まで
  // 段階管理へ落ちていた。第3の観点かファイル数の裏づけを要求する
  return /(?:統合|移植|複数ファイル|段階|アーキテクチャ)/.test(text) || domains >= 3 || paths >= 4
}

const parsePlan = (raw: string): Stage[] => {
  let value: unknown
  try {
    value = JSON.parse(raw)
  } catch {
    throw new Error("plan_jsonはJSON配列で指定してください")
  }
  if (!Array.isArray(value) || value.length < 3 || value.length > 10) {
    throw new Error("複雑な変更は3〜10工程に分割してください")
  }
  return value.map((item, index) => {
    if (!item || typeof item !== "object") throw new Error(`工程${index + 1}が不正です`)
    const record = item as Record<string, unknown>
    const stage = {
      name: String(record.name ?? "").trim(),
      change: String(record.change ?? "").trim(),
      verify: String(record.verify ?? "").trim(),
    }
    if (!stage.name || !stage.change || !stage.verify) {
      throw new Error(`工程${index + 1}にはname・change・verifyが必要です`)
    }
    return stage
  })
}

const stateSummary = (state: SessionState) => [
  `目的: ${state.objective}`,
  `計画: ${state.planned ? `${state.stages.length}工程` : "未登録"}`,
  `現在工程: ${state.planned ? `${state.currentStage + 1}/${state.stages.length} ${state.stages[state.currentStage]?.name ?? ""}` : "未開始"}`,
  `編集: ${state.edited ? "あり" : "なし"}`,
  `テスト: ${state.testsPassed ? "成功" : "未成功"}`,
  `dry-run: ${state.dryRunPassed ? "成功" : "未成功"}`,
  `本番反映: ${state.deployed ? "成功" : "未実施"}`,
  `デプロイ予定: ${state.deployRequested ? "あり" : "なし"}`,
].join("\n")

const server: Plugin = async () => ({
  tool: {
    stage_guard: tool({
      description:
        "複雑な変更の工程計画を登録・確認する。編集前にplanを1回実行し、各工程に変更内容と検証方法を入れる。",
      args: {
        action: tool.schema.enum(["plan", "status", "advance", "complete", "reset"]),
        objective: tool.schema.string().optional(),
        plan_json: tool.schema.string().optional().describe(
          '[{"name":"調査","change":"対象を確認","verify":"依存関係を照合"}, ...] の形式',
        ),
        deploy_requested: tool.schema.boolean().optional(),
        evidence: tool.schema.string().optional().describe("advance時の確認結果。実行したテストや照合内容を具体的に書く"),
      },
      async execute(args, context) {
        restoreSessions()
        const current = sessions.get(context.sessionID)
        if (args.action === "status") {
          return current ? stateSummary(current) : "このセッションに段階管理中の変更はありません"
        }
        if (args.action === "reset") {
          // 計画が実態と合わなくなったとき、作業ごと詰まないための出口
          sessions.delete(context.sessionID)
          persistSessions()
          return "段階管理を解除しました。必要ならplanで登録し直してください"
        }
        if (args.action === "plan") {
          const objective = args.objective?.trim()
          if (!objective || !args.plan_json) throw new Error("planにはobjectiveとplan_jsonが必要です")
          const state: SessionState = {
            objective,
            deployRequested: Boolean(args.deploy_requested) || Boolean(current?.deployRequested),
            planned: true,
            edited: false,
            testsPassed: false,
            dryRunPassed: false,
            deployed: false,
            stages: parsePlan(args.plan_json),
            currentStage: 0,
            stageDirty: false,
            evidence: [],
          }
          sessions.set(context.sessionID, state)
          persistSessions()
          return `段階計画を登録しました。\n${stateSummary(state)}`
        }
        if (!current) throw new Error("対象の段階計画がありません。resetで解除するか、planで登録してください")
        if (args.action === "advance") {
          const evidence = args.evidence?.trim()
          if (!evidence) throw new Error("次工程へ進むには具体的な確認結果evidenceが必要です")
          if (current.stageDirty) {
            throw new Error("現在工程の編集後テストが成功していないため次工程へ進めません")
          }
          if (current.currentStage >= current.stages.length - 1) {
            throw new Error("最終工程です。必要な確認後にcompleteしてください")
          }
          current.evidence.push(`${current.stages[current.currentStage].name}: ${evidence}`)
          current.currentStage += 1
          persistSessions()
          return `次工程へ進みました。\n${stateSummary(current)}`
        }
        // 計画より少ない工程で目的を達することはある。理由を書けば打ち切れる
        if (current.currentStage < current.stages.length - 1 && !args.evidence?.trim()) {
          throw new Error(
            "未完了の工程があります。advanceで順番に進めるか、残り工程が不要になった理由をcompleteのevidenceへ書いてください",
          )
        }
        if (current.edited && !current.testsPassed) {
          throw new Error("編集後のテスト成功が確認できないため完了できません")
        }
        if (current.deployRequested && !current.deployed) {
          throw new Error("依頼された本番反映の成功が確認できないため完了できません")
        }
        sessions.delete(context.sessionID)
        persistSessions()
        return "段階計画を完了しました"
      },
    }),
  },

  "chat.message": async (input, output) => {
    restoreSessions()
    const text = messageText(output.parts)
    if (!isComplexChange(text)) return
    if (!sessions.has(input.sessionID)) {
      sessions.set(input.sessionID, {
        objective: text.slice(0, 300),
        deployRequested: /(?:登録|デプロイ|反映|--apply)/i.test(text),
        planned: false,
        edited: false,
        testsPassed: false,
        dryRunPassed: false,
        deployed: false,
        stages: [],
        currentStage: 0,
        stageDirty: false,
        evidence: [],
      })
    }
    output.message.tools = { ...output.message.tools, stage_guard: true }
    output.message.system = [
      output.message.system,
      "この依頼は複雑な変更として段階管理されています。調査後、編集前にstage_guardのplanを呼び、3〜10工程をname/change/verify付きで登録してください。工程ごとに対象を限定し、検証成功後に次へ進んでください。編集後のテスト成功前にデプロイせず、Open WebUIの--apply前には必ずdry-runを成功させてください。登録成功と業務処理のE2E成功は別々に確認してください。段階管理が依頼の実態と合っていない場合はstage_guard(action=reset)で解除してよいです。",
    ].filter(Boolean).join("\n\n")
  },

  "tool.execute.before": async (input, output) => {
    restoreSessions()
    const state = sessions.get(input.sessionID)
    if (!state) return
    if (EDIT_TOOLS.has(input.tool) && !state.planned) {
      throw new Error(
        "複雑な変更は編集前にstage_guard(action=plan)で工程計画を登録してください。" +
          "段階管理が不要な依頼だった場合はstage_guard(action=reset)で解除できます。",
      )
    }
    if (input.tool !== "bash") return
    const command = String(output.args?.command ?? "")
    if (APPLY_COMMAND.test(command)) {
      if (state.currentStage < state.stages.length - 1) {
        throw new Error("未完了の工程があるためOpen WebUIへ--applyできません")
      }
      if (!state.testsPassed) throw new Error("テスト成功前はOpen WebUIへ--applyできません")
      if (!state.dryRunPassed) throw new Error("同じセッションでdry-runに成功してから--applyしてください")
    }
  },

  "tool.execute.after": async (input, output) => {
    restoreSessions()
    const state = sessions.get(input.sessionID)
    if (!state) return
    if (EDIT_TOOLS.has(input.tool)) {
      state.edited = true
      state.testsPassed = false
      state.dryRunPassed = false
      state.deployed = false
      state.stageDirty = true
      persistSessions()
      return
    }
    if (input.tool !== "bash") return
    const command = String(input.args?.command ?? "")
    if (TEST_COMMAND.test(command)) {
      state.testsPassed = true
      state.stageDirty = false
    }
    if (
      DEPLOY_COMMAND.test(command) &&
      DRY_RUN_COMMAND.test(command) &&
      !APPLY_COMMAND.test(command)
    ) state.dryRunPassed = true
    if (APPLY_COMMAND.test(command)) state.deployed = true
    persistSessions()
  },
})

export default { id: "staged-change-guard", server }
