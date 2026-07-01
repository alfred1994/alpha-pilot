export class DecisionsTab {
    constructor(app) {
        this.app = app;
        this.radarChart = null;
    }

    text(value, fallback = '-') {
        if (value === null || value === undefined || value === '') return fallback;
        return String(value);
    }

    escape(value) {
        return this.text(value, '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    stockLabel(item) {
        const code = this.text(item?.code, '');
        const name = this.text(item?.name, '');
        if (name && name !== code) return `${name} ${code}`;
        return code || '-';
    }

    async load() {
        const res = await fetch(`${this.app.apiBase}/decisions?limit=10`);
        const data = await res.json();
        const container = document.getElementById('decisions-list');
        container.innerHTML = '';

        if (!data.success || !data.decisions || data.decisions.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无最新决策数据</div>';
            return;
        }

        this.renderStats(data.decisions);

        data.decisions.forEach(d => {
            const div = document.createElement('div');
            div.className = 'decision-item';
            
            const badgeClass = d.action === 'BUY' ? 'btn-success' : (d.action === 'SELL' ? 'btn-danger' : '');
            const badgeText = d.action === 'BUY' ? '买入 BUY' : (d.action === 'SELL' ? '卖出 SELL' : '持有 HOLD');
            const reasoning = this.text(d.reasoning, '');
            const shortReasoning = reasoning.substring(0, 180);
            const stockText = this.escape(this.stockLabel(d));
            
            div.innerHTML = `
                <div class="decision-item-header">
                    <div>
                        <span style="font-weight:700; font-size:16px;">${stockText}</span>
                        <span class="badge" style="margin-left:8px;">置信度: ${(d.confidence * 100).toFixed(0)}%</span>
                    </div>
                    <span class="badge ${badgeClass}">${badgeText}</span>
                </div>
                <div class="decision-item-body">
                    <p style="margin-bottom:8px;"><b>决策时间:</b> ${this.escape(d.date)}</p>
                    <p style="margin-bottom:8px;"><b>逻辑摘要:</b> ${this.escape(shortReasoning)}${reasoning.length > 180 ? '...' : ''}</p>
                    <button class="decision-btn-detail" data-id="${d.id}">查看决策详情</button>
                </div>
            `;
            container.appendChild(div);
        });

        container.querySelectorAll('.decision-btn-detail').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const id = parseInt(e.currentTarget.getAttribute('data-id'));
                const decision = data.decisions.find(item => item.id === id);
                if (decision) {
                    this.showDetailModal(decision);
                }
            });
        });

        document.getElementById('modal-close-btn').addEventListener('click', () => {
            document.getElementById('decision-modal').classList.remove('active');
        });
    }

    renderStats(decisions) {
        if (!decisions || !decisions.length) return;

        const total = decisions.length;
        const buys = decisions.filter(d => d.action === 'BUY').length;
        const sells = decisions.filter(d => d.action === 'SELL').length;
        const avgConf = decisions.reduce((s, d) => s + Number(d.confidence || 0), 0) / total;

        const totalEl = document.getElementById('dec-total-count');
        if (totalEl) totalEl.textContent = total;

        const ratioEl = document.getElementById('dec-buy-sell-ratio');
        if (ratioEl) ratioEl.textContent = `${buys} / ${sells}`;

        const confEl = document.getElementById('dec-avg-confidence');
        if (confEl) confEl.textContent = `${(avgConf * 100).toFixed(0)}%`;
    }

    showDetailModal(d) {
        document.getElementById('modal-title').textContent = `${this.stockLabel(d)} 决策推理详情`;
        document.getElementById('modal-confidence').textContent = `${(d.confidence * 100).toFixed(0)}%`;
        
        const actionEl = document.getElementById('modal-action');
        actionEl.textContent = d.action;
        actionEl.className = `value ${d.action === 'BUY' ? 'green-text' : (d.action === 'SELL' ? 'red-text' : '')}`;

        document.getElementById('modal-reasoning').textContent = this.text(d.reasoning, '暂无公开摘要');

        document.getElementById('decision-modal').classList.add('active');

        this.renderRadarChart(d.dimensions || {});
    }

    renderRadarChart(dims) {
        const chartDom = document.getElementById('radar-chart');
        if (!this.radarChart) {
            this.radarChart = echarts.init(chartDom);
        }

        const technical = dims.technical?.score || 50;
        const capital = dims.capital?.score || 50;
        const sentiment = dims.sentiment?.score || 50;
        const emotion = dims.emotion?.score || 50;
        const fundamental = dims.fundamental?.score || 50;

        const option = {
            backgroundColor: 'transparent',
            radar: {
                indicator: [
                    { name: '技术面', max: 100 },
                    { name: '资金面', max: 100 },
                    { name: '舆情面', max: 100 },
                    { name: '情绪面', max: 100 },
                    { name: '基本面', max: 100 }
                ],
                splitArea: { show: false },
                splitLine: { lineStyle: { color: 'rgba(255,255,255,0.04)' } },
                axisLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } },
                name: { textStyle: { color: '#8a96a8', fontSize: 11 } }
            },
            series: [{
                name: '5维信号',
                type: 'radar',
                data: [
                    {
                        value: [technical, capital, sentiment, emotion, fundamental],
                        name: '得分',
                        itemStyle: { color: '#5b79e2' },
                        areaStyle: { color: 'rgba(91, 121, 226, 0.25)' }
                    }
                ]
            }]
        };

        this.radarChart.setOption(option);
        setTimeout(() => this.radarChart.resize(), 100);
    }
}
