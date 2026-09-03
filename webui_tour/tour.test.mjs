/*
 * tour.js の判定部分の実行時テスト。
 *
 * ブラウザが無いので画面は作れない。代わりに、壊れると案内が黙って
 * おかしくなる部分(目印の解決・位置の計算・台本の検査)だけを直接呼ぶ。
 * tour.js は document が無い環境では画面へ触らず api だけ載せて終わるので、
 * node からそのまま読み込める。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
vm.runInThisContext(readFileSync(join(HERE, 'tour.js'), 'utf8'), { filename: 'tour.js' });
const tour = globalThis.__WEBUI_TOUR__;

const VIEW = { width: 1000, height: 800 };
const rect = (left, top, width, height) => ({ left, top, width, height });
const element = (width = 20, height = 20) => ({
	getBoundingClientRect: () => rect(0, 0, width, height)
});

test('document が無い環境でも読み込めて、判定だけ使える', () => {
	assert.ok(tour, 'tour.js が api を載せていません');
	assert.equal(typeof tour.parseTourId, 'function');
	assert.equal(typeof tour.start, 'undefined', 'node では画面側の関数を載せない');
});

test('URLに指定が無ければ案内を始めない', () => {
	assert.equal(tour.parseTourId({ hash: '', search: '' }), null);
	assert.equal(tour.parseTourId({ hash: '#/chat/abc', search: '?x=1' }), null);
	assert.equal(tour.parseTourId(null), null);
});

test('#tour= と ?tour= のどちらでも台本idを取り出す', () => {
	assert.equal(tour.parseTourId({ hash: '#tour=excel-comment', search: '' }), 'excel-comment');
	assert.equal(tour.parseTourId({ hash: '', search: '?tour=excel-comment' }), 'excel-comment');
	assert.equal(tour.parseTourId({ hash: '', search: '?a=1&tour=abc_1' }), 'abc_1');
});

test('候補セレクタは順に試し、最初に見つかったものを使う', () => {
	const wanted = element();
	const doc = { querySelector: (sel) => (sel === '#second' ? wanted : null) };
	assert.equal(tour.resolveAnchor(doc, ['#first', '#second', '#third']), wanted);
});

test('壊れたセレクタが混じっても案内を止めず次の候補へ進む', () => {
	const wanted = element();
	const doc = {
		querySelector: (sel) => {
			if (sel === 'bad[[') throw new Error('不正なセレクタ');
			return sel === '#ok' ? wanted : null;
		}
	};
	assert.equal(tour.resolveAnchor(doc, ['bad[[', '#ok']), wanted);
});

test('畳まれていて大きさが0の要素は見つからなかった扱いにする', () => {
	const collapsed = element(0, 0);
	const doc = { querySelector: () => collapsed };
	assert.equal(tour.resolveAnchor(doc, ['#hidden']), null);
});

test('どの候補にも当たらなければ null(呼び出し側が案内を中止する)', () => {
	const doc = { querySelector: () => null };
	assert.equal(tour.resolveAnchor(doc, ['#a', '#b']), null);
	assert.equal(tour.resolveAnchor(doc, []), null);
});

test('穴は対象より少し大きく、画面外へははみ出さない', () => {
	const box = tour.spotlightBox(rect(100, 100, 50, 20), VIEW, 8);
	assert.deepEqual(box, { left: 92, top: 92, width: 66, height: 36 });

	const atEdge = tour.spotlightBox(rect(0, 0, 40, 40), VIEW, 8);
	assert.equal(atEdge.left, 0, '左端でマイナスにならない');
	assert.equal(atEdge.top, 0);

	const overflow = tour.spotlightBox(rect(980, 780, 40, 40), VIEW, 8);
	assert.equal(overflow.left + overflow.width, VIEW.width, '右端を超えない');
	assert.equal(overflow.top + overflow.height, VIEW.height, '下端を超えない');
});

test('吹き出しは下に入るなら下、入らないなら上へ置く', () => {
	const size = { width: 320, height: 150 };
	const below = tour.bubblePlacement(rect(400, 100, 100, 40), VIEW, size, 12);
	assert.equal(below.placement, 'below');
	assert.ok(below.top >= 152);

	const above = tour.bubblePlacement(rect(400, 700, 100, 40), VIEW, size, 12);
	assert.equal(above.placement, 'above');
	assert.ok(above.top >= 0);
});

test('上下に入らなければ横、どこにも入らなければ画面中央へ逃がす', () => {
	const size = { width: 320, height: 150 };
	const tall = tour.bubblePlacement(rect(10, 0, 60, 800), VIEW, size, 12);
	assert.equal(tall.placement, 'right');

	const huge = tour.bubblePlacement(rect(0, 0, 1000, 800), VIEW, size, 12);
	assert.equal(huge.placement, 'center');
	assert.ok(huge.left >= 0 && huge.left + size.width <= VIEW.width);
});

test('吹き出しは必ず画面の中に収まる', () => {
	const size = { width: 320, height: 150 };
	for (const target of [rect(0, 0, 10, 10), rect(990, 0, 10, 10), rect(990, 790, 10, 10)]) {
		const spot = tour.bubblePlacement(target, VIEW, size, 12);
		assert.ok(spot.left >= 0, `左へはみ出した: ${spot.left}`);
		assert.ok(spot.top >= 0, `上へはみ出した: ${spot.top}`);
		assert.ok(spot.left + size.width <= VIEW.width, '右へはみ出した');
		assert.ok(spot.top + size.height <= VIEW.height, '下へはみ出した');
	}
});

test('実物の台本(excel_comment.json)が検査を通る', () => {
	const script = JSON.parse(readFileSync(join(HERE, 'tours', 'excel_comment.json'), 'utf8'));
	assert.deepEqual(tour.validateScript(script), []);
	assert.ok(script.steps.length >= 3, '手順が少なすぎます');
	for (const step of script.steps) {
		assert.ok(Array.isArray(step.anchors) && step.anchors.length > 0, `目印が無い手順: ${step.text}`);
	}
});

test('壊れた台本は理由つきで弾く', () => {
	assert.deepEqual(tour.validateScript(null), ['台本がオブジェクトではありません']);
	assert.deepEqual(tour.validateScript({ id: 'a', title: 'b', steps: [] }), ['steps が1件もありません']);
	assert.deepEqual(tour.validateScript({ id: 'a', title: 'b', steps: [{ anchors: [] }] }), [
		'steps[0]: text がありません'
	]);
	assert.deepEqual(tour.validateScript({ id: 'a', title: 'b', steps: [{ text: 'x', anchors: '#a' }] }), [
		'steps[0]: anchors は配列で書きます'
	]);
	assert.deepEqual(tour.validateScript({ id: 'a', title: 'b', steps: [{ text: 'x', anchors: [''] }] }), [
		'steps[0]: anchors[0] が空です'
	]);
});
