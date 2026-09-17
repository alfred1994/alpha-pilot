// Deterministic offline browser-contract tests, no npm dependencies.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

class Element {
    constructor() { this.children = []; this.textContent = ''; this.value = ''; this.listeners = {}; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    replaceChildren(...children) { this.children = children; }
    append(child) { this.children.push(child); }
}
const elements = new Map();
globalThis.document = {
    getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
    querySelectorAll() { return []; },
    createElement() { return new Element(); },
};
const charts = [];
globalThis.window = {
    addEventListener() {},
    echarts: { init() {
        const chart = { options: null, cleared: false, setOption(options) { this.options = options; }, clear() { this.cleared = true; }, resize() {} };
        charts.push(chart); return chart;
    } },
};
const source = await readFile(new URL('../web/static/js/modules/returns.js', import.meta.url), 'utf8');
const { ReturnsTab } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const tab = new ReturnsTab({ apiBase: '/api', setText(id, value) { document.getElementById(id).textContent = String(value); } });
const fixture = {
    success: true, available: true, effective_start: '2020-01-01', effective_end: '2020-01-03',
    snapshots: 3, invalid_snapshots: 0, benchmark_points: 2, reset_suspected: false,
    summary: { start_assets: 100, end_assets: 90, asset_change: -10, asset_return: -0.1,
        benchmark_return: 0, relative_asset_change: -0.1, max_asset_drawdown: -0.25 },
    points: [
        { date: '2020-01-01', total_assets: 100, previous_date: null, change_since_previous: null, change_rate_since_previous: null, asset_return: 0, benchmark_return: 0 },
        { date: '2020-01-02', total_assets: 120, previous_date: '2020-01-01', change_since_previous: 20, change_rate_since_previous: 0.2, asset_return: 0.2, benchmark_return: null },
        { date: '2020-01-03', total_assets: 90, previous_date: '2020-01-02', change_since_previous: -30, change_rate_since_previous: -0.25, asset_return: -0.1, benchmark_return: 0 },
    ],
    note: '未调整资金流',
};
globalThis.fetch = async () => ({ ok: true, json: async () => fixture });
await tab.load();
assert.equal(document.getElementById('returns-benchmark').textContent, '0.00%');
assert.equal(document.getElementById('returns-rate').textContent, '-10.00%');
assert.equal(document.getElementById('returns-rows').children.length, 3);
assert.deepEqual(charts[0].options.series[1].data, [0, null, 0]);
assert.equal(charts[0].options.series[1].connectNulls, false);
assert.equal(charts[1].options.series[0].data[0].value, null);
// In-flight older success cannot overwrite the newer empty response.
let release;
globalThis.fetch = () => new Promise(resolve => { release = resolve; });
const old = tab.load();
assert.equal(document.getElementById('returns-rate').textContent, '不可用');
assert.equal(document.getElementById('returns-rows').children.length, 0);
globalThis.fetch = async () => ({ ok: true, json: async () => ({ success: true, available: false }) });
await tab.load();
release({ ok: true, json: async () => fixture });
await old;
assert.equal(document.getElementById('returns-rate').textContent, '不可用');
assert.match(document.getElementById('returns-status').textContent, /没有有效日终快照/);
globalThis.fetch = async () => { throw new Error('offline'); };
await tab.load();
assert.match(document.getElementById('returns-status').textContent, /读取失败/);
assert.equal(document.getElementById('returns-rows').children.length, 0);
assert.ok(charts.every(chart => chart.cleared));
console.log('PASS returns frontend: zero/missing, charts, table, stale requests, empty and failure states');
