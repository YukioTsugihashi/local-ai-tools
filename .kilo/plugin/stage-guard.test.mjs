// 段階管理の発火条件の実行時テスト。
// 緩めすぎると単純な修正まで止まり、厳しくしすぎると大改造が素通りする。

import assert from "node:assert/strict"
import test from "node:test"

import { isComplexChange } from "./stage-guard.ts"

test("単純な修正では段階管理へ落とさない", () => {
  // 以前は「.py」+「テスト」の2種で発火し、この程度でも止まっていた
  assert.equal(isComplexChange("office_excel.py を修正してテストして"), false)
  assert.equal(isComplexChange("この関数のバグを直して"), false)
  assert.equal(isComplexChange("READMEを更新して"), false)
})

test("複数領域にまたがる変更は段階管理へ落とす", () => {
  assert.equal(
    isComplexChange(
      "Open WebUIのPipeとDockerのbridgeを変更してpytestを通してから--applyで反映して",
    ),
    true,
  )
  // 「統合」「移植」などは単独でも対象
  assert.equal(isComplexChange("この機能を移植して"), true)
  assert.equal(isComplexChange("アーキテクチャを変更して"), true)
})

test("変更を伴わない依頼は対象外", () => {
  assert.equal(isComplexChange("Pipeとbridgeとテストの構成を教えて"), false)
  assert.equal(isComplexChange("ログを見せて"), false)
})

test("多数のパスを含む依頼は対象", () => {
  assert.equal(
    isComplexChange(
      "tools/a.py と tools/b.py と app/c.py と app/d.py を修正して",
    ),
    true,
  )
})
