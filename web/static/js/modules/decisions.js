export class DecisionsTab {
    constructor(app) {
        this.app = app;
        this.decisions = [];
        this.kind = 'all';
        this.page = 1;
        this.requestId = 0;
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
        const requestId = ++this.requestId;
        const query = new URLSearchParams({ limit: '50', page: String(this.page), kind: this.kind });
        const date = document.getElementById('decision-date')?.value;
        if (date) { query.set('start_date', date); query.set('end_date', date); }
        try {
            const response = await fetch(`${this.app.apiBase}/decisions?${query}`);
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error('Unavailable');
            if (requestId !== this.requestId) return;
            this.decisions = data.decisions || [];
            this.setText('decision-page', `第 ${data.page} 页 · 共 ${data.total} 条 · 本页 ${new Set(this.decisions.map(d => d.code)).size} 只股票`);
            document.getElementById('decision-prev')?.toggleAttribute('disabled', this.page <= 1);
            document.getElementById('decision-next')?.toggleAttribute('disabled', !data.has_more);
        } catch (error) {
            if (requestId !== this.requestId) return;
            this.setText('decisions-list', '判断记录读取失败，请重试；不能据此认定没有信号。');
            ['dec-total-count', 'dec-signal-count', 'dec-observation-count', 'dec-avg-confidence'].forEach(id => this.setText(id, '不可用'));
            this.initFilters();
            return;
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
            this.page = 1;
            this.load();
        });
        document.getElementById('decision-date')?.addEventListener('change', () => { this.page = 1; this.load(); });
        document.getElementById('decision-prev')?.addEventListener('click', () => { this.page = Math.max(1, this.page - 1); this.load(); });
        document.getElementById('decision-next')?.addEventListener('click', () => { this.page += 1; this.load(); });
        document.getElementById('decision-retry')?.addEventListener('click', () => this.load());
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
                        <div class="decision-title"><strong>${this.escape(this.stockLabel(item))}</strong><small>${this.escape(item.created_at || item.date)} · 置信度 ${(Number(item.confidence || 0) * 100).toFixed(0)}%</small></div>
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
        this.setText('modal-reasoning', `${item.reasoning || '暂无公开判断依据'}\n时间：${item.created_at || item.date}；扫描：${item.scan_id || '未知（历史记录）'}；评分快照：${item.evidence_available ? '决策时点' : '未记录，不用最新数据代替'}`);
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
        if (!available.length) {
            // 无多维评分数据时显示占位，避免全 0 塌陷雷达
            this.radarChart.clear();
            this.radarChart.setOption({
                title: {
                    text: '该决策未包含多维评分数据',
                    left: 'center',
                    top: 'middle',
                    textStyle: { color: '#6f725e', fontSize: 12, fontWeight: 400 },
                },
                series: [],
            });
            setTimeout(() => this.radarChart?.resize(), 50);
            return;
        }
        const rows = available;
        this.radarChart.setOption({
            title: { text: '' },
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
