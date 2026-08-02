export class DecisionsTab {
    constructor(app) {
        this.app = app;
        this.decisions = [];
        this.kind = 'all';
        this.radarChart = null;
        this.filtersReady = false;
        this.modalReady = false;
    }

    text(value, fallback = '-') {
        return value === null || value === undefined || value === '' ? fallback : String(value);
    }

    escape(value) {
        return this.text(value, '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    stockLabel(item) {
        const name = this.text(item?.name, '');
        const code = this.text(item?.code, '');
        return name && name !== code ? `${name} ${code}` : code || '-';
    }

    async load() {
        try {
            const response = await fetch(`${this.app.apiBase}/decisions?limit=50`);
            const data = await response.json();
            this.decisions = data.success ? (data.decisions || []) : [];
        } catch (error) {
            console.error('Failed to load decisions:', error);
            this.decisions = [];
        }
        this.renderStats();
        this.renderList();
        this.initFilters();
        this.initModal();
        if (window.lucide) window.lucide.createIcons();
    }

    initFilters() {
        if (this.filtersReady) return;
        const filters = document.getElementById('decision-filters');
        if (!filters) return;
        filters.addEventListener('click', event => {
            const button = event.target.closest('[data-kind]');
            if (!button) return;
            filters.querySelectorAll('.filter-btn').forEach(item => item.classList.toggle('active', item === button));
            this.kind = button.dataset.kind || 'all';
            this.renderList();
        });
        this.filtersReady = true;
    }

    initModal() {
        if (this.modalReady) return;
        document.getElementById('modal-close-btn')?.addEventListener('click', () => this.closeModal());
        document.getElementById('decision-modal')?.addEventListener('click', event => {
            if (event.target.id === 'decision-modal') this.closeModal();
        });
        this.modalReady = true;
    }

    renderStats() {
        const signals = this.decisions.filter(item => item.decision_type === 'signal' || ['BUY', 'SELL'].includes(item.action));
        const observations = this.decisions.filter(item => item.action === 'HOLD');
        const buys = signals.filter(item => item.action === 'BUY').length;
        const sells = signals.filter(item => item.action === 'SELL').length;
        const confidence = this.decisions.length
            ? this.decisions.reduce((sum, item) => sum + Number(item.confidence || 0), 0) / this.decisions.length : 0;
        this.setText('dec-total-count', this.decisions.length);
        this.setText('dec-signal-count', signals.length);
        this.setText('dec-observation-count', observations.length);
        this.setText('dec-buy-sell-ratio', `BUY ${buys} · SELL ${sells}`);
        this.setText('dec-avg-confidence', this.decisions.length ? `${(confidence * 100).toFixed(0)}%` : '-');
    }

    renderList() {
        const container = document.getElementById('decisions-list');
        if (!container) return;
        const rows = this.kind === 'all' ? this.decisions : this.decisions.filter(item => {
            const kind = item.decision_type || (item.action === 'HOLD' ? 'observation' : 'signal');
            return kind === this.kind;
        });
        if (!rows.length) {
            container.innerHTML = '<div class="empty-state">当前筛选下没有判断记录</div>';
            return;
        }
        container.innerHTML = rows.map(item => {
            const kind = item.decision_type || (item.action === 'HOLD' ? 'observation' : 'signal');
            const kindLabel = kind === 'signal' ? `交易信号 · ${item.action}` : '观察结论 · HOLD';
            return `
                <article class="decision-item ${kind}" data-decision-id="${item.id}">
                    <div class="decision-head">
                        <div class="decision-title"><strong>${this.escape(this.stockLabel(item))}</strong><small>${this.escape(item.date)} · 置信度 ${(Number(item.confidence || 0) * 100).toFixed(0)}%</small></div>
                        <span class="decision-kind">${this.escape(kindLabel)}</span>
                    </div>
                    <p class="decision-reason">${this.escape(item.reasoning || '暂无公开判断依据')}</p>
                    <button class="decision-detail-btn" data-detail-id="${item.id}">查看判断详情</button>
                </article>`;
        }).join('');
        container.querySelectorAll('[data-detail-id]').forEach(button => button.addEventListener('click', () => {
            const id = Number(button.dataset.detailId);
            const decision = this.decisions.find(item => item.id === id);
            if (decision) this.showDetail(decision);
        }));
    }

    showDetail(item) {
        const kind = item.decision_type || (item.action === 'HOLD' ? 'observation' : 'signal');
        this.setText('modal-decision-type', kind === 'signal' ? '交易信号' : '观察结论');
        this.setText('modal-title', this.stockLabel(item));
        this.setText('modal-action', item.action);
        this.setText('modal-confidence', `${(Number(item.confidence || 0) * 100).toFixed(0)}%`);
        this.setText('modal-reasoning', item.reasoning || '暂无公开判断依据');
        const modal = document.getElementById('decision-modal');
        modal?.classList.add('active');
        modal?.setAttribute('aria-hidden', 'false');
        this.renderRadar(item.dimensions || {});
    }

    closeModal() {
        const modal = document.getElementById('decision-modal');
        modal?.classList.remove('active');
        modal?.setAttribute('aria-hidden', 'true');
    }

    renderRadar(dimensions) {
        const dom = document.getElementById('radar-chart');
        if (!dom || !window.echarts) return;
        if (!this.radarChart) this.radarChart = window.echarts.init(dom);
        const keys = ['technical', 'capital', 'sentiment', 'emotion', 'fundamental', 'ml'];
        const labels = ['技术面', '资金面', '舆情面', '情绪面', '基本面', '机器学习'];
        const available = keys.map((key, index) => ({ key, label: labels[index] })).filter(item => dimensions[item.key]);
        const rows = available.length ? available : keys.slice(0, 5).map((key, index) => ({ key, label: labels[index] }));
        this.radarChart.setOption({
            radar: { indicator: rows.map(item => ({ name: item.label, max: 100 })), splitArea: { areaStyle: { color: ['rgba(139,157,131,.04)', 'rgba(139,157,131,.12)'] } }, axisLine: { lineStyle: { color: 'rgba(52,66,52,.18)' } }, splitLine: { lineStyle: { color: 'rgba(52,66,52,.14)' } }, name: { color: '#6f725e', fontSize: 9 } },
            series: [{ type: 'radar', data: [{ value: rows.map(item => Number(dimensions[item.key]?.score || 0)), areaStyle: { color: 'rgba(198,107,61,.2)' }, lineStyle: { color: '#c66b3d', width: 2 }, itemStyle: { color: '#c66b3d' } }] }],
        });
        setTimeout(() => this.radarChart?.resize(), 50);
    }

    setText(id, value) {
        const element = document.getElementById(id);
        if (element) element.textContent = value;
    }
}
