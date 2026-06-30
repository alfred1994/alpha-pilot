export class DashboardTab {
    constructor(app) {
        this.app = app;
        this.chart = null;
    }

    async load() {
        const data = this.app.globalData;
        if (!data) return;

        // Render dashboard values
        document.getElementById('total-assets').textContent = `￥${data.account.total_assets.toLocaleString('zh-CN', {minimumFractionDigits: 0, maximumFractionDigits: 0})}`;
        document.getElementById('available-cash').textContent = `￥${data.account.cash.toLocaleString('zh-CN', {minimumFractionDigits: 0, maximumFractionDigits: 0})}`;
        
        let dailyPnl = 0.0;
        let cumulativePnlPct = 0.0;
        try {
            const pnlRes = await fetch(`${this.app.apiBase}/performance?days=2`);
            if (pnlRes.ok) {
                const pnlData = await pnlRes.json();
                if (pnlData.success && pnlData.performance && pnlData.performance.length > 0) {
                    const latest = pnlData.performance[pnlData.performance.length - 1];
                    dailyPnl = latest.daily_pnl;
                    cumulativePnlPct = latest.cumulative_pnl_pct;
                }
            }
        } catch (e) {
            console.error("Failed to fetch performance for PnL stats:", e);
        }
        
        // 优先使用后端计算的总盈亏百分比
        if (data.account && data.account.total_pnl_pct !== undefined) {
            cumulativePnlPct = data.account.total_pnl_pct;
        }

        const totalPnlVal = data.account.total_pnl !== undefined ? data.account.total_pnl : (data.account.total_assets - 1000000.0);
        
        const totalPnlBox = document.getElementById('total-pnl');
        totalPnlBox.textContent = `${totalPnlVal >= 0 ? '+' : ''}${totalPnlVal.toLocaleString('zh-CN', {maximumFractionDigits:0})} (${(cumulativePnlPct * 100).toFixed(2)}%)`;
        totalPnlBox.className = `stat-val ${totalPnlVal >= 0 ? 'green-text' : 'red-text'}`;

        const dailyPnlBox = document.getElementById('daily-pnl');
        dailyPnlBox.textContent = `${dailyPnl >= 0 ? '+' : ''}${dailyPnl.toLocaleString('zh-CN', {maximumFractionDigits:0})}`;
        dailyPnlBox.className = `stat-val ${dailyPnl >= 0 ? 'green-text' : 'red-text'}`;

        // Render Cockpit Status
        const wdDot = document.getElementById('cockpit-watchdog');
        const wdText = document.getElementById('cockpit-watchdog-text');
        if (wdDot && wdText) {
            if (data.watchdog?.ok) {
                wdDot.className = 'status-indicator-dot green-dot';
                wdText.textContent = '看门狗服务正常';
                wdText.className = 'pos-value green-text';
            } else {
                wdDot.className = 'status-indicator-dot red-dot pulse-dot';
                wdText.textContent = '服务异常或延滞';
                wdText.className = 'pos-value red-text';
            }
        }

        const trDot = document.getElementById('cockpit-trading');
        const trText = document.getElementById('cockpit-trading-text');
        if (trDot && trText) {
            if (!data.control?.paused) {
                trDot.className = 'status-indicator-dot green-dot';
                trText.textContent = '自动驾驶交易中';
                trText.className = 'pos-value green-text';
            } else {
                trDot.className = 'status-indicator-dot orange-dot pulse-dot';
                trText.textContent = `交易暂停挂起 (${data.control?.reason || '手动暂停'})`;
                trText.className = 'pos-value orange-text';
            }
        }

        // Render Today closed loop progress
        const steps = ['prefetch', 'scan', 'execute', 'review'];
        steps.forEach(step => {
            const el = document.getElementById(`step-${step}`);
            if (el) {
                if (data.pipeline_progress?.[step]) {
                    el.classList.add('completed');
                } else {
                    el.classList.remove('completed');
                }
            }
        });

        // Render Cockpit Warning list
        const riskList = document.getElementById('risk-warnings-list');
        if (riskList) {
            riskList.innerHTML = '';
            if (data.risk_warnings && data.risk_warnings.length > 0) {
                data.risk_warnings.forEach(warn => {
                    const item = document.createElement('div');
                    item.className = 'risk-warning-item';
                    item.innerHTML = `<i data-lucide="alert-circle" style="width:14.5px; height:14.5px; color:var(--danger-color);"></i> <span>${warn}</span>`;
                    riskList.appendChild(item);
                });
                if (window.lucide) {
                    window.lucide.createIcons();
                }
            } else {
                riskList.innerHTML = '<div class="empty-state" style="padding: 10px;">运行状况正常</div>';
            }
        }

        // Render Recent Logs
        const logList = document.getElementById('events-list');
        if (logList) {
            logList.innerHTML = '';
            if (data.recent_logs && data.recent_logs.length > 0) {
                data.recent_logs.forEach(log => {
                    const item = document.createElement('div');
                    item.className = 'log-item';
                    const timeText = log.time ? `[${log.time}]` : '';
                    const errText = log.error ? `<span class="red-text"> 错误: ${log.error}</span>` : '';
                    const statusText = log.status ? `(${log.status})` : '';
                    item.innerHTML = `${timeText} <span style="color:#f7ca5e; font-weight:500;">${log.type || 'cycle'}</span>${statusText}: ${log.action}${errText}`;
                    logList.appendChild(item);
                });
            } else {
                logList.innerHTML = '<div class="empty-state" style="padding: 10px;">暂无日志记录</div>';
            }
        }

        // Render Positions Detailed Cockpit Cards
        const posContainer = document.getElementById('positions-list');
        posContainer.innerHTML = '<div class="empty-state">加载持仓数据中...</div>';
        
        try {
            const posRes = await fetch(`${this.app.apiBase}/positions`);
            if (!posRes.ok) {
                throw new Error(`HTTP Error ${posRes.status}`);
            }
            const posData = await posRes.json();
            posContainer.innerHTML = '';
            if (posData.success && posData.positions && posData.positions.length > 0) {
                posData.positions.forEach(pos => {
                    const pnlClass = pos.pnl >= 0 ? 'green-text' : 'red-text';
                    const pnlPctText = `${pos.pnl >= 0 ? '+' : ''}${(pos.pnl_pct * 100).toFixed(2)}%`;
                    
                    const slText = pos.stop_loss_price ? `￥${pos.stop_loss_price.toFixed(2)}` : '-';
                    const tsText = pos.trailing_stop_price ? `￥${pos.trailing_stop_price.toFixed(2)}` : '-';
                    
                    let decisionTag = '';
                    if (pos.latest_decision) {
                        const act = pos.latest_decision.action;
                        const actClass = act === 'BUY' ? 'badge-success' : (act === 'SELL' ? 'badge-danger' : 'badge-neutral');
                        const actText = act === 'BUY' ? '买入' : (act === 'SELL' ? '卖出' : '持有');
                        decisionTag = `<span class="badge ${actClass}" style="margin-left:4px;" title="最新决策原因: ${pos.latest_decision.reasoning || '-'}">${actText} (${(pos.latest_decision.confidence * 100).toFixed(0)}%)</span>`;
                    }

                    const item = document.createElement('div');
                    item.className = 'position-item-card';
                    item.innerHTML = `
                        <div class="pos-card-header" style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid rgba(255,255,255,0.03); padding-bottom:6px; margin-bottom:6px;">
                            <div>
                                <span class="pos-name" style="font-weight:600; font-size:14px; color:var(--text-primary);">${pos.name}</span>
                                <span class="pos-code" style="font-size:11px; color:var(--text-muted); margin-left:6px;">${pos.code}</span>
                            </div>
                            <div style="display:flex; align-items:center; gap: 4px;">
                                <span class="badge" style="background:rgba(91,121,226,0.1); color:var(--primary-color);">权重 ${(pos.weight * 100).toFixed(1)}%</span>
                                ${decisionTag}
                            </div>
                        </div>
                        <div class="pos-card-body" style="display:grid; grid-template-columns:repeat(2, 1fr); gap:8px; font-size:12.5px;">
                            <div>
                                <span style="color:var(--text-secondary);">持股数: </span><span class="font-outfit" style="color:var(--text-primary);">${pos.shares}股</span>
                            </div>
                            <div>
                                <span style="color:var(--text-secondary);">买入成本: </span><span class="font-outfit" style="color:var(--text-primary);">￥${pos.buy_price.toFixed(2)}</span>
                            </div>
                            <div>
                                <span style="color:var(--text-secondary);">当前价格: </span><span class="font-outfit" style="color:var(--text-primary);">￥${pos.current_price.toFixed(2)}</span>
                            </div>
                            <div>
                                <span style="color:var(--text-secondary);">浮动盈亏: </span><span class="font-outfit ${pnlClass}" style="font-weight:600;">￥${pos.pnl.toLocaleString('zh-CN', {maximumFractionDigits:0})} (${pnlPctText})</span>
                            </div>
                            <div style="grid-column: span 2; display:flex; justify-content:space-between; border-top:1px dashed rgba(255,255,255,0.03); padding-top:6px; margin-top:4px; font-size:11.5px; color:var(--text-muted);">
                                <span>ATR止损: <span class="red-text font-outfit">${slText}</span></span>
                                <span>移动止损: <span class="orange-text font-outfit">${tsText}</span></span>
                            </div>
                        </div>
                    `;
                    posContainer.appendChild(item);
                });
            } else {
                posContainer.innerHTML = '<div class="empty-state">当前账户无持仓</div>';
            }
        } catch (e) {
            console.error("Failed to load detailed positions:", e);
            posContainer.innerHTML = `<div class="empty-state red-text" style="padding:10px;">⚠️ 获取持仓详情失败: ${e.message}</div>`;
        }

        await this.renderPerformanceChart();
        await this.renderRecentTrades();
    }

    async renderPerformanceChart() {
        const chartDom = document.getElementById('perf-chart');
        try {
            const res = await fetch(`${this.app.apiBase}/performance?days=15`);
            if (!res.ok) {
                throw new Error(`HTTP Error ${res.status}`);
            }
            const data = await res.json();
            if (!data.success || !data.performance || data.performance.length === 0) {
                chartDom.innerHTML = '<div class="empty-state" style="padding:40px;">暂无近15天业绩对比数据</div>';
                return;
            }

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
        } catch (e) {
            console.error("Failed to render performance chart:", e);
            chartDom.innerHTML = `<div class="empty-state red-text" style="padding:40px;">⚠️ 收益率走势图加载失败: ${e.message}</div>`;
        }
    }

    async renderRecentTrades() {
        const tbody = document.getElementById('trades-table-body');
        try {
            const res = await fetch(`${this.app.apiBase}/trades?limit=5`);
            if (!res.ok) {
                throw new Error(`HTTP Error ${res.status}`);
            }
            const data = await res.json();
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
                
                let pnlText = '-';
                if (t.action === 'SELL') {
                    pnlText = `${t.pnl >= 0 ? '+' : ''}${t.pnl.toLocaleString('zh-CN', {maximumFractionDigits:0})} (${(t.pnl_pct * 100).toFixed(2)}%)`;
                }

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
        } catch (e) {
            console.error("Failed to load recent trades:", e);
            tbody.innerHTML = `<tr><td colspan="8" class="text-center red-text" style="font-weight:600; padding:20px;">⚠️ 加载交易成交明细失败: ${e.message}</td></tr>`;
        }
    }
}
