/*
 * tour.js の「画面に出す」部分の実行時テスト。
 *
 * 判定だけのテスト(tour.test.mjs)では mount/show/place が一度も動かず、
 * 「何も表示されない」を再現できなかった。偽DOM(dom-stub.mjs)の上で
 * 実際に組み立てさせ、覆い・輪・吹き出しが出ること、片づくことを見る。
 */
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

import { createStubDom, makeAnchor, StubNode } from './dom-stub.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(join(HERE, 'tour.js'), 'utf8');
const REAL_SCRIPT = JSON.parse(readFileSync(join(HERE, 'tours', 'excel_comment.json'), 'utf8'));

const SIMPLE = {
	id: 'demo',
	title: '見本の案内',
	steps: [
		{ label: '入力欄', text: 'ここに入力します', anchors: ['#chat-input-container'] },
		{ label: '送信', text: 'これで送ります', anchors: ['#send-message-button'] }
	]
};

// 案内が動いたままだと追従用のsetIntervalがnodeを終わらせない。
// 開いた世界をここへ控えて、最後にまとめて閉じる
const opened = [];
after(() => {
	for (const dom of opened) {
		try {
			dom.window.__WEBUI_TOUR__.stop();
		} catch (error) {
			/* 閉じられなくてもテストの結果は変えない */
		}
	}
});

/** 偽DOMの上で tour.js を動かして、その世界ごと返す */
function boot({ scripts = { demo: SIMPLE }, anchors = {}, hash = '', waitMs = 30, wanted = null, pathname = '/', model = null, navbar = false } = {}) {
	let bar = null;
	let dots = null;
	if (navbar) {
		dots = makeAnchor(1100, 12, 28, 28);
		bar = new StubNode('div');
		bar.appendChild(dots);
		anchors['#chat-context-menu-button'] = dots;
	}
	if (model) {
		const label = makeAnchor(10, 10, 120, 24);
		label.textContent = model;
		anchors['#model-selector-0-button'] = label;
	}
	const dom = createStubDom({ anchors, location: { hash, search: '', pathname } });
	if (bar) dom.document.body.appendChild(bar);
	dom.window.__WEBUI_TOUR_SCRIPTS__ = scripts;
	dom.window.__WEBUI_TOUR_WAIT_MS__ = waitMs;
	if (wanted) dom.window.__WEBUI_TOUR_WANTED__ = wanted; // loader.js が先に控えた分
	const sandbox = {
		window: dom.window,
		document: dom.document,
		setTimeout,
		clearTimeout,
		setInterval,
		clearInterval,
		Date,
		console
	};
	vm.createContext(sandbox);
	vm.runInContext(SOURCE, sandbox, { filename: 'tour.js' });
	opened.push(dom);
	return { ...dom, tour: dom.window.__WEBUI_TOUR__, bar, dots };
}

const nodes = (dom) => dom.document.body.all();
const byClass = (dom, cls) => nodes(dom).filter((node) => String(node.className).split(' ').includes(cls));
const one = (dom, cls) => byClass(dom, cls)[0] || null;
const bubbleText = (dom) => (one(dom, 'mp-tour-text') || {}).textContent;
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

test('URLに指定が無ければ案内は始まらない(呼び出し口だけ置く)', () => {
	const dom = boot({ hash: '' });
	assert.equal(one(dom, 'mp-tour-root'), null, '頼まれていないのに覆いを出している');
	assert.equal(dom.tour.isRunning(), false);
});

test('知らない台本idを指定されても何も出さず落ちない', () => {
	const dom = boot({ hash: '#tour=存在しない' });
	assert.equal(one(dom, 'mp-tour-root'), null);
	assert.equal(dom.tour.isRunning(), false);
});

test('#tour= を付けると覆い・輪・吹き出しが出る', () => {
	const dom = boot({
		hash: '#tour=demo',
		anchors: { '#chat-input-container': makeAnchor(100, 200, 400, 40) }
	});
	assert.equal(dom.tour.isRunning(), true, '案内が始まっていない');
	assert.ok(one(dom, 'mp-tour-root'), '覆いが無い');
	assert.ok(one(dom, 'mp-tour-ring'), '輪が無い');
	assert.ok(one(dom, 'mp-tour-bubble'), '吹き出しが無い');
	assert.equal(bubbleText(dom), 'ここに入力します');
	assert.equal((one(dom, 'mp-tour-count') || {}).textContent, '1 / 2');
	assert.equal((one(dom, 'mp-tour-title') || {}).textContent, '見本の案内');
});

test('輪は対象の位置に合わせて置かれる', () => {
	const dom = boot({
		hash: '#tour=demo',
		anchors: { '#chat-input-container': makeAnchor(100, 200, 400, 40) }
	});
	const ring = one(dom, 'mp-tour-ring');
	// 対象(100,200,400x40)の周りへ8pxの余白
	assert.equal(ring.style.left, '92px');
	assert.equal(ring.style.top, '192px');
	assert.equal(ring.style.width, '416px');
	assert.equal(ring.style.height, '56px');
	const bubble = one(dom, 'mp-tour-bubble');
	assert.ok(bubble.style.left.endsWith('px') && bubble.style.top.endsWith('px'), '吹き出しが配置されていない');
});

test('「次へ」で進み、最後まで行くと片づく', () => {
	const dom = boot({
		hash: '#tour=demo',
		anchors: {
			'#chat-input-container': makeAnchor(100, 200, 400, 40),
			'#send-message-button': makeAnchor(520, 200, 36, 36)
		}
	});
	const next = one(dom, 'mp-tour-btn-main');
	assert.equal(next.textContent, '次へ');
	next.click();
	assert.equal(bubbleText(dom), 'これで送ります');
	assert.equal(one(dom, 'mp-tour-count').textContent, '2 / 2');
	assert.equal(one(dom, 'mp-tour-btn-main').textContent, '終わる', '最後の手順で表示が変わらない');

	one(dom, 'mp-tour-btn-main').click();
	assert.equal(dom.tour.isRunning(), false);
	assert.equal(one(dom, 'mp-tour-root'), null, '終わったのに覆いが残っている');
});

test('「やめる」で即座に片づく', () => {
	const dom = boot({
		hash: '#tour=demo',
		anchors: { '#chat-input-container': makeAnchor() }
	});
	const quit = byClass(dom, 'mp-tour-btn').find((node) => node.textContent === 'やめる');
	quit.click();
	assert.equal(dom.tour.isRunning(), false);
	assert.equal(one(dom, 'mp-tour-root'), null);
});

test('光っている場所を実際に押すと次へ進む', async () => {
	const anchor = makeAnchor(100, 200, 400, 40);
	const dom = boot({
		hash: '#tour=demo',
		anchors: { '#chat-input-container': anchor, '#send-message-button': makeAnchor(520, 200, 36, 36) }
	});
	anchor.click();
	await sleep(300); // 押した直後は画面が動くので少し待ってから進む作り
	assert.equal(bubbleText(dom), 'これで送ります');
});

test('目印が見つからなければ理由を出して中止し、覆いも残さない', async () => {
	const dom = boot({ hash: '#tour=demo', anchors: {}, waitMs: 30 });
	await sleep(300);
	assert.equal(dom.tour.isRunning(), false, '見つからないのに案内が残っている');
	assert.equal(one(dom, 'mp-tour-root'), null, '覆いが画面に残ると操作できなくなる');
	assert.equal(dom.alerts.length, 1, '理由を出していない');
	assert.match(dom.alerts[0], /見つかりませんでした/);
	assert.match(dom.alerts[0], /使い方ヘルプ/);
});

test('実物の台本(Excelコメント)を最後まで通せる', () => {
	const anchors = {};
	for (const step of REAL_SCRIPT.steps) anchors[step.anchors[0]] = makeAnchor(100, 100, 200, 40);
	const dom = boot({ scripts: { 'excel-comment': REAL_SCRIPT }, hash: '#tour=excel-comment', anchors });

	assert.equal(dom.tour.isRunning(), true);
	for (let i = 0; i < REAL_SCRIPT.steps.length; i++) {
		assert.equal(
			one(dom, 'mp-tour-count').textContent,
			i + 1 + ' / ' + REAL_SCRIPT.steps.length,
			'手順' + (i + 1) + 'で進めなくなった'
		);
		assert.equal(bubbleText(dom), REAL_SCRIPT.steps[i].text);
		one(dom, 'mp-tour-btn-main').click();
	}
	assert.equal(dom.tour.isRunning(), false, '最後まで進んでも終わらない');
	assert.equal(dom.alerts.length, 0, '途中で中止していた');
});

test('あとから #tour= を付けても始まる(画面を開いたままURLへ足す場合)', () => {
	const dom = boot({ hash: '', anchors: { '#chat-input-container': makeAnchor() } });
	assert.equal(dom.tour.isRunning(), false);
	dom.window.location.hash = '#tour=demo';
	for (const handler of dom.window.listeners.hashchange || []) handler({ type: 'hashchange' });
	assert.equal(dom.tour.isRunning(), true, 'hashchange で始まらない');
	assert.ok(one(dom, 'mp-tour-bubble'));
});

test('二度始めても覆いは1つだけ', () => {
	const dom = boot({ hash: '#tour=demo', anchors: { '#chat-input-container': makeAnchor() } });
	dom.tour.start('demo');
	assert.equal(byClass(dom, 'mp-tour-root').length, 1, '覆いが重なって残っている');
});

test('URLのハッシュが消されていても、loader.jsが控えたidで始まる', () => {
	// Open WebUI は起動時にURLを書き換えることがある。生成物が読まれる頃には
	// #tour= が消えていても、案内は始まらなければならない
	const dom = boot({ hash: '', wanted: 'demo', anchors: { '#chat-input-container': makeAnchor() } });
	assert.equal(dom.tour.isRunning(), true, '控えたidで始まらない(実画面で何も出ない原因になる)');
	assert.equal(bubbleText(dom), 'ここに入力します');
});

test('控えたidが無く、ハッシュも無ければ従来どおり何もしない', () => {
	const dom = boot({ hash: '', wanted: null });
	assert.equal(dom.tour.isRunning(), false);
	assert.equal(one(dom, 'mp-tour-root'), null);
});

// ---------------------------------------------------------------------------
// 常設の呼び出し口(画面の隅の「？」)
// ---------------------------------------------------------------------------

const launchBtn = (dom) => one(dom, 'mp-tour-launch-btn');
const launchMenu = (dom) => one(dom, 'mp-tour-launch-menu');
const launchItems = (dom) => byClass(dom, 'mp-tour-launch-item');

test('URLに指定が無くても呼び出し口だけは置かれる', () => {
	const dom = boot({ hash: '' });
	assert.ok(launchBtn(dom), '「？」ボタンが無い(利用者が自分で始められない)');
	assert.equal(launchBtn(dom).getAttribute('aria-label'), '使い方の案内');
	assert.equal(launchMenu(dom).style.display, 'none', '最初から一覧が開いている');
	assert.equal(dom.tour.isRunning(), false, 'ボタンを置いただけで案内が始まっている');
});

test('「？」を押すとツアーの一覧が開き、もう一度押すと閉じる', () => {
	const dom = boot({ hash: '' });
	launchBtn(dom).click();
	assert.equal(launchMenu(dom).style.display, 'block');
	assert.equal(launchItems(dom).length, 1);
	assert.equal(one(dom, 'mp-tour-launch-name').textContent, '見本の案内');
	launchBtn(dom).click();
	assert.equal(launchMenu(dom).style.display, 'none');
});

test('一覧から選ぶと案内が始まり、呼び出し口は隠れる', () => {
	const dom = boot({ hash: '', anchors: { '#chat-input-container': makeAnchor() } });
	launchBtn(dom).click();
	launchItems(dom)[0].click();
	assert.equal(dom.tour.isRunning(), true, '一覧から始められない');
	assert.equal(bubbleText(dom), 'ここに入力します');
	assert.equal(launchMenu(dom).style.display, 'none', '一覧が開いたまま');
	assert.equal(one(dom, 'mp-tour-launch').style.display, 'none', '案内中に呼び出し口が重なっている');
});

test('案内を終えると呼び出し口が戻る', () => {
	const dom = boot({ hash: '#tour=demo', anchors: { '#chat-input-container': makeAnchor() } });
	assert.equal(one(dom, 'mp-tour-launch').style.display, 'none');
	dom.tour.stop();
	assert.equal(one(dom, 'mp-tour-launch').style.display, '', '終わっても呼び出し口が戻らない');
});

test('実物の台本は説明つきで一覧に出る', () => {
	const dom = boot({ scripts: { 'excel-comment': REAL_SCRIPT }, hash: '', model: 'Excel分析' });
	launchBtn(dom).click();
	assert.equal(one(dom, 'mp-tour-launch-name').textContent, REAL_SCRIPT.title);
	assert.equal(one(dom, 'mp-tour-launch-desc').textContent, REAL_SCRIPT.description);
});

test('壊れた台本は一覧に出さない', () => {
	const dom = boot({ scripts: { broken: { id: 'broken', title: '壊れ', steps: [] } }, hash: '' });
	assert.equal(dom.tour.availableTours().length, 0);
	assert.equal(launchBtn(dom), null, '始められない案内を一覧に出している');
});

test('ログイン画面には呼び出し口を出さない', () => {
	const dom = boot({ hash: '', pathname: '/auth' });
	assert.equal(launchBtn(dom), null);
});

test('呼び出し口は三点メニューの左へ入る(送信ボタンと重ならない)', () => {
	const dom = boot({ hash: '', navbar: true });

	const root = dom.bar.children.find((node) =>
		String(node.className).split(' ').includes('mp-tour-launch')
	);
	assert.ok(root, '三点メニューと同じ並びに入っていない');
	assert.equal(dom.tour.isRunning(), false, 'ボタンを置いただけで案内が始まっている');
	assert.equal(dom.bar.children.indexOf(root), 0, '三点メニューより右にある');
	assert.equal(dom.bar.children.indexOf(dom.dots), 1);
	assert.ok(
		String(root.className).split(' ').includes('mp-tour-launch-inline'),
		'並びへ差し込む見た目になっていない'
	);
});

test('差し込み先が見つからなければ右上へ固定する', () => {
	const dom = boot({ hash: '', anchors: {} });
	const root = one(dom, 'mp-tour-launch');
	assert.ok(root, '呼び出し口が消えてしまっている');
	assert.ok(
		String(root.className).split(' ').includes('mp-tour-launch-fixed'),
		'保険の固定配置になっていない'
	);
	assert.equal(root.parentNode, dom.document.body);
});

// ---------------------------------------------------------------------------
// 関係のあるモデルの画面にだけ出す / 画面が描き直されても消えない
// ---------------------------------------------------------------------------

const EXCEL_TOUR = { ...SIMPLE, id: 'excel', title: 'Excelの案内', models: ['Excel分析'] };

test('台本が対象にしていないモデルの画面には出さない', () => {
	const dom = boot({ scripts: { excel: EXCEL_TOUR }, model: '文章処理' });
	assert.equal(launchBtn(dom), null, '関係ないモデルに「?」が乗っている');
});

test('対象のモデルの画面には出す', () => {
	const dom = boot({ scripts: { excel: EXCEL_TOUR }, model: 'Excel分析' });
	assert.ok(launchBtn(dom), '対象のモデルなのに出ていない');
	launchBtn(dom).click();
	assert.equal(one(dom, 'mp-tour-launch-name').textContent, 'Excelの案内');
});

test('models を書いていない台本はどの画面でも出す', () => {
	const dom = boot({ scripts: { demo: SIMPLE }, model: '文章処理' });
	assert.ok(launchBtn(dom), 'models 未指定なのに消えている');
});

test('モデル名が部分一致でも対象とみなす', () => {
	const dom = boot({ scripts: { excel: EXCEL_TOUR }, model: 'Excel分析 ▾' });
	assert.ok(launchBtn(dom), 'モデル名に飾りが付くと見失う');
});

test('画面上部ごと描き直されても呼び出し口を入れ直す', () => {
	const dom = boot({ hash: '', navbar: true });
	const root = dom.bar.children.find((node) =>
		String(node.className).split(' ').includes('mp-tour-launch')
	);
	assert.ok(root, '最初の差し込みに失敗している');

	// Open WebUI が上部を作り直した状況。切り離された側の親は残るので、
	// parentNode で判定すると「付いている」と誤認して入れ直せない
	dom.document.body.removeChild(dom.bar);
	dom.tour.sync();

	assert.ok(
		dom.document.body.all().some((node) =>
			String(node.className).split(' ').includes('mp-tour-launch')
		),
		'描き直しのあと「?」が消えたままになる'
	);
});

test('モデル名が取れないときは隠さない', () => {
	// 画面の作りが変わってモデル名を読めなくなっても、「?」が消えては困る。
	// 消える側に倒すと出ない原因も分からなくなる(2026-08-18に実際そうなった)
	const dom = boot({ scripts: { excel: EXCEL_TOUR }, model: null });
	assert.ok(launchBtn(dom), 'モデルを判別できないだけで消えている');
});

test('URLのmodels指定でも対象と判定する', () => {
	const dom = boot({ scripts: { excel: { ...EXCEL_TOUR, models: ['excel_analysis'] } } });
	dom.window.location.search = '?models=excel_analysis';
	dom.tour.sync();
	assert.ok(launchBtn(dom), 'URLのモデル指定を見ていない');
});

test('モデル名に空白が混じっても照合できる', () => {
	// アイコンや装飾で「Excel 分析」のように空白入りで取れることがある。
	// 素の一致では外れ、「全モデルに出る/すぐ消える」になる(2026-08-18に実発生)
	const dom = boot({ scripts: { excel: EXCEL_TOUR }, model: '  Excel 分析  ▾ ' });
	assert.ok(launchBtn(dom), '空白混じりの表示名で見失っている');
});

test('#tour=debug は診断だけ出して案内を始めない', () => {
	const dom = boot({ scripts: { excel: EXCEL_TOUR }, hash: '#tour=debug', model: '文章処理' });
	assert.equal(dom.tour.isRunning(), false);
	const text = dom.document.body.all().map((n) => n.textContent).join('|');
	assert.match(text, /操作案内の状態/);
	assert.match(text, /読み取ったモデル名/);
	assert.match(text, /文章処理/, '何を読んだかが分からないと切り分けられない');
});
