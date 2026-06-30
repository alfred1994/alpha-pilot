export class EvolutionTab {
    constructor(app) {
        this.app = app;
        this.weightsChart = null;
        this.allLessons = [];
        this.currentCategory = 'all';
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

    async load() {
        await this.loadLessonsData();
        this.renderLessons();
        await this.renderWeightsChart();
        await this.renderAdaptiveParams();
        this.initFilters();
    }

    initFilters() {
        const filterContainer = document.querySelector('.lessons-filter');
        if (!filterContainer || filterContainer.dataset.initialized) return;
        
        filterContainer.dataset.initialized = 'true';
        filterContainer.querySelectorAll('button').forEach(btn => {
            btn.addEventListener('click', (e) => {
                filterContainer.querySelectorAll('button').forEach(b => b.classList.remove('active-filter'));
                e.currentTarget.classList.add('active-filter');
                this.currentCategory = e.currentTarget.getAttribute('data-category');
                this.renderLessons();
            });
        });
    }

    async loadLessonsData() {
        try {
            const res = await fetch(`${this.app.apiBase}/lessons?limit=40`);
            if (res.ok) {
                const data = await res.json();
                if (data.success && data.lessons) {
                    this.allLessons = data.lessons;
                } else {
                    this.allLessons = [];
                }
            }
        } catch (e) {
            console.error("Failed to load lessons data:", e);
            this.allLessons = [];
        }
    }

    renderLessons() {
        const container = document.getElementById('lessons-list');
        container.innerHTML = '';

        const filtered = this.currentCategory === 'all' 
            ? this.allLessons 
            : this.allLessons.filter(l => l.category === this.currentCategory);

        if (filtered.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无符合条件的教训记录</div>';
            return;
        }

        filtered.forEach((l, idx) => {
            const div = document.createElement('div');
            div.className = 'lesson-item';
            
            const categoryLabel = {
                'buy': '买入教训', 'sell': '卖出教训', 'risk': '风控提醒', 'regime': '市场环境'
            }[l.category] || l.category;
            
            // Related trades parsing
            let tradesHtml = '';
            if (l.related_trades) {
                try {
                    let trades = [];
                    if (typeof l.related_trades === 'string') {
                        trades = JSON.parse(l.related_trades);
                    } else if (Array.isArray(l.related_trades)) {
                        trades = l.related_trades;
                    }
                    if (Array.isArray(trades) && trades.length > 0) {
                        tradesHtml = `<div class="mt-2" style="display:flex; gap:4px; flex-wrap:wrap;">` + 
                            trades.map(t => `<span class="badge" style="font-size:10px; background:rgba(91,121,226,0.1); color:var(--primary-color); border:1px solid rgba(91,121,226,0.2);">${this.escape(t)}</span>`).join('') + 
                            `</div>`;
                    }
                } catch (e) {
                    console.error("Error parsing related trades:", e);
                }
            }

            // Text truncate logic
            const content = this.text(l.content, '');
            const isLong = content.length > 100;
            const shortContent = isLong ? content.substring(0, 100) + '...' : content;
            
            div.innerHTML = `
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                    <span style="font-weight:600; font-size:14px; color:#f7ca5e;">💡 ${this.escape(categoryLabel)}</span>
                    <span style="font-size:11px; color:var(--text-muted);">${this.escape(l.date)}</span>
                </div>
                <div class="lesson-content" style="font-size:13px; color:var(--text-secondary); line-height:1.5; cursor: ${isLong ? 'pointer' : 'default'};">
                    <span class="text-body">${this.escape(shortContent)}</span>
                    ${isLong ? `<a class="expand-btn" style="color:var(--primary-color); font-size:12px; margin-left:4px; text-decoration:none;">展开</a>` : ''}
                </div>
                ${tradesHtml}
            `;

            if (isLong) {
                const contentDiv = div.querySelector('.lesson-content');
                const textBody = contentDiv.querySelector('.text-body');
                const expandBtn = contentDiv.querySelector('.expand-btn');
                let isExpanded = false;
                
                contentDiv.addEventListener('click', (e) => {
                    isExpanded = !isExpanded;
                    if (isExpanded) {
                        textBody.textContent = content;
                        expandBtn.textContent = '收起';
                    } else {
                        textBody.textContent = shortContent;
                        expandBtn.textContent = '展开';
                    }
                    e.stopPropagation();
                });
            }

            container.appendChild(div);
        });
    }

    async renderWeightsChart() {
        const data = this.app.globalData;
        if (!data || !data.adaptive) return;

        const chartDom = document.getElementById('weights-chart');
        if (!this.weightsChart) {
            this.weightsChart = echarts.init(chartDom);
        }

        const weights = data.adaptive.weights || {
            'technical': 0.40,
            'capital': 0.10,
            'sentiment': 0.25,
            'emotion': 0.15,
            'fundamental': 0.10
        };

        const categories = ['技术面', '资金面', '舆情面', '情绪面', '基本面'];
        const values = [
            (weights.technical * 100).toFixed(0),
            (weights.capital * 100).toFixed(0),
            (weights.sentiment * 100).toFixed(0),
            (weights.emotion * 100).toFixed(0),
            (weights.fundamental * 100).toFixed(0)
        ];

        const option = {
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'axis',
                backgroundColor: 'rgba(25, 30, 45, 0.9)',
                borderWidth: 0,
                textStyle: { color: '#f0f3f8' }
            },
            grid: {
                left: '3%', right: '4%', bottom: '3%', containLabel: true
            },
            xAxis: {
                type: 'category',
                data: categories,
                axisLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } },
                axisLabel: { color: '#8a96a8' }
            },
            yAxis: {
                type: 'value',
                axisLabel: { formatter: '{value}%', color: '#8a96a8' },
                splitLine: { lineStyle: { color: 'rgba(255,255,255,0.04)' } }
            },
            series: [{
                name: '自适应权重',
                type: 'bar',
                data: values,
                itemStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: '#5b79e2' },
                        { offset: 1, color: '#5ce2a4' }
                    ]),
                    borderRadius: [8, 8, 0, 0]
                },
                barWidth: '35%'
            }]
        };

        this.weightsChart.setOption(option);
        window.addEventListener('resize', () => this.weightsChart.resize());
    }

    async renderAdaptiveParams() {
        const data = this.app.globalData;
        if (!data || !data.adaptive) return;

        const a = data.adaptive;
        
        document.getElementById('param-buy-threshold').textContent = a.buy_threshold ?? '-';
        document.getElementById('param-min-score').textContent = a.min_score ?? '-';
        document.getElementById('param-top-k-delta').textContent = a.top_k_delta ?? '-';
        document.getElementById('param-position-scale').textContent = a.position_scale ?? '-';
        
        const lastUpdateText = a.last_update ? `上次参数进化时间: ${a.last_update}` : '上次参数进化时间: -';
        document.getElementById('param-last-update').textContent = lastUpdateText;
        
        const regimeLabel = {
            'bull': '🟢 牛市环境',
            'bear': '🔴 熊市环境',
            'sideways': '⚪ 震荡环境',
            'rebound': '🔵 超跌反弹'
        }[a.regime] || `⚪ ${a.regime || '震荡'}环境`;
        document.getElementById('param-regime').textContent = `当前自适应环境识别: ${regimeLabel}`;

        const latestEvoCard = document.getElementById('latest-evolution-card');
        if (latestEvoCard && a.latest_adjustment) {
            const adj = a.latest_adjustment;
            document.getElementById('evo-date').textContent = adj.date || '-';
            const winRate = adj.overall?.win_rate !== undefined ? `${(adj.overall.win_rate * 100).toFixed(1)}%` : '-';
            const pf = adj.overall?.profit_factor !== undefined ? adj.overall.profit_factor.toFixed(2) : '-';
            const trades = adj.overall?.sell_trades ?? '-';
            
            document.getElementById('evo-win-rate').textContent = winRate;
            document.getElementById('evo-profit-factor').textContent = pf;
            document.getElementById('evo-trades').textContent = `${trades}笔平仓`;
            
            const list = document.getElementById('evo-actions');
            list.innerHTML = '';
            if (adj.adjustments && adj.adjustments.length > 0) {
                adj.adjustments.forEach(item => {
                    const li = document.createElement('li');
                    let oldNewText = '';
                    if (item.old !== undefined && item.new !== undefined) {
                        oldNewText = ` (${item.old} → ${item.new})`;
                    }
                    li.innerHTML = `<b>[${this.escape(item.type || '参数调整')}]</b> ${this.escape(item.reason || '')}${this.escape(oldNewText)}`;
                    list.appendChild(li);
                });
            } else {
                list.innerHTML = '<li>指标表现正常，本次未调整核心参数</li>';
            }
            latestEvoCard.style.display = 'block';
        } else if (latestEvoCard) {
            latestEvoCard.style.display = 'none';
        }
    }
}
