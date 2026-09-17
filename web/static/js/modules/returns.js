const known = value => typeof value === 'number' && Number.isFinite(value);
const percent = value => known(value) ? `${(value * 100).toFixed(2)}%` : '不可用';
const money = value => known(value) ? `￥${value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}` : '不可用';

export class ReturnsTab {
    constructor(app) {
        this.app = app;
        this.requestId = 0;
        this.charts = [];
        document.getElementById('returns-apply').addEventListener('click', () => this.load());
        document.querySelectorAll('[data-return-period]').forEach(button => button.addEventListener('click', () => {
            this.period(button.dataset.returnPeriod);
            this.load();
        }));
        window.addEventListener('resize', () => this.charts.forEach(chart => chart.resize()));
        this.period('month');
    }

    period(period) {
        // Shanghai calendar date, independent of the browser's timezone.
        const now = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Shanghai' }));
        const end = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
        if (period === 'week') now.setDate(now.getDate() - (now.getDay() + 6) % 7);
        else if (period === 'year') now.setMonth(0, 1);
        else now.setDate(1);
        const start = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
        document.getElementById('returns-start').value = start;
        document.getElementById('returns-end').value = end;
    }

    text(id, value) { this.app.setText(id, value); }

    async load() {
        const id = ++this.requestId;
        const query = new URLSearchParams();
        for (const field of ['start', 'end']) {
            const value = document.getElementById(`returns-${field}`).value;
            if (value) query.set(`${field}_date`, value);
        }
        this.clear('正在读取所选区间，旧结果已清除');
        try {
            const response = await fetch(`${this.app.apiBase}/research/returns?${query}`);
            if (!response.ok) throw new Error();
            const data = await response.json();
            if (id !== this.requestId) return;
            if (!data.success) throw new Error();
            if (!data.available) { this.clear('该区间没有有效日终快照，不能判定为零收益'); return; }
            this.render(data);
        } catch {
            if (id === this.requestId) this.clear('读取失败，请检查日期范围（最多367天、不得选择未来日期）后重试');
        }
    }

    clear(message) {
        for (const key of ['assets', 'change', 'rate', 'benchmark', 'relative', 'drawdown']) this.text(`returns-${key}`, '不可用');
        this.text('returns-status', message);
        this.text('returns-note', '未加载有效结果');
        document.getElementById('returns-rows').replaceChildren();
        this.charts.forEach(chart => chart.clear());
        this.text('returns-chart-state', message);
    }

    render(data) {
        const s = data.summary;
        this.text('returns-status', `实际快照区间 ${data.effective_start} 至 ${data.effective_end} · ${data.snapshots} 个有效快照 · ${data.invalid_snapshots} 个无效快照 · ${data.benchmark_points} 个同期基准点${data.reset_suspected ? ' · 初始资金变化，疑似账户重置，停止比较账户收益' : ''}`);
        this.text('returns-note', data.note);
        this.text('returns-assets', `${money(s.start_assets)} → ${money(s.end_assets)}`);
        this.text('returns-change', money(s.asset_change));
        this.text('returns-rate', percent(s.asset_return));
        this.text('returns-benchmark', percent(s.benchmark_return));
        this.text('returns-relative', known(s.relative_asset_change) ? `${(s.relative_asset_change * 100).toFixed(2)} 个百分点` : '不可用');
        this.text('returns-drawdown', percent(s.max_asset_drawdown));
        const body = document.getElementById('returns-rows');
        body.replaceChildren();
        for (const point of data.points) {
            const row = document.createElement('tr');
            for (const value of [point.date, money(point.total_assets), point.previous_date || '区间起点', money(point.change_since_previous), percent(point.change_rate_since_previous), percent(point.benchmark_return)]) {
                const cell = document.createElement('td');
                cell.textContent = value;
                row.append(cell);
            }
            body.append(row);
        }
        if (!window.echarts) { this.text('returns-chart-state', '图表组件不可用，数值请查看下方明细'); return; }
        if (!this.charts.length) this.charts = ['returns-chart', 'returns-daily-chart'].map(id => window.echarts.init(document.getElementById(id)));
        this.text('returns-chart-state', '按实际快照日期展示；折线缺值断开，首日不画盈亏柱。资产变化未作资金流调整。');
        const dates = data.points.map(point => point.date);
        const common = {
            tooltip: { trigger: 'axis', renderMode: 'richText' },
            grid: { left: 65, right: 20, top: 50, bottom: 55 },
            xAxis: { type: 'category', data: dates },
        };
        this.charts[0].setOption({ ...common,
            legend: { data: ['资产变化（未调整资金流）', '沪深300'] },
            yAxis: { type: 'value', axisLabel: { formatter: '{value}%' } },
            series: [
                { name: '资产变化（未调整资金流）', type: 'line', connectNulls: false, smooth: false, data: data.points.map(p => known(p.asset_return) ? +(p.asset_return * 100).toFixed(4) : null) },
                { name: '沪深300', type: 'line', connectNulls: false, smooth: false, data: data.points.map(p => known(p.benchmark_return) ? +(p.benchmark_return * 100).toFixed(4) : null) },
            ],
        }, true);
        this.charts[1].setOption({ ...common,
            yAxis: { type: 'value', name: '元' },
            series: [{ name: '较前一快照资产变化', type: 'bar', data: data.points.map(p => ({ value: p.change_since_previous, itemStyle: { color: p.change_since_previous > 0 ? '#c66b3d' : '#7a8f6f' } })) }],
        }, true);
        this.charts.forEach(chart => chart.resize());
    }
}
