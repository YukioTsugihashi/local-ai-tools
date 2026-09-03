/*
 * Excelアップロードガードの実行時テスト。
 *
 * このガードは .xlsx/.xlsm の添付に process=false を付け、Open WebUIの
 * 抽出・ベクトル化を止める(Excel分析はファイルの中身を直接読むため)。
 *
 * ただし**出席者名簿は例外**にしなければならない。議事録は名簿のテキストを
 * 使うので、抽出を止めると中身0文字で届き、名簿なしの議事録ができる。
 * 2026-08-18に実発生: 出席者名簿.xlsx の抽出が0字、roster_chars=0。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(join(HERE, '..', 'open_webui_excel_upload_guard.js'), 'utf8');

class FakeFile {
	constructor(name) {
		this.name = name;
	}
}

class FakeFormData {
	constructor(file) {
		this.file = file;
	}
	get(key) {
		return key === 'file' ? this.file : null;
	}
}

/** ガードを読み込み、包まれた fetch と「実際に呼ばれたURL」を返す */
function load() {
	const seen = [];
	const sandbox = {
		File: FakeFile,
		FormData: FakeFormData,
		Request: class {},
		URL,
		console,
		window: {
			location: { origin: 'http://localhost:3000' },
			fetch: (input) => {
				seen.push(String(input));
				return Promise.resolve('ok');
			}
		}
	};
	sandbox.window.window = sandbox.window;
	vm.createContext(sandbox);
	vm.runInContext(SOURCE, sandbox, { filename: 'upload-guard.js' });
	return { fetch: sandbox.window.fetch, seen, window: sandbox.window, sandbox };
}

function upload(guard, filename) {
	guard.fetch('http://localhost:3000/api/v1/files/', {
		method: 'POST',
		body: new FakeFormData(new FakeFile(filename))
	});
	return guard.seen[guard.seen.length - 1];
}

test('分析用のExcelは抽出を止める(埋め込みを無駄に走らせない)', () => {
	const guard = load();
	assert.match(upload(guard, 'R8第1四半期 科別分析.xlsx'), /process=false/);
	assert.match(upload(guard, '集計.xlsm'), /process=false/);
});

test('出席者名簿は抽出を止めない(議事録がテキストを使う)', () => {
	const guard = load();
	for (const name of ['出席者名簿.xlsx', '名簿.xlsx', '参加者一覧.xlsx', 'roster.xlsm']) {
		assert.doesNotMatch(upload(guard, name), /process=false/, name);
	}
});

test('Excel以外はそのまま通す', () => {
	const guard = load();
	assert.doesNotMatch(upload(guard, '文字起こし.docx'), /process=false/);
});

test('二重に読み込んでも包み直さない', () => {
	// index.html へ直接埋め込む形と /static から読む形が同時に生きうる
	const guard = load();
	const wrapped = guard.window.fetch;
	vm.runInContext(SOURCE, guard.sandbox, { filename: 'again.js' });
	assert.equal(guard.window.fetch, wrapped, 'fetch が包み直されている');
	assert.equal(guard.window.__MP_EXCEL_UPLOAD_GUARD__, true);
});
