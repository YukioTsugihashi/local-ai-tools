/*
 * Open WebUI の画面に重ねる操作案内(ツアー)の実行本体。
 *
 * 設計の前提:
 *  - 外部リソースを一切読まない(オフライン運用のため)
 *  - 入口は画面の隅の「?」ボタン1つだけ。押すまで案内は始まらない
 *    (URLの #tour=<id> でも始められる)
 *  - 覆いも輪も pointer-events: none。案内中も画面は普通に操作できる。
 *    自動クリックはしない(誤操作の取り返しがつかないため見せるだけにする)
 *  - 目印(anchors)が見つからないときは黙って壊れず、理由を出して終わる
 *
 * 台本は配備時に window.__WEBUI_TOUR_SCRIPTS__ へ埋め込まれる
 * (正本は webui_tour/tours/*.json。tools/webui_tour.py が束ねる)。
 */
(function () {
	'use strict';

	var NS = 'mp-tour';
	var PAD = 8; // 輪と対象のすきま(px)
	var GAP = 12; // 吹き出しと対象のすきま(px)
	var BUBBLE = { width: 320, height: 150 }; // 配置計算用のおおよその大きさ
	var WAIT_MS = 8000; // 目印が現れるまで待つ上限
	var POLL_MS = 150;
	var TRACK_MS = 250; // 画面変化に追従する間隔

	// ---------------------------------------------------------------
	// 純粋な計算(DOMを触らない)。node のテストはここを直接呼ぶ
	// ---------------------------------------------------------------

	/** URLから開始するツアーIDを取り出す。無ければ null(＝何もしない) */
	function parseTourId(loc) {
		if (!loc) return null;
		var found = /[#?&]tour=([A-Za-z0-9_-]+)/.exec(String(loc.hash || '') + String(loc.search || ''));
		return found ? found[1] : null;
	}

	/**
	 * 候補セレクタを順に試して最初に見つかった要素を返す。
	 * 不正なセレクタが1つ混じっても、そこで案内を止めない。
	 */
	function resolveAnchor(doc, selectors) {
		if (!doc || !selectors || !selectors.length) return null;
		for (var i = 0; i < selectors.length; i++) {
			var el = null;
			try {
				el = doc.querySelector(selectors[i]);
			} catch (error) {
				continue; // セレクタが壊れていても次の候補へ進む
			}
			if (el && isVisible(el)) return el;
		}
		return null;
	}

	/** 存在しても畳まれている(幅も高さも0)要素は見つからなかった扱いにする */
	function isVisible(el) {
		if (!el || typeof el.getBoundingClientRect !== 'function') return true;
		var rect = el.getBoundingClientRect();
		return rect.width > 0 || rect.height > 0;
	}

	/** 対象を少し広げた穴の位置。画面外へはみ出す分は詰める */
	function spotlightBox(rect, viewport, padding) {
		var pad = typeof padding === 'number' ? padding : PAD;
		var left = Math.max(0, rect.left - pad);
		var top = Math.max(0, rect.top - pad);
		var right = Math.min(viewport.width, rect.left + rect.width + pad);
		var bottom = Math.min(viewport.height, rect.top + rect.height + pad);
		return {
			left: left,
			top: top,
			width: Math.max(0, right - left),
			height: Math.max(0, bottom - top)
		};
	}

	/**
	 * 吹き出しの位置。下→上→右→左の順に入る場所を探し、
	 * どこにも入らなければ画面中央へ置く(画面外に出して見失わせない)。
	 */
	function bubblePlacement(rect, viewport, size, gap) {
		var box = size || BUBBLE;
		var space = typeof gap === 'number' ? gap : GAP;
		var below = rect.top + rect.height + space;
		var above = rect.top - space - box.height;
		var rightOf = rect.left + rect.width + space;
		var leftOf = rect.left - space - box.width;
		var clampX = function (x) {
			return Math.min(Math.max(space, x), Math.max(space, viewport.width - box.width - space));
		};
		var clampY = function (y) {
			return Math.min(Math.max(space, y), Math.max(space, viewport.height - box.height - space));
		};
		var centeredX = clampX(rect.left + rect.width / 2 - box.width / 2);
		var centeredY = clampY(rect.top + rect.height / 2 - box.height / 2);

		if (below + box.height <= viewport.height) {
			return { left: centeredX, top: below, placement: 'below' };
		}
		if (above >= 0) {
			return { left: centeredX, top: above, placement: 'above' };
		}
		if (rightOf + box.width <= viewport.width) {
			return { left: rightOf, top: centeredY, placement: 'right' };
		}
		if (leftOf >= 0) {
			return { left: leftOf, top: centeredY, placement: 'left' };
		}
		return {
			left: clampX(viewport.width / 2 - box.width / 2),
			top: clampY(viewport.height / 2 - box.height / 2),
			placement: 'center'
		};
	}

	/**
	 * 台本の検査。配備前に tools/webui_tour.py からも同じ観点で見るが、
	 * 手で書き換えられた台本が流れ込む場合に備えて実行時にも確かめる。
	 */
	function validateScript(script) {
		var errors = [];
		if (!script || typeof script !== 'object') return ['台本がオブジェクトではありません'];
		if (!script.id) errors.push('id がありません');
		if (!script.title) errors.push('title がありません');
		var steps = script.steps;
		if (!Array.isArray(steps) || steps.length === 0) {
			errors.push('steps が1件もありません');
			return errors;
		}
		if (script.models !== undefined && !Array.isArray(script.models)) {
			errors.push('models は配列で書きます');
		}
		for (var i = 0; i < steps.length; i++) {
			var step = steps[i] || {};
			var where = 'steps[' + i + ']';
			if (!step.text) errors.push(where + ': text がありません');
			if (step.anchors !== undefined && !Array.isArray(step.anchors)) {
				errors.push(where + ': anchors は配列で書きます');
			}
			if (Array.isArray(step.anchors)) {
				for (var j = 0; j < step.anchors.length; j++) {
					if (typeof step.anchors[j] !== 'string' || !step.anchors[j]) {
						errors.push(where + ': anchors[' + j + '] が空です');
					}
				}
			}
		}
		return errors;
	}

	var api = {
		parseTourId: parseTourId,
		resolveAnchor: resolveAnchor,
		spotlightBox: spotlightBox,
		bubblePlacement: bubblePlacement,
		validateScript: validateScript,
		isVisible: isVisible
	};

	// DOMが無い環境(nodeのテスト)ではここまで。画面には触らない
	if (typeof document === 'undefined' || typeof window === 'undefined') {
		if (typeof globalThis !== 'undefined') globalThis.__WEBUI_TOUR__ = api;
		return;
	}

	// ---------------------------------------------------------------
	// 画面まわり
	// ---------------------------------------------------------------

	var STYLE = [
		'.' + NS + '-root{position:fixed;inset:0;z-index:2147483000;pointer-events:none;}',
		'.' + NS + '-ring{position:fixed;border-radius:10px;pointer-events:none;',
		'box-shadow:0 0 0 9999px rgba(15,23,42,.62);outline:3px solid #38bdf8;',
		'transition:all .18s ease-out;}',
		'.' + NS + '-bubble{position:fixed;width:320px;max-width:calc(100vw - 24px);',
		'pointer-events:auto;background:#fff;color:#0f172a;border-radius:12px;',
		'box-shadow:0 12px 32px rgba(0,0,0,.35);padding:14px 16px;',
		'font-family:system-ui,"Yu Gothic UI","Meiryo",sans-serif;font-size:14px;line-height:1.7;}',
		'.' + NS + '-title{font-weight:700;margin:0 0 6px;font-size:13px;color:#0284c7;}',
		'.' + NS + '-text{margin:0 0 12px;white-space:pre-wrap;}',
		'.' + NS + '-foot{display:flex;align-items:center;gap:8px;}',
		'.' + NS + '-count{margin-right:auto;font-size:12px;color:#64748b;}',
		'.' + NS + '-btn{border:0;border-radius:8px;padding:6px 14px;font-size:13px;cursor:pointer;',
		'font-family:inherit;background:#e2e8f0;color:#0f172a;}',
		'.' + NS + '-btn-main{background:#0284c7;color:#fff;}',
		'.' + NS + '-btn:disabled{opacity:.4;cursor:default;}',
		'.' + NS + '-launch{font-family:system-ui,"Yu Gothic UI","Meiryo",sans-serif;}',
		// 画面上部の並びへ差し込む形。送信ボタンと重ならない
		'.' + NS + '-launch-inline{position:relative;display:inline-flex;align-items:center;',
		'vertical-align:middle;margin-right:2px;}',
		// 差し込み先が見つからなかったときの保険(右上へ固定)
		'.' + NS + '-launch-fixed{position:fixed;top:10px;right:150px;z-index:2147482000;}',
		'.' + NS + '-launch-btn{width:30px;height:30px;border-radius:50%;border:0;cursor:pointer;',
		'font-size:15px;font-weight:700;background:#0284c7;color:#fff;line-height:1;',
		'display:flex;align-items:center;justify-content:center;padding:0;}',
		'.' + NS + '-launch-menu{position:absolute;top:calc(100% + 8px);right:0;z-index:2147482000;',
		'background:#fff;color:#0f172a;border-radius:12px;padding:8px;',
		'box-shadow:0 12px 32px rgba(0,0,0,.3);min-width:240px;max-width:320px;}',
		'.' + NS + '-launch-head{margin:4px 8px 8px;font-size:12px;font-weight:700;color:#64748b;}',
		'.' + NS + '-launch-item{display:block;width:100%;text-align:left;border:0;background:none;',
		'cursor:pointer;padding:8px;border-radius:8px;font-family:inherit;color:inherit;}',
		'.' + NS + '-launch-item:hover{background:#e0f2fe;}',
		'.' + NS + '-launch-name{display:block;font-size:14px;font-weight:600;}',
		'.' + NS + '-launch-desc{display:block;font-size:12px;color:#64748b;margin-top:2px;}',
		'@media (prefers-color-scheme:dark){',
		'.' + NS + '-bubble{background:#1e293b;color:#e2e8f0;}',
		'.' + NS + '-title{color:#38bdf8;}',
		'.' + NS + '-count{color:#94a3b8;}',
		'.' + NS + '-btn{background:#334155;color:#e2e8f0;}',
		'.' + NS + '-launch-menu{background:#1e293b;color:#e2e8f0;}',
		'.' + NS + '-launch-item:hover{background:#334155;}',
		'.' + NS + '-launch-desc{color:#94a3b8;}',
		'.' + NS + '-btn-main{background:#0ea5e9;color:#04202f;}}'
	].join('');

	var state = null;

	function viewport() {
		return { width: window.innerWidth, height: window.innerHeight };
	}

	function el(tag, cls, text) {
		var node = document.createElement(tag);
		if (cls) node.className = cls;
		if (text) node.textContent = text;
		return node;
	}

	function ensureStyle() {
		if (document.getElementById(NS + '-style')) return;
		var style = el('style');
		style.id = NS + '-style';
		style.textContent = STYLE;
		document.head.appendChild(style);
	}

	function mount() {
		ensureStyle();
		var root = el('div', NS + '-root');
		var ring = el('div', NS + '-ring');
		var bubble = el('div', NS + '-bubble');
		var title = el('p', NS + '-title');
		var text = el('p', NS + '-text');
		var foot = el('div', NS + '-foot');
		var count = el('span', NS + '-count');
		var back = el('button', NS + '-btn', '戻る');
		var next = el('button', NS + '-btn ' + NS + '-btn-main', '次へ');
		var quit = el('button', NS + '-btn', 'やめる');
		foot.appendChild(count);
		foot.appendChild(quit);
		foot.appendChild(back);
		foot.appendChild(next);
		bubble.appendChild(title);
		bubble.appendChild(text);
		bubble.appendChild(foot);
		root.appendChild(ring);
		root.appendChild(bubble);
		document.body.appendChild(root);
		return { root: root, ring: ring, bubble: bubble, title: title, text: text, count: count, back: back, next: next, quit: quit };
	}

	/** 目印が現れるまで待つ。時間切れは null を返す(例外にしない) */
	function waitForAnchor(selectors, done) {
		var deadline = Date.now() + (window.__WEBUI_TOUR_WAIT_MS__ || WAIT_MS); // 待ち時間はテストから縮められる
		var timer = null;
		var look = function () {
			var found = resolveAnchor(document, selectors);
			if (found || Date.now() > deadline) {
				if (timer) clearInterval(timer);
				if (state) state.waitTimer = null;
				done(found);
				return;
			}
		};
		var first = resolveAnchor(document, selectors);
		if (first) {
			done(first);
			return;
		}
		timer = setInterval(look, POLL_MS);
		if (state) state.waitTimer = timer;
	}

	function place(anchor) {
		if (!state) return;
		var view = viewport();
		var rect = anchor
			? anchor.getBoundingClientRect()
			: { left: view.width / 2, top: view.height / 2, width: 0, height: 0 };
		var box = spotlightBox(rect, view);
		var ring = state.ui.ring.style;
		ring.left = box.left + 'px';
		ring.top = box.top + 'px';
		ring.width = box.width + 'px';
		ring.height = box.height + 'px';
		var size = { width: state.ui.bubble.offsetWidth || BUBBLE.width, height: state.ui.bubble.offsetHeight || BUBBLE.height };
		var spot = bubblePlacement(box, view, size);
		state.ui.bubble.style.left = spot.left + 'px';
		state.ui.bubble.style.top = spot.top + 'px';
	}

	function clearStepListeners() {
		if (!state || !state.anchorClick) return;
		try {
			state.anchorClick.target.removeEventListener('click', state.anchorClick.handler, true);
		} catch (error) {
			/* 取り外しに失敗しても案内は続ける */
		}
		state.anchorClick = null;
	}

	function show(index) {
		if (!state) return;
		clearStepListeners();
		if (state.waitTimer) {
			clearInterval(state.waitTimer);
			state.waitTimer = null;
		}
		var steps = state.script.steps;
		if (index < 0 || index >= steps.length) {
			stop();
			return;
		}
		state.index = index;
		var step = steps[index];
		state.ui.title.textContent = state.script.title;
		state.ui.text.textContent = step.text;
		state.ui.count.textContent = index + 1 + ' / ' + steps.length;
		state.ui.back.disabled = index === 0;
		state.ui.next.textContent = index === steps.length - 1 ? '終わる' : '次へ';

		var selectors = Array.isArray(step.anchors) ? step.anchors : [];
		if (!selectors.length) {
			state.anchor = null;
			place(null);
			return;
		}
		waitForAnchor(selectors, function (anchor) {
			if (!state || state.index !== index) return;
			if (!anchor) {
				abort('画面の「' + (step.label || step.text.slice(0, 12)) + '」が見つかりませんでした。');
				return;
			}
			state.anchor = anchor;
			place(anchor);
			// 対象を実際に押したら次へ進む。押させるだけで、こちらからは押さない
			var handler = function () {
				if (state && state.index === index) setTimeout(function () { show(index + 1); }, 250);
			};
			try {
				anchor.addEventListener('click', handler, true);
				state.anchorClick = { target: anchor, handler: handler };
			} catch (error) {
				/* 監視できなくても「次へ」で進める */
			}
		});
	}

	function abort(reason) {
		var message = reason + '\n画面が変わった可能性があります。手順は「使い方ヘルプ」で確認できます。';
		stop();
		try {
			window.alert(message);
		} catch (error) {
			/* alertが塞がれていても落とさない */
		}
	}

	function track() {
		if (!state) return;
		place(state.anchor);
	}

	function onKey(event) {
		if (event.key === 'Escape') stop();
	}

	var launcher = null;
	var launcherTimer = null;
	// 今その一覧に出している台本の並び。変わったときだけ作り直す
	var launcherShown = '';

	// 画面上部の並び。この要素の「左」へ入れる。
	// 見つからなければ右上へ固定する(位置がずれても押せなくはならない)
	var LAUNCH_NEIGHBOURS = [
		'#chat-context-menu-button',
		'#chat-share-button',
		'#chat-artifacts-button'
	];

	// 画面上部のモデル名。台本の models と突き合わせ、関係ない画面には出さない
	var MODEL_LABELS = ['#model-selector-0-button', '[id^="model-selector-"][id$="-button"]'];

	/** 台本として成立しているものだけを一覧に出す */
	function availableTours() {
		var scripts = window.__WEBUI_TOUR_SCRIPTS__ || {};
		return Object.keys(scripts)
			.filter(function (id) {
				return validateScript(scripts[id]).length === 0;
			})
			.sort();
	}

	/**
	 * 名前を突き合わせる形にそろえる。
	 *
	 * モデル名はアイコンや装飾と一緒に別々の要素へ入るため、textContent が
	 * 「Excel 分析」のように空白混じりで取れる。素の一致では外れる
	 * (2026-08-18に「全モデルに出る/すぐ消える」として実際に起きた)。
	 */
	function normaliseName(value) {
		return String(value || '').replace(/\s+/g, '').toLowerCase();
	}

	/**
	 * 今どのモデルの画面かを表す文字列。画面上部の表示名とURLの models= を繋ぐ。
	 * どちらも取れなければ空文字(=判別できない)。
	 */
	function currentModelName() {
		var node = resolveAnchor(document, MODEL_LABELS);
		var label = node ? String(node.textContent || '').trim() : '';
		var found = /[?&]models=([^&#]+)/.exec(
			String(window.location.search || '') + String(window.location.hash || '')
		);
		var fromUrl = '';
		if (found) {
			try {
				fromUrl = decodeURIComponent(found[1]);
			} catch (error) {
				fromUrl = found[1];
			}
		}
		return (label + ' ' + fromUrl).trim();
	}

	/**
	 * 今の画面に関係する台本だけ返す。
	 *
	 * 台本の models に書いたモデルの画面でだけ一覧に出す。関係ない
	 * モデルにまで「?」が乗って見えるのを避けるため(2026-08-18に指摘)。
	 * models を書いていない台本はどの画面でも出す。
	 */
	function toursForCurrentModel() {
		var scripts = window.__WEBUI_TOUR_SCRIPTS__ || {};
		var model = currentModelName();
		return availableTours().filter(function (id) {
			var wanted = scripts[id].models;
			if (!Array.isArray(wanted) || !wanted.length) return true;
			// モデル名が取れないときは隠さない。隠す側に倒すと、画面の作りが
			// 少し変わっただけで「?」が出なくなり、原因も分からなくなる
			if (!model) return true;
			var seen = normaliseName(model);
			return wanted.some(function (name) {
				return name && seen.indexOf(normaliseName(name)) >= 0;
			});
		});
	}

	/**
	 * 本当に画面へ載っているか。
	 *
	 * parentNode の有無では判定できない。Open WebUIが画面上部ごと
	 * 描き直すと、切り離された側の親が残るため「付いている」ように見え、
	 * 入れ直しが走らずボタンが消えたままになる(出たり出なかったりの原因)。
	 */
	function isInDocument(node) {
		var current = node;
		while (current) {
			if (current === document.body) return true;
			current = current.parentNode;
		}
		return false;
	}

	function toggleMenu(open) {
		if (!launcher) return;
		launcher.menu.style.display = open ? 'block' : 'none';
		launcher.open = open;
	}

	/** 入れ物だけ作る。中身(台本の一覧)は fillMenu が入れ替える */
	function buildLauncher() {
		ensureStyle();
		var root = el('div', NS + '-launch');
		var menu = el('div', NS + '-launch-menu');
		menu.style.display = 'none';
		var button = el('button', NS + '-launch-btn', '？');
		button.setAttribute('aria-label', '使い方の案内');
		button.title = '使い方の案内';
		button.addEventListener('click', function () {
			toggleMenu(!launcher.open);
		});
		root.appendChild(menu);
		root.appendChild(button);
		document.addEventListener('keydown', function (event) {
			if (event.key === 'Escape') toggleMenu(false);
		}, true);
		return { root: root, button: button, menu: menu, open: false };
	}

	function fillMenu(ids) {
		var menu = launcher.menu;
		while (menu.children && menu.children.length) {
			menu.removeChild(menu.children[menu.children.length - 1]);
		}
		menu.appendChild(el('p', NS + '-launch-head', '使い方の案内'));
		ids.forEach(function (id) {
			var script = window.__WEBUI_TOUR_SCRIPTS__[id];
			var item = el('button', NS + '-launch-item');
			item.appendChild(el('span', NS + '-launch-name', script.title));
			if (script.description) item.appendChild(el('span', NS + '-launch-desc', script.description));
			item.addEventListener('click', function () {
				toggleMenu(false);
				start(id);
			});
			menu.appendChild(item);
		});
	}

	/**
	 * 呼び出し口を画面へ入れる。三点メニューの左へ並べる。
	 * 下の隅へ置くと送信ボタンと重なって邪魔になるため(2026-08-18に指摘)。
	 */
	function attachLauncher() {
		if (!launcher) return;
		var neighbour = resolveAnchor(document, LAUNCH_NEIGHBOURS);
		// 切り離された並びへ入れても画面には出ない。今も画面上にあるものだけ使う
		var usable = neighbour && neighbour.parentNode && isInDocument(neighbour);
		var inline = String(launcher.root.className).indexOf(NS + '-launch-inline') >= 0;
		if (isInDocument(launcher.root) && (inline || !usable)) return;
		// 右上へ逃がしたあとで並びが現れたら、そちらへ移す
		detachLauncher();
		if (usable) {
			launcher.root.className = NS + '-launch ' + NS + '-launch-inline';
			neighbour.parentNode.insertBefore(launcher.root, neighbour);
			return;
		}
		launcher.root.className = NS + '-launch ' + NS + '-launch-fixed';
		document.body.appendChild(launcher.root);
	}

	function detachLauncher() {
		if (launcher && launcher.root.parentNode) {
			launcher.root.parentNode.removeChild(launcher.root);
		}
	}

	/**
	 * 画面の状態に呼び出し口を合わせる。読み込み時と、以後2秒ごとに走る。
	 *
	 * モデルの切替も画面の描き直しも、こちらへ通知は来ない。
	 * 定期的に見て合わせるのがいちばん壊れにくい。
	 */
	function syncLauncher() {
		if (String(window.location.pathname || '').indexOf('/auth') === 0) return;
		if (state) return; // 案内中は触らない(輪や吹き出しと重なるため)
		var ids = toursForCurrentModel();
		if (!ids.length) {
			if (launcher) {
				toggleMenu(false);
				detachLauncher();
				launcherShown = '';
			}
			return;
		}
		if (!launcher) launcher = buildLauncher();
		var key = ids.join(',');
		if (key !== launcherShown) {
			fillMenu(ids);
			toggleMenu(false);
			launcherShown = key;
		}
		attachLauncher();
	}

	/**
	 * 何を見て出す/出さないを決めたのかを画面に出す。
	 *
	 * 「出ない」「全部に出る」の原因が、モデル名の読み取りなのか台本なのか、
	 * 開発者ツールを開かずに切り分けられるようにするため。
	 * URLに #tour=debug を付けたときだけ出る。
	 */
	function showDiagnostics() {
		ensureStyle();
		var box = el('div', NS + '-launch ' + NS + '-launch-fixed');
		var panel = el('div', NS + '-launch-menu');
		panel.style.display = 'block';
		panel.appendChild(el('p', NS + '-launch-head', '操作案内の状態'));
		var scripts = window.__WEBUI_TOUR_SCRIPTS__ || {};
		var rows = [
			['読み取ったモデル名', currentModelName() || '(読めません)'],
			['照合用に整えた形', normaliseName(currentModelName()) || '(空)'],
			['台本', Object.keys(scripts).join('、') || '(なし)'],
			['今の画面に出す台本', toursForCurrentModel().join('、') || '(なし)'],
			['版', String(window.__WEBUI_TOUR_VERSION__ || '不明')]
		];
		rows.forEach(function (row) {
			var line = el('div', NS + '-launch-item');
			line.appendChild(el('span', NS + '-launch-name', row[0]));
			line.appendChild(el('span', NS + '-launch-desc', row[1]));
			panel.appendChild(line);
		});
		box.appendChild(panel);
		document.body.appendChild(box);
	}

	function mountLauncher() {
		if (launcherTimer) return;
		syncLauncher();
		launcherTimer = setInterval(syncLauncher, 2000);
		// ブラウザには無いAPI。nodeのテストがこの見張りで終わらなくなるのを防ぐ
		if (launcherTimer && launcherTimer.unref) launcherTimer.unref();
	}

	/** 案内中は呼び出し口を隠す(輪や吹き出しと重なるため) */
	function showLauncher(visible) {
		if (launcher) launcher.root.style.display = visible ? '' : 'none';
	}

	function start(id) {
		var scripts = window.__WEBUI_TOUR_SCRIPTS__ || {};
		var script = scripts[id];
		if (!script) return false;
		if (validateScript(script).length) return false;
		stop();
		state = { script: script, index: 0, ui: mount(), anchor: null, waitTimer: null, anchorClick: null };
		state.ui.next.addEventListener('click', function () { show(state.index + 1); });
		state.ui.back.addEventListener('click', function () { show(state.index - 1); });
		state.ui.quit.addEventListener('click', function () { stop(); });
		state.trackTimer = setInterval(track, TRACK_MS);
		window.addEventListener('resize', track, true);
		window.addEventListener('scroll', track, true);
		document.addEventListener('keydown', onKey, true);
		showLauncher(false);
		show(0);
		return true;
	}

	function stop() {
		if (!state) return;
		clearStepListeners();
		if (state.waitTimer) clearInterval(state.waitTimer);
		if (state.trackTimer) clearInterval(state.trackTimer);
		window.removeEventListener('resize', track, true);
		window.removeEventListener('scroll', track, true);
		document.removeEventListener('keydown', onKey, true);
		if (state.ui.root.parentNode) state.ui.root.parentNode.removeChild(state.ui.root);
		state = null;
		showLauncher(true);
		syncLauncher(); // 案内中に画面が変わっていても、終わった時点で付け直す
	}

	api.start = start;
	api.stop = stop;
	api.availableTours = availableTours;
	// 画面に合わせ直す。通常は2秒ごとに自動で走る(テストからも呼ぶ)
	api.sync = syncLauncher;
	api.isRunning = function () { return state !== null; };
	window.__WEBUI_TOUR__ = api;

	// URLに指定があるときだけ動く。無ければ何も起きない。
	// __WEBUI_TOUR_WANTED__ は loader.js が読み込みの最初に控えたid。
	// Open WebUI(SvelteKit)は起動時にURLを書き換えることがあり、
	// このファイルが読まれる頃には #tour= が消えている場合があるため。
	function autoStart() {
		var id = parseTourId(window.location) || window.__WEBUI_TOUR_WANTED__;
		if (id === 'debug') {
			showDiagnostics();
			return;
		}
		mountLauncher();
		if (id) start(id);
	}
	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', autoStart);
	} else {
		autoStart();
	}
	window.addEventListener('hashchange', function () {
		var id = parseTourId(window.location);
		if (id) start(id);
	});
})();
