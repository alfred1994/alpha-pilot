export class EvolutionTab {
    constructor(app) {
        this.app = app;
        this.lessons = [];
        this.category = 'all';
        this.filtersReady = false;
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
                <div><dt>LLM 评估池</dt><dd>Top ${this.escape(params.top_k ?? '-')}</dd></div>
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
