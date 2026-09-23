const VARIANT_LABELS = {
    baseline: '正式策略',
    loose_top: '放宽门槛',
    strict_top: '收紧门槛',
    ml_heavy: 'ML 加权',
    ml_none: '去 ML',
    ml_only: '纯 ML',
};

const VARIANT_DESC = {
    baseline: '参数基准',
    loose_top: 'top_k +1 · 最低分 -3',
    strict_top: 'top_k -1 · 最低分 +3',
    ml_heavy: 'ML 权重 ×2',
    ml_none: 'ML 权重 ×0',
    ml_only: '仅按 ML 分排序',
};

export class EvolutionTab {
    constructor(app) {
        this.app = app;
        this.lessons = [];
        this.category = 'all';
        this.filtersReady = false;
        this.shadowLoaded = false;
    }

    text(value, fallback = '-') {
        return value === null || value === undefined || value === '' ? fallback : String(value);
    }

    escape(value) {
        return this.text(value, '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    async load() {
        try {
            const response = await fetch(`${this.app.apiBase}/lessons?limit=60`);
            const data = await response.json();
            this.lessons = data.success ? (data.lessons || []) : [];
        } catch (error) {
            console.error('Failed to load lessons:', error);
            this.lessons = [];
        }
        this.renderStrategies();
        this.renderLessons();
        this.initFilters();
        this.loadShadow();
    }

    known(value) {
        return typeof value === 'number' && Number.isFinite(value);
    }

    pct(value, digits = 2) {
        return this.known(value) ? `${(value * 100).toFixed(digits)}%` : '未记录';
    }

    // 影子绩效一天最多更新一次（T+5 回填后），只在首次进入本页时拉取，
    // 不跟随 15 秒轮询重复请求。
    async loadShadow() {
        if (this.shadowLoaded) return;
        const container = document.getElementById('shadow-board');
        if (!container) return;
        try {
            const response = await fetch(`${this.app.apiBase}/shadow/leaderboard`);
            const data = await response.json();
            if (!data.success) throw new Error('unavailable');
            this.shadowLoaded = true;
            this.renderShadow(data);
        } catch (error) {
            console.error('Failed to load shadow leaderboard:', error);
            container.innerHTML = '<div class="empty-state">影子指标读取失败，下一次进入本页时重试</div>';
            this.setText('shadow-status', '读取失败');
        }
    }

    renderShadow(data) {
        const container = document.getElementById('shadow-board');
        const rows = data.leaderboard || [];
        const candidates = new Set(data.promotion_candidates || []);
        if (!rows.length || rows.every(row => !row.metrics?.trading_days)) {
            container.innerHTML = '<div class="empty-state">尚未记录影子策略决策，等待影子交易循环积累样本</div>';
            this.setText('shadow-status', '暂无数据');
            return;
        }
        this.setText('shadow-status', `${rows.length} 个变体并行对比`);
        const maxAbs = Math.max(
            ...rows.map(row => Math.abs(row.metrics?.avg_net_5d || 0)),
            0.0001,
        );
        const head = `
            <div class="shadow-head">
                <span>策略变体</span><span>T+5 平均净收益</span><span>相对正式</span><span>胜率</span><span>样本（交易日 · 已回填买入）</span>
            </div>`;
        container.innerHTML = head + rows.map(row => {
            const id = row.variant_id;
            const m = row.metrics || {};
            const isBaseline = id === 'baseline';
            const value = m.avg_net_5d;
            const barWidth = this.known(value) ? Math.max(Math.abs(value) / maxAbs * 100, 3) : 0;
            const delta = row.avg_net_5d_delta;
            const deltaText = isBaseline ? '基准'
                : this.known(delta) ? `${delta > 0 ? '+' : ''}${(delta * 100).toFixed(2)}pp` : '未记录';
            const status = candidates.has(id)
                ? '<span class="shadow-badge candidate">晋级候选</span>'
                : row.mature
                    ? '<span class="shadow-badge mature">样本成熟</span>'
                    : '<span class="shadow-badge">样本积累中</span>';
            return `
                <div class="shadow-row${isBaseline ? ' baseline' : ''}">
                    <div class="shadow-id">
                        <div><strong>${this.escape(VARIANT_LABELS[id] || id)}</strong><small>${this.escape(VARIANT_DESC[id] || id)}</small></div>
                        ${status}
                    </div>
                    <div class="shadow-return">
                        <div class="shadow-bar-track"><div class="shadow-bar${this.known(value) && value > 0 ? ' pos' : ''}" style="width:${barWidth}%"></div></div>
                        <strong>${this.pct(value)}</strong>
                    </div>
                    <div class="shadow-cell"><span>相对正式策略</span><strong>${this.escape(deltaText)}</strong></div>
                    <div class="shadow-cell"><span>买入胜率</span><strong>${this.pct(m.win_rate, 0)}</strong></div>
                    <div class="shadow-cell"><span>样本</span><strong>${Number(m.trading_days || 0)} 天 · ${Number(m.buys || 0)} 笔${m.pending_buys ? ` · ${m.pending_buys} 笔待回填` : ''}</strong></div>
                </div>`;
        }).join('');
    }

    strategyState() {
        const data = this.app.globalData || {};
        const strategy = data.daily_trader?.strategy || {};
        return {
            current: strategy.current || data.strategy_directive || null,
            pending: strategy.pending || data.pending_strategy_directive || null,
            diff: strategy.diff || [],
        };
    }

    renderStrategies() {
        const state = this.strategyState();
        this.renderStrategy('current', state.current);
        this.renderStrategy('pending', state.pending);
        this.renderDiff(state.diff);
        const evaluation = state.pending?.evaluation || state.current?.evaluation || {};
        const verdictLabels = { supported: '假设得到支持', refuted: '假设被事实否定', inconclusive: '证据不足' };
        const verdict = evaluation.verdict || 'inconclusive';
        this.setText('evaluation-verdict', verdictLabels[verdict] || '证据不足');
        const verdictEl = document.getElementById('evaluation-verdict');
        if (verdictEl) verdictEl.className = `evaluation-verdict ${this.escape(verdict)}`;
        this.setText('evaluation-evidence', evaluation.evidence || '下一次日终复盘会依据实际漏斗和执行事实进行评估。');
    }

    renderStrategy(prefix, item) {
        this.setText(`strategy-${prefix}-date`, item?.effective_date || '-');
        this.setText(`strategy-${prefix}-intent`, item?.intent || (prefix === 'current' ? '等待策略' : '尚未生成'));
        this.setText(`strategy-${prefix}-summary`, item?.summary || '-');
        const params = item?.params || {};
        const container = document.getElementById(`strategy-${prefix}-params`);
        if (container) {
            container.innerHTML = item ? `
                <div><dt>每轮买入上限</dt><dd>${this.escape(params.top_k ?? '-')} 笔</dd></div>
                <div><dt>候选最低分</dt><dd>${this.escape(params.min_score ?? '-')}</dd></div>
                <div><dt>单票仓位上限</dt><dd>${params.max_weight !== undefined ? `${(Number(params.max_weight) * 100).toFixed(0)}%` : '-'}</dd></div>
            ` : '';
        }
        if (prefix === 'current') this.setText('strategy-current-hypothesis', item?.hypothesis || '-');
        if (prefix === 'pending') this.setText('strategy-pending-rationale', item?.rationale || '-');
    }

    renderDiff(changes) {
        const container = document.getElementById('strategy-page-diff');
        if (!container) return;
        if (!changes.length) {
            container.innerHTML = '<div class="empty-state">当前与下一策略没有参数变化</div>';
            return;
        }
        const format = (key, value) => key === 'max_weight' && value !== null && value !== undefined
            ? `${(Number(value) * 100).toFixed(0)}%` : this.text(value);
        container.innerHTML = changes.map(item => `
            <div class="strategy-diff"><span>${this.escape(item.label)}</span><span>${this.escape(format(item.key, item.before))}</span><b>→ ${this.escape(format(item.key, item.after))}</b></div>
        `).join('');
    }

    initFilters() {
        if (this.filtersReady) return;
        const container = document.querySelector('.lessons-filter');
        if (!container) return;
        container.addEventListener('click', event => {
            const button = event.target.closest('[data-category]');
            if (!button) return;
            container.querySelectorAll('.filter-btn').forEach(item => item.classList.toggle('active', item === button));
            this.category = button.dataset.category || 'all';
            this.renderLessons();
        });
        this.filtersReady = true;
    }

    renderLessons() {
        const container = document.getElementById('lessons-list');
        if (!container) return;
        const rows = this.category === 'all' ? this.lessons : this.lessons.filter(item => item.category === this.category);
        if (!rows.length) {
            container.innerHTML = '<div class="empty-state">当前筛选下没有复盘教训</div>';
            return;
        }
        const categoryLabels = { entry: '入场', execution: '执行', risk: '风控', general: '综合', buy: '买入', sell: '卖出', regime: '市场环境', position: '仓位' };
        container.innerHTML = rows.map(item => `
            <article class="lesson-item">
                <div class="lesson-head"><span>${this.escape(categoryLabels[item.category] || item.category)}</span><span>${this.escape(item.date)}</span></div>
                <p>${this.escape(item.content)}</p>
            </article>
        `).join('');
    }

    setText(id, value) {
        const element = document.getElementById(id);
        if (element) element.textContent = value;
    }
}
