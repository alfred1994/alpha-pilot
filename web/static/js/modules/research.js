const DIMENSIONS = { technical: '技术', capital: '资金', sentiment: '舆情', emotion: '情绪', fundamental: '基本面', ml: 'ML' };
const known = value => typeof value === 'number' && Number.isFinite(value);
const pct = value => known(value) ? `${(value * 100).toFixed(2)}%` : '未成熟 / 未回填';
const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
};

export class ResearchTab {
    constructor(app) {
        this.app = app;
        this.page = 1;
        this.requestId = 0;
        this.marketRequestId = 0;
        this.hasMore = false;
        this.bind();
    }

    bind() {
        ['research-start', 'research-end', 'research-layer'].forEach(id => {
            document.getElementById(id)?.addEventListener('change', () => { this.page = 1; this.load(); });
        });
        document.getElementById('research-prev')?.addEventListener('click', () => {
            if (this.page > 1) { this.page -= 1; this.load(); }
        });
        document.getElementById('research-next')?.addEventListener('click', () => {
            if (this.hasMore) { this.page += 1; this.load(); }
        });
        document.getElementById('research-retry')?.addEventListener('click', () => this.load());
        document.getElementById('open-research')?.addEventListener('click', () => this.app.switchTab('research'));
    }

    text(id, text) { this.app.setText(id, text); }

    async get(path) {
        const response = await fetch(`${this.app.apiBase}/research/${path}`);
        if (!response.ok) throw new Error('读取失败');
        const data = await response.json();
        if (!data.success) throw new Error('读取失败');
        return data;
    }

    async loadMarket() {
        const id = ++this.marketRequestId;
        try {
            const data = await this.get('market');
            if (id !== this.marketRequestId) return;
            const container = document.getElementById('market-evidence-metrics');
            container.replaceChildren();
            if (!data.available) {
                this.text('market-evidence-status', '暂无市场证据快照');
                this.text('market-evidence-note', '尚未记录，不能视为市场中性或零涨跌。');
                return;
            }
            const market = data.market;
            this.text('market-evidence-status', `${this.app.regimeLabel(market.regime)} · ${market.date} · ${market.stale ? '日期已过期' : '最近快照'}`);
            this.text('market-evidence-note', `${market.trend_stale ? '指数源已过期，相应指标不可用。' : ''}${market.note}`);
            for (const metric of market.metrics) {
                const box = element('div', undefined, 'research-metric');
                box.append(element('span', metric.label), element('strong', known(metric.value) ? `${metric.value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}${metric.unit}` : '未记录'));
                container.append(box);
            }
        } catch {
            if (id !== this.marketRequestId) return;
            document.getElementById('market-evidence-metrics').replaceChildren();
            this.text('market-evidence-status', '市场证据读取失败');
            this.text('market-evidence-note', '不能据此判定市场状态；下一次自动刷新将重试。');
        }
    }

    async load() {
        const id = ++this.requestId;
        this.hasMore = false;
        this.controls(true);
        this.text('research-page', `正在读取第 ${this.page} 页`);
        const query = new URLSearchParams({ page: this.page, limit: 20 });
        for (const [field, key] of [['research-start', 'start_date'], ['research-end', 'end_date'], ['research-layer', 'layer']]) {
            const value = document.getElementById(field)?.value;
            if (value) query.set(key, value);
        }
        try {
            const data = await this.get(`candidates?${query}`);
            if (id !== this.requestId) return;
            if (!data.available) { this.clear('尚无候选观察存储，覆盖情况未知'); return; }
            const s = data.summary;
            this.text('research-observations', s.observations);
            this.text('research-stocks', s.unique_stocks);
            this.text('research-evaluated', s.evaluated);
            this.text('research-matured', `${s.matured_5d} / ${s.observations}`);
            this.text('research-scope', `${data.start_date} 至 ${data.end_date} · ${s.scans} 轮扫描 · T+5 涉及 ${s.matured_stocks} 只独立股票 · 最近观察 ${s.latest_observation || '无'}`);
            this.text('research-note', data.note);
            this.text('research-page', `第 ${data.page} 页 · 共 ${s.observations} 条`);
            this.hasMore = data.has_more;
            this.renderGroups(data.groups);
            this.renderCandidates(data.candidates);
        } catch {
            if (id !== this.requestId) return;
            this.clear('读取失败，请检查日期范围（最多367天）后重试；不能视为零候选');
        } finally {
            if (id === this.requestId) this.controls(false);
        }
    }

    controls(loading) {
        document.getElementById('research-prev').disabled = loading || this.page <= 1;
        document.getElementById('research-next').disabled = loading || !this.hasMore;
    }

    clear(message) {
        ['research-observations', 'research-stocks', 'research-evaluated', 'research-matured'].forEach(id => this.text(id, '未知'));
        this.text('research-scope', message);
        this.text('research-page', '暂无可用分页');
        this.text('research-note', '未加载有效样本。');
        document.getElementById('research-groups').replaceChildren(element('p', message));
        document.getElementById('research-list').replaceChildren(element('p', message));
    }

    renderGroups(groups) {
        const root = document.getElementById('research-groups');
        root.replaceChildren();
        if (!groups.length) { root.append(element('p', '所选区间没有已保存的候选观察')); return; }
        for (const group of groups) {
            const card = element('div', undefined, 'research-group');
            card.append(element('strong', group.label),
                element('p', `${group.observations} 次观察 · ${group.unique_stocks} 只股票 · ${group.matured_5d} 个T+5样本`),
                element('small', group.matured_5d ? `T+5多头观察均值 ${pct(group.mean_net_5d)} · 正收益占比 ${pct(group.positive_rate_5d)}` : '尚无有效T+5数据，不计算收益占比'));
            root.append(card);
        }
    }

    renderCandidates(rows) {
        const root = document.getElementById('research-list');
        root.replaceChildren();
        if (!rows.length) { root.append(element('p', '本页没有候选记录')); return; }
        for (const row of rows) {
            const card = element('article', undefined, 'card research-candidate');
            const head = element('div', undefined, 'section-heading compact');
            head.append(element('h3', `${row.name || row.code} · ${row.code}`),
                element('span', `计划动作 ${row.action} · 模型 ${row.llm_action || '未记录'}`));
            card.append(head, element('p', `${row.observed_at} · 评分 ${known(row.score) ? row.score.toFixed(1) : '未知'} · ${row.denial_label}`),
                element('p', row.hold_reason || '未记录拒绝原因；计划不代表成交'),
                element('p', `T+3 多头观察 ${pct(row.net_return_3d)}　/　T+5 多头观察 ${pct(row.net_return_5d)}`));
            const detail = element('details');
            detail.append(element('summary', '查看当时证据与统计口径'));
            const grid = element('div', undefined, 'research-metrics');
            for (const [key, label] of Object.entries(DIMENSIONS)) {
                const item = row.dimensions[key];
                grid.append(element('div', `${label}：${known(item?.score) ? item.score.toFixed(1) : '未记录'} / 置信度 ${known(item?.confidence) ? pct(item.confidence) : '未记录'}`, 'research-metric'));
            }
            detail.append(grid, element('p', `扫描 ${row.scan_id || '未知'} · 策略版本 ${row.strategy_version || '未知'}`),
                element('p', `价格来源 ${row.price_source || '未知'} · 费用率 ${known(row.fee_rate) ? pct(row.fee_rate) : '未知'} · 单边滑点率 ${known(row.slippage_rate) ? pct(row.slippage_rate) : '未知'}`),
                element('p', `结果回填时间 ${row.evaluated_at || '尚未回填'}。缺失证据不以最新评分代替，观察收益不等于实际成交收益。`));
            card.append(detail);
            root.append(card);
        }
    }
}
