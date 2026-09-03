/*
 * tour.js の描画部分を node で動かすための、最低限の偽DOM。
 *
 * ブラウザを置けないので「画面に何も出ない」を再現する手段が無かった。
 * tour.js が実際に使う分だけを実装して、覆い・輪・吹き出しが本当に
 * 組み立てられるかを確かめられるようにする(判定だけのテストでは
 * mount/show/place が一度も動かず、そこが抜けていた)。
 */

class StubNode {
	constructor(tag) {
		this.tagName = String(tag || '').toUpperCase();
		this.children = [];
		this.parentNode = null;
		this.style = {};
		this.className = '';
		this.id = '';
		this.textContent = '';
		this.disabled = false;
		this.listeners = {};
		this.attributes = {};
		this.rect = { left: 0, top: 0, width: 100, height: 30 };
		this.offsetWidth = 320;
		this.offsetHeight = 150;
	}

	appendChild(child) {
		child.parentNode = this;
		this.children.push(child);
		return child;
	}

	insertBefore(child, reference) {
		const at = this.children.indexOf(reference);
		child.parentNode = this;
		this.children.splice(at < 0 ? this.children.length : at, 0, child);
		return child;
	}

	removeChild(child) {
		const at = this.children.indexOf(child);
		if (at >= 0) this.children.splice(at, 1);
		child.parentNode = null;
		return child;
	}

	setAttribute(name, value) {
		this.attributes[name] = String(value);
	}

	getAttribute(name) {
		return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
	}

	addEventListener(type, handler) {
		(this.listeners[type] = this.listeners[type] || []).push(handler);
	}

	removeEventListener(type, handler) {
		const list = this.listeners[type] || [];
		const at = list.indexOf(handler);
		if (at >= 0) list.splice(at, 1);
	}

	/** テストから「押した」を起こす */
	click() {
		for (const handler of (this.listeners.click || []).slice()) handler({ type: 'click' });
	}

	getBoundingClientRect() {
		return this.rect;
	}

	/** 自分以下を平らに並べる(テストの確認用) */
	all() {
		return this.children.reduce((found, child) => found.concat(child.all()), [this]);
	}

	get text() {
		return this.all()
			.map((node) => node.textContent)
			.join('\n');
	}
}

/** tour.js が触る範囲だけの window/document を作る */
export function createStubDom(options = {}) {
	const anchors = options.anchors || {};
	const head = new StubNode('head');
	const body = new StubNode('body');
	const alerts = [];

	const document = {
		readyState: options.readyState || 'complete',
		head,
		body,
		listeners: {},
		createElement: (tag) => new StubNode(tag),
		getElementById: (id) => head.all().concat(body.all()).find((node) => node.id === id) || null,
		querySelector: (selector) => {
			if (typeof anchors[selector] === 'undefined') return null;
			const value = anchors[selector];
			if (value === null) return null;
			if (value instanceof Error) throw value;
			return value;
		},
		addEventListener: (type, handler) => {
			(document.listeners[type] = document.listeners[type] || []).push(handler);
		},
		removeEventListener: (type, handler) => {
			const list = document.listeners[type] || [];
			const at = list.indexOf(handler);
			if (at >= 0) list.splice(at, 1);
		}
	};

	const window = {
		innerWidth: options.width || 1200,
		innerHeight: options.height || 800,
		location: options.location || { hash: '', search: '' },
		listeners: {},
		alert: (message) => alerts.push(message),
		addEventListener: (type, handler) => {
			(window.listeners[type] = window.listeners[type] || []).push(handler);
		},
		removeEventListener: (type, handler) => {
			const list = window.listeners[type] || [];
			const at = list.indexOf(handler);
			if (at >= 0) list.splice(at, 1);
		}
	};

	return { window, document, alerts, anchor: (width, height) => makeAnchor(width, height) };
}

export function makeAnchor(left = 100, top = 100, width = 120, height = 32) {
	const node = new StubNode('button');
	node.rect = { left, top, width, height };
	return node;
}

export { StubNode };
