export class EvolutionTab {
    constructor(app) {
        this.app = app;
        this.weightsChart = null;
    }

    async load() {
        await this.renderLessons();
        await this.renderWeightsChart();
    }

    async renderLessons() {
        const res = await fetch(`${this.app.apiBase}/lessons?limit=10`);
        const data = await res.json();
        const container = document.getElementById('lessons-list');
        container.innerHTML = '';

        if (!data.success || !data.lessons || data.lessons.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无交易教训记录</div>';
            return;
        }

        data.lessons.forEach(l => {
            const div = document.createElement('div');
            div.className = 'lesson-item';
            
            const categoryLabel = {
                'buy': '买入教训', 'sell': '卖出教训', 'risk': '风控提醒', 'regime': '市场环境'
            }[l.category] || l.category;
            
            div.innerHTML = `
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                    <span style="font-weight:600; font-size:14px; color:#f7ca5e;">💡 ${categoryLabel}</span>
                    <span style="font-size:11px; color:var(--text-muted);">${l.date}</span>
                </div>
                <div style="font-size:13px; color:var(--text-secondary); line-height:1.5;">
                    ${l.content}
                </div>
            `;
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
}
