export class DashboardTab {
    constructor(app) {
        this.app = app;
        this.chart = null;
    }

    async load() {
        const data = this.app.globalData;
        if (!data) return;

        document.getElementById('total-assets').textContent = `￥${data.account.total_assets.toLocaleString('zh-CN', {minimumFractionDigits: 0, maximumFractionDigits: 0})}`;
        document.getElementById('available-cash').textContent = `￥${data.account.cash.toLocaleString('zh-CN', {minimumFractionDigits: 0, maximumFractionDigits: 0})}`;
        
        const pnlRes = await fetch(`${this.app.apiBase}/performance?days=2`);
        const pnlData = await pnlRes.json();
        
        let dailyPnl = 0.0;
        let cumulativePnlPct = 0.0;
        if (pnlData.success && pnlData.performance && pnlData.performance.length > 0) {
            const latest = pnlData.performance[pnlData.performance.length - 1];
            dailyPnl = latest.daily_pnl;
            cumulativePnlPct = latest.cumulative_pnl_pct;
        }
        
        const totalPnlVal = data.account.total_assets - 1000000.0;
        
        const totalPnlBox = document.getElementById('total-pnl');
        totalPnlBox.textContent = `${totalPnlVal >= 0 ? '+' : ''}${totalPnlVal.toLocaleString('zh-CN', {maximumFractionDigits:0})} (${(cumulativePnlPct * 100).toFixed(2)}%)`;
        totalPnlBox.className = `stat-val ${totalPnlVal >= 0 ? 'green-text' : 'red-text'}`;

        const dailyPnlBox = document.getElementById('daily-pnl');
        dailyPnlBox.textContent = `${dailyPnl >= 0 ? '+' : ''}${dailyPnl.toLocaleString('zh-CN', {maximumFractionDigits:0})}`;
        dailyPnlBox.className = `stat-val ${dailyPnl >= 0 ? 'green-text' : 'red-text'}`;

        const posContainer = document.getElementById('positions-list');
        posContainer.innerHTML = '';
        if (data.account.positions && data.account.positions.length > 0) {
            data.account.positions.forEach(code => {
                const item = document.createElement('div');
                item.className = 'position-item';
                item.innerHTML = `
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <span style="font-weight:600;">${code}</span>
                        <span class="badge" style="background: rgba(91,121,226,0.15); color:#5b79e2;">持仓中</span>
                    </div>
                `;
                posContainer.appendChild(item);
            });
        } else {
            posContainer.innerHTML = '<div class="empty-state">暂无任何持仓</div>';
        }

        await this.renderPerformanceChart();
        await this.renderRecentTrades();
    }

    async renderPerformanceChart() {
        const res = await fetch(`${this.app.apiBase}/performance?days=15`);
        const data = await res.json();
        if (!data.success || !data.performance) return;

        const chartDom = document.getElementById('perf-chart');
        if (!this.chart) {
            this.chart = echarts.init(chartDom);
        }

        const dates = data.performance.map(item => item.date);
        const returns = data.performance.map(item => (item.cumulative_pnl_pct * 100).toFixed(2));
        const benchmarks = data.performance.map(item => (item.benchmark_pnl_pct * 100).toFixed(2));

        const option = {
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'axis',
                backgroundColor: 'rgba(25, 30, 45, 0.9)',
                borderWidth: 0,
                textStyle: { color: '#f0f3f8' }
            },
            legend: {
                data: ['AI交易员', '沪深300'],
                textStyle: { color: '#8a96a8' }
            },
            grid: {
                left: '3%', right: '4%', bottom: '3%', containLabel: true
            },
            xAxis: {
                type: 'category',
                data: dates,
                axisLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } },
                axisLabel: { color: '#8a96a8' }
            },
            yAxis: {
                type: 'value',
                axisLabel: { formatter: '{value}%', color: '#8a96a8' },
                splitLine: { lineStyle: { color: 'rgba(255,255,255,0.04)' } }
            },
            series: [
                {
                    name: 'AI交易员',
                    type: 'line',
                    data: returns,
                    smooth: true,
                    itemStyle: { color: '#5ce2a4' },
                    areaStyle: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                            { offset: 0, color: 'rgba(92, 226, 164, 0.2)' },
                            { offset: 1, color: 'rgba(92, 226, 164, 0)' }
                        ])
                    }
                },
                {
                    name: '沪深300',
                    type: 'line',
                    data: benchmarks,
                    smooth: true,
                    itemStyle: { color: '#5b79e2' }
                }
            ]
        };

        this.chart.setOption(option);
        window.addEventListener('resize', () => this.chart.resize());
    }

    async renderRecentTrades() {
        const res = await fetch(`${this.app.apiBase}/trades?limit=5`);
        const data = await res.json();
        const tbody = document.getElementById('trades-table-body');
        tbody.innerHTML = '';

        if (!data.success || !data.trades || data.trades.length === 0) {
            tbody.innerHTML = '<tr><td colspan="8" class="text-center">暂无历史成交记录</td></tr>';
            return;
        }

        data.trades.forEach(t => {
            const tr = document.createElement('tr');
            const actionClass = t.action === 'BUY' ? 'green-text' : 'red-text';
            const actionText = t.action === 'BUY' ? '买入' : '卖出';
            const pnlClass = t.pnl >= 0 ? 'green-text' : 'red-text';
            const pnlText = t.pnl_pct != 0 ? `${t.pnl >= 0 ? '+' : ''}${t.pnl.toLocaleString('zh-CN', {maximumFractionDigits:0})} (${(t.pnl_pct * 100).toFixed(2)}%)` : '-';

            tr.innerHTML = `
                <td>${t.date}</td>
                <td><span style="font-weight:600;">${t.name}</span><br><span style="font-size:11px;color:gray;">${t.code}</span></td>
                <td class="${actionClass}">${actionText}</td>
                <td class="font-outfit">￥${t.price.toFixed(2)}</td>
                <td class="font-outfit">${t.shares}股</td>
                <td class="font-outfit">￥${t.fee.toFixed(2)}</td>
                <td class="${pnlClass} font-outfit">${pnlText}</td>
                <td style="max-width: 250px; font-size:12px; color:var(--text-secondary);" title="${t.reason}">${t.reason ? t.reason : '-'}</td>
            `;
            tbody.appendChild(tr);
        });
    }
}
