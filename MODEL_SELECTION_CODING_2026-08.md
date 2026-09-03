# コーディング用ローカルLLM選定調査(2026-08-14)

qwen3-coder(30B-A3B)の後継として、Kilo Codeのcode/debug役に使うモデルの選定調査。
**方針: 速度より正確性・確実な動作を優先**(ユーザー指定)。モデルの取得・設定変更は未実施。

## 結論(推奨)

| 順位 | モデル | 理由の要約 |
|---|---|---|
| **主推奨** | **qwen3.6:27b**(Q4 17GB) | 現行qwen3-coderと同一開発元の後継世代。同一評価系列内で現行系より明確に上位。ツール呼び出しが現行と同じ`qwen3_coder`パーサ系統で**Kilo/Ollama互換リスクが最小**。VRAM 32GBに完全収容。Apache 2.0 |
| 対抗 | muse-glimmer:30b(Q4 18GB) | Meta公表のagentic評価が高水準+ツール失敗リカバリ調整済み。Apache 2.0。ただしリリース4日目で実績薄・日本語明示なし |
| 条件付き | mistral-medium-3.5:128b(Q4 80GB) | 日本語明示・自己申告値は最高クラス。ただし128B denseでVRAM超過→推定1〜2 tok/s。Kiloの対話ループには非現実的。夜間バッチ的用途なら |

---

## 1. 調査手順(ユーザー指定の方法論)

1. 公式Ollamaの「Coding」カテゴリ一覧を候補母集団にする → **20モデル**
2. クラウド専用・128GB RAMにQ4が収まらないものを除外 → **11モデル残**
3. 各開発元の公式モデルカードで構造・コード評価・ツール対応・日本語・ライセンスを確認
4. ベンチマークの版・実行基盤が違う数値は横比較しない(同一開発元・同一ハーネス内のみ順位づけに使用)
5. 実機とKiloの条件で最終絞り込み

調査実施: 2026-08-14。Ollamaページ+各開発元の公式一次情報(HFモデルカード・公式ブログ)のみを根拠とし、第三者記事は不使用。
※ 数値はWebページからの機械抽出を経ているため、**導入決定前に該当モデルカードの表を目視確認することを推奨**。

## 2. 実機・Kilo条件(絞り込みの基準)

- **GPU: RTX 5090(VRAM 32GB)**、RAM 128GB、i7-14700KF(実測: nvidia-smi / Win32_ComputerSystem)
- 動作実績: gpt-oss:120b(65GB・MoE)がRAMオフロードで動作済み → 65GB級まで実証あり
- Ollama経由でKilo Codeから利用。**ツール呼び出しの安定性が最重要**(現行qwen3-coderは稀に生テキストでツール呼び出しを吐く既知の揺れあり)
- コンテキスト64Kの派生Modelfile(-64k)を作って運用する方式
- 応答は日本語必須(現行は中国語応答対策ルールで管理中)
- 完全オフライン化前にpullを済ませる必要あり。商用利用可のライセンス必須(病院内利用)

## 3. Step 1-2: 母集団20モデルと除外結果

| # | モデル | 判定 | 根拠 |
|---|---|---|---|
| 1 | glm-5.2 | ❌ クラウド専用 | `:cloud`タグのみ(756B) |
| 2 | deepseek-v4-flash | ❌ クラウド専用 | 全タグ`-cloud`(284B-A13B) |
| 3 | kimi-k3 | ❌ クラウド専用 | `:cloud`のみ(2.8T)+有料サブスク前提 |
| 4 | muse-glimmer | ✅ 候補 | 30b=18GB、tools/thinking/vision、128K |
| 5 | nemotron-3.5-lightning | ✅ 候補 | 30b=25GB、tools/thinking、1M ctx |
| 6 | gemma4 | ✅ 候補 | 12b/26b/31b=7.6〜20GB、tools/vision/thinking、256K |
| 7 | qwen3.5 | ✅ 候補 | 27b/35b/122b=17〜81GB、tools/vision/thinking、256K |
| 8 | qwen3.6 | ✅ 候補 | 27b=17GB / 35b=24GB、tools/vision/thinking、256K |
| 9 | glm-5.1 | ❌ クラウド専用 | `:cloud`のみ |
| 10 | minimax-m2.7 | ❌ クラウド専用 | `:cloud`のみ(229B) |
| 11 | nemotron-3-super | ✅ 候補 | 120b=87GB(ローカル可)、tools/thinking、256K |
| 12 | ornith | ✅ 候補 | 9b=5.6GB / 35b=21GB、tools、256K |
| 13 | minimax-m3 | ❌ クラウド専用 | `:cloud`のみ |
| 14 | nemotron3(Nano Omni) | ✅ 候補 | 33b=28GB、マルチモーダル、128K |
| 15 | lfm2 | ✅ 候補 | 24b=14GB、tools、32K |
| 16 | kimi-k2.7-code | ❌ クラウド専用 | `:cloud`のみ(1.04T) |
| 17 | granite4.1 | ✅ 候補 | 3b/8b/30b=2.1〜17GB、tools、128K |
| 18 | mistral-medium-3.5 | ✅ 候補 | 128b=80GB(ローカル可)、tools/vision/thinking、256K |
| 19 | kimi-k2.6 | ❌ クラウド専用 | `:cloud`のみ(1.04T) |
| 20 | deepseek-v4-pro | ❌ クラウド専用 | 全タグ`-cloud`(1.6T-A49B) |

**「Q4が128GBに収まらない」による除外は0件**(ローカル最大はnemotron-3-super:120bの87GB)。
ただし実機はVRAM 32GBなので、**24GB以下=GPU完全収容で高速/それ以上=RAMオフロードで大幅低速**の二層に分かれる。

## 4. Step 3: 公式モデルカード調査(候補11)

ベンチ数値は**各開発元の自己申告値**。ハーネス・条件が異なるため**表をまたいだ横比較は不可**(§5参照)。

### Qwen3.6 / Qwen3.5(Alibaba Qwen)— 同一開発元評価系列
出典: huggingface.co/Qwen/Qwen3.6-27B ほか各カード、github.com/QwenLM/Qwen3.6

| モデル | 構造 | SWE-bench Verified | LiveCodeBench v6 | Terminal-Bench 2.0 |
|---|---|---|---|---|
| **Qwen3.6-27B**(2026-04-22) | dense 27B(Gated DeltaNetハイブリッド) | **77.2** | **83.9** | **59.3** |
| Qwen3.6-35B-A3B(2026-04-16) | MoE 総35B/A3B | 73.4 | 80.4 | 51.5 |
| Qwen3.5-27B(2026-02-24) | dense 27B | 72.4 | 80.7 | 41.6 |
| Qwen3.5-35B-A3B | MoE 総35B/A3B | 69.2 | 74.6 | 40.5 |
| Qwen3.5-122B-A10B | MoE 総122B/A10B | 72.0 | 78.9 | 49.4 |
| (参考)Qwen3-Coder-Next(2026-01-30、Ollama Coding一覧外) | MoE 総80B/A3B | 70.6 | — | 36.2 |

- SWE-bench系の条件(3.6のカード脚注): 「Internal agent scaffold (bash + file-edit tools); temp=1.0, top_p=0.95, 200K context」
- **Qwen3.5/3.6世代にCoder専用モデルは存在しない**(HF公式org検索で確認)。汎用モデルがagentic codingを主力用途として吸収し、ツールパーサ名も汎用モデルで`qwen3_coder`のまま
- ツール呼び出し: 公式対応(`--tool-call-parser qwen3_coder`)。「excels in tool calling capabilities」明記
- 全モデルVisionエンコーダ付き。コンテキスト262Kネイティブ(→1M拡張可)。thinkingがデフォルト
- 日本語: 「201言語・方言対応」とのみ公表、**日本語の名指しはなし**(前世代Qwen3では119言語リストに日本語明示があった)
- ライセンス: **Apache 2.0**

### Muse Glimmer 30B(Meta、2026-08-10)
出典: research.meta.ai/blog/introducing-muse-glimmer-open-agentic-model、huggingface.co/meta-models/Muse-Glimmer-30B

- dense 30B+視覚エンコーダ、コンテキスト131K。「always-on local agents」向け、ツール失敗リカバリにチューニングと公称
- Meta自社ハーネス(bash+ファイル編集scaffold、4回平均): **SWE-bench Verified 76.0 / SWE-bench Pro 51.2 / Terminal-Bench 2.1 51.7**
- ツール呼び出し: 公式対応(精密スキーマ・長ワークフロー対応を明記)
- 日本語: 「100+言語で学習」のみ、**名指しなし**。「強サポート言語外では劣化しうる」の注記あり
- ライセンス: **Apache 2.0**。HF orgは`meta-models`(Verified済み)

### Mistral Medium 3.5(128B、2026-04-28 GA)
出典: mistral.ai/news/vibe-remote-agents-mistral-medium-3-5/、huggingface.co/mistralai/Mistral-Medium-3.5-128B

- **dense 128B**+Vision、コンテキスト256K、reasoning切替可
- **SWE-bench Verified 77.6%**(ただし**ハーネス・条件は非公表**)、τ³-Telecom 91.4
- Devstral系(コーディング特化線)は全てdeprecatedで本モデルに統合。「supersedes all our previous coding models」
- ツール呼び出し: 公式対応(native function calling / JSON出力)。Ollamaを公式推論スタックに列挙
- 日本語: **対応言語に明示**
- ライセンス: **Modified MIT**(前月の全世界連結**月間**収益$20M超の企業は利用不可。病院規模なら商用可)

### NVIDIA Nemotron 3系
出典: huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16 ほか

| モデル | 構造 | コード評価(NVIDIA自社NeMoハーネス) | 日本語 | ライセンス |
|---|---|---|---|---|
| 3.5 Lightning 30B-A3B(2026-08-11) | Mamba-2+MoEハイブリッド、1M ctx | SWE-bench Verified **51.56** / Terminal-Bench 2.1 24.58 | 明示あり(英語主) | OpenMDW-1.1 |
| 3 Super 120B-A12B(2026-03-11) | LatentMoEハイブリッド、1M ctx | **SWE-bench未公表**。LiveCodeBench v6 78.44(FP8) | 明示あり | Nemotron Open Model License |
| 3 Nano Omni 31B-A3B(2026-04-28) | マルチモーダルMoE、256K | コード評価公表なし | **英語のみ** | NVIDIA Open Model Agreement |

- 3モデルともツール呼び出し公式対応。ただし注記「NVIDIAの一貫ハーネスによる測定であり、各社の自己申告値とは異なりうる」→ 他社の自己申告値との直接比較不可

### ornith(DeepReinforce Team、2026-06-25)
出典: ornith.ai/ornith_1_0.html、huggingface.co/ornith-ai

- 9B dense / 35B MoE(**Qwen3.5・Gemma4ベースのポストトレーニング**)、262K ctx、MIT
- 自社公表(OpenHandsハーネス): SWE-bench Verified 9B=69.4 / 35B=**75.6**、Terminal-Bench 2.1(Terminus-2)35B=64.2
- ツール呼び出し対応(`qwen3_xml`パーサ)。日本語: 記載なし
- **開発元の法人素性が不透明**(所在地・資金・設立情報なし、HF orgはドメイン認証なし・公開メンバー1名)。arXiv実績(CUDA-L1/L2)はあり

### Granite 4.1(IBM、2026-04-29)
出典: huggingface.co/ibm-granite/granite-4.1-30b ほか、research.ibm.com/blog/granite-4-1-ai-foundation-models

- dense 3B/8B/30B、128K ctx、**Apache 2.0**、**日本語明示**(12言語)
- コード評価は静的ベンチ中心: HumanEval 30B=88.41、BigCodeBench 38.77。**SWE-bench系の公表なし**
- ツール呼び出し公式対応(BFCL v3=73.68@30B)

### Gemma 4(Google、2026-03-31)
出典: blog.google/innovation-and-ai/technology/developers-tools/gemma-4/、ai.google.dev/gemma/docs/core/model_card_4

- 31B dense / 26B MoE(A3.8B)/ 12B dense ほか、256K ctx、**Apache 2.0**(旧Gemma規約の適用外を明記)
- コード評価: LiveCodeBench v6 31B=**80.0** / 26B=77.1、Codeforces ELO 31B=2150。**SWE-bench系の公表なし**
- ツール呼び出し公式対応(native function calling / 構造化JSON)。日本語: 「140+言語」のみで名指しなし(MMMLU 31B=88.4)
- 実機にgemma4:26b / 12bがpull済み

### LFM2-24B-A2B(Liquid AI、2026-02-24)
出典: huggingface.co/LiquidAI/LFM2-24B-A2B

- モデルカードに「**We don't recommend using it for coding**」と明記 → **コーディング用途としては除外**
- コンテキスト32KでKiloの64K運用にも不適合

## 5. ベンチ数値の取り扱い(重要)

横比較してよいのは**同一開発元・同一ハーネスの数値のみ**:
- Qwen系列内: Qwen3.6-27B(77.2)> 3.6-35B(73.4)> 3.5-122B(72.0)≈ 3.5-27B(72.4)> Coder-Next(70.6)→ **27B denseが自社系列の頂点**
- NVIDIA NeMoハーネスの51.56(Lightning)とQwen自社scaffoldの77.2は**比較不可**(測定系が違う)
- Mistralの77.6は条件非公表のため**他社との比較には使えない**(NVIDIA自身も「自己申告値とは異なりうる」と注記)
- Meta(76.0)・ornith(75.6)もそれぞれ自社測定・別ハーネス

→ 序列を確実に言えるのは「**Qwen系列の中でQwen3.6-27Bが現行qwen3-coder世代より上**」という一点。他社モデルは「同水準帯の可能性がある」以上のことは公表値からは言えない。

## 6. Step 5: 実機+Kilo条件での最終評価

| 候補 | VRAM収容(Q4+64K KV) | ツール互換 | 日本語 | ライセンス | 総合 |
|---|---|---|---|---|---|
| **qwen3.6:27b** | ✅ 17GB→完全収容 | ◎ 現行と同一パーサ系統・運用実績あり | △ 明示なし(現行Qwenで運用実績あり) | Apache 2.0 | **主推奨** |
| muse-glimmer:30b | ✅ 18GB→完全収容 | ○ 公式対応(実績4日) | △ 明示なし・注記あり | Apache 2.0 | 対抗(要検証) |
| qwen3.6:35b | ⚠ 24GB→ほぼ収容 | ◎ 同上 | △ | Apache 2.0 | 27bの速度代替 |
| gemma4:31b | ✅ 20GB | ○ | △ | Apache 2.0 | agentic実務評価(SWE-bench)の公表なし |
| mistral-medium-3.5:128b | ❌ 80GB→大部分RAM | ○ | ◎ 明示 | Modified MIT(病院規模は可) | dense 128Bで推定1〜2 tok/s。対話ループ不適 |
| nemotron-3-super:120b | ❌ 87GB→大部分RAM | ○ | ◎ 明示 | Nemotron License | SWE-bench未公表で根拠不足 |
| nemotron-3.5-lightning:30b | ⚠ 25GB | ○ | ◎ 明示 | OpenMDW-1.1 | 自社ハーネス値が控えめ・積極材料不足 |
| ornith:35b | ✅ 21GB | ○ | ✕ 記載なし | MIT | 開発元素性不透明→病院オフライン環境に不適 |
| granite4.1:30b | ✅ 17GB | ○ | ◎ 明示 | Apache 2.0 | agentic評価の公表なし・静的ベンチ中心 |
| nemotron3:33b | ✅ 28GB | ○ | ✕ **英語のみ** | NVIDIA OMA | 除外(コード評価も公表なし) |
| lfm2:24b | ✅ 14GB | ○ | ◎ 明示 | LFM License | 除外(開発元がコーディング非推奨を明記) |

### 主推奨の根拠(qwen3.6:27b)

1. **確実性**: 現行と同じQwen系のチャットテンプレート/ツールパーサ(`qwen3_coder`)なので、Kilo+Ollamaのツール呼び出し経路の互換リスクが候補中最小。日本語強制ルール・柵など既存の運用ノウハウがそのまま効く
2. **正確性**: 唯一横比較が成立する自社系列内で現行世代を明確に上回る(SWE-bench V 77.2、脚注に条件明記)。thinkingデフォルト=遅くなるが「時間がかかってもよい」方針に合致
3. **実機適合**: Q4 17GBでRTX 5090に完全収容(ハイブリッド線形注意のためKVキャッシュも小さい)。現行30B-A3Bより高精度クラスでありながらGPU内で完結
4. Apache 2.0で再配布・オフライン恒久保管も問題なし

### リスクと対処

- 日本語の公式明示なし → 現行Qwen運用と同じ既知リスク。既存の「応答は必ず日本語」ルール+導入時スモークで確認
- thinkingモデルをKilo code/debug役で使うのは初(plan役gpt-ossで実績あり)→ CLIスモークで要検証
- 万一27b denseの体感速度が許容外なら qwen3.6:35b(A3B MoE=現行と同速度クラス)に切替

## 7. 導入する場合の手順案(未実施)

1. `ollama pull qwen3.6:27b`(17GB。**オフライン化前に必須**)
2. `qwen3.6-27b-64k` Modelfileを作成(num_ctx 65536。D:\offline-kit\modelfiles\ に保管)
3. kilo.jsonc のcode/debug/orchestrator系を差し替え(qwen3-coder-64kは併存・即時切り戻し可能に)
4. CLIスモークテスト: ツール呼び出し安定性・日本語応答・柵の拒否/許可・excel_commentプラグイン経路
5. 数日並走して問題なければ既定化。D:\offline-kit\ へのモデルblobバックアップも実施

## 8. 出典一覧

### 母集団・除外判定
- https://ollama.com/search?c=coding (母集団20件)
- https://ollama.com/library/{glm-5.2, deepseek-v4-flash, kimi-k3, muse-glimmer, nemotron-3.5-lightning, gemma4, qwen3.5, qwen3.6, glm-5.1, minimax-m2.7, nemotron-3-super, ornith, minimax-m3, nemotron3, lfm2, kimi-k2.7-code, granite4.1, mistral-medium-3.5, kimi-k2.6, deepseek-v4-pro} (各タグ・サイズ・cloud判定)

### 公式モデルカード・発表
- Qwen: https://huggingface.co/Qwen/Qwen3.6-27B / Qwen3.6-35B-A3B / Qwen3.5-27B / Qwen3.5-35B-A3B / Qwen3.5-122B-A10B / Qwen3-Coder-Next、https://github.com/QwenLM/Qwen3.6 / Qwen3.5 / Qwen3-Coder
- Meta: https://research.meta.ai/blog/introducing-muse-glimmer-open-agentic-model、https://research.meta.ai/static/muse-glimmer-methodology(評価方法論PDF)、https://huggingface.co/meta-models/Muse-Glimmer-30B、https://developer.meta.com/ai/models/muse-glimmer/
- Mistral: https://mistral.ai/news/vibe-remote-agents-mistral-medium-3-5/、https://huggingface.co/mistralai/Mistral-Medium-3.5-128B(+ /raw/main/LICENSE)、https://docs.mistral.ai/models/model-cards/mistral-medium-3-5-26-04
- NVIDIA: https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16、https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-FP8、https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16、developer.nvidia.com各発表ブログ
- Google: https://blog.google/innovation-and-ai/technology/developers-tools/gemma-4/、https://ai.google.dev/gemma/docs/core/model_card_4、https://ai.google.dev/gemma/docs/releases、https://ai.google.dev/gemma/terms
- DeepReinforce: https://ornith.ai/ornith_1_0.html、https://huggingface.co/ornith-ai/Ornith-1.0-35B(+ 9B、各config.json)
- Liquid AI: https://huggingface.co/LiquidAI/LFM2-24B-A2B、https://www.liquid.ai/blog/lfm2-24b-a2b、https://www.liquid.ai/lfm-license
- IBM: https://huggingface.co/ibm-granite/granite-4.1-30b(+ 8b / 3b)、https://research.ibm.com/blog/granite-4-1-ai-foundation-models

### 実機条件
- nvidia-smi(RTX 5090 / 32,607 MiB)、Win32_ComputerSystem(RAM 127.8GB)、ollama list(gpt-oss:120b=65GB動作実績)— 2026-08-14実測
