export class DashboardTab {
    constructor(app) {
        this.app = app;
        this.chart = null;
        this.resizeBound = false;
    }

    money(value, digits = 0) {
        const number = Number(value || 0);
        return `￥${number.toLocaleString('zh-CN', {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits
        })}`;
    }

    pct(value, digits = 1) {
        const number = Number(value || 0) * 100;
        return `${number >= 0 ? '+' : ''}${number.toFixed(digits)}%`;
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

    setText(id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    setClass(id, className) {
        const el = document.getElementById(id);
        if (el) el.className = className;
    }

    actionText(action) {
        return {
            BUY: '买入',
            SELL: '卖出',
            HOLD: '持有'
        }[action] || this.text(action);
    }

    async load() {
        const data = this.app.globalData;
        if (!data || !data.account) return;

        const positions = await this.loadPositions();
        const performance = await this.loadPerformance();
        const trades = await this.loadTrades();
        const decisions = await this.loadDecisions();

        this.renderAccount(data, positions, trades, performance);
        this.renderCockpit(data, positions);
        this.renderWarnings(data);
        this.renderPublicEvents(data);
        this.renderPositions(positions);
        this.renderDecisionSummary(decisions);
        this.renderStrategySnapshot(data);
        this.renderPerformanceChart(performance);
        this.renderRecentTrades(trades);
    }

    async fetchJson(url) {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
    }

    async loadPositions() {
        try {
            const data = await this.fetchJson(`${this.app.apiBase}/positions`);
            return data.success ? (data.positions || []) : [];
        } catch (e) {
            console.error('Failed to load positions:', e);
            return [];
        }
    }

    async loadPerformance() {
        try {
            const data = await this.fetchJson(`${this.app.apiBase}/performance?days=15`);
            return data.success ? (data.performance || []) : [];
        } catch (e) {
            console.error('Failed to load performance:', e);
            return [];
        }
    }

    async loadTrades() {
        try {
            const data = await this.fetchJson(`${this.app.apiBase}/trades?limit=12`);
            return data.success ? {
                total: data.total || 0,
                trades: data.trades || []
            } : { total: 0, trades: [] };
        } catch (e) {
            console.error('Failed to load trades:', e);
            return { total: 0, trades: [] };
        }
    }

    async loadDecisions() {
        try {
            const data = await this.fetchJson(`${this.app.apiBase}/decisions?limit=6`);
            return data.success ? (data.decisions || []) : [];
        } catch (e) {
            console.error('Failed to load decisions:', e);
            return [];
        }
    }

    renderAccount(data, positions, tradesData, performance) {
        const account = data.account;
        const latest = performance.length ? performance[performance.length - 1] : {};
        const totalPnl = account.total_pnl !== undefined
            ? Number(account.total_pnl)
            : Number(account.total_assets || 0) - Number(account.initial_capital || 0);
        const totalPnlPct = account.total_pnl_pct !== undefined
            ? Number(account.total_pnl_pct)
            : (Number(account.initial_capital || 0) > 0 ? totalPnl / Number(account.initial_capital) : 0);
        const dailyPnl = Number(latest.daily_pnl || 0);
        const positionValue = positions.reduce((sum, pos) => sum + Number(pos.market_value || 0), 0);
        const sellTrades = tradesData.trades.filter(t => t.action === 'SELL');
        const winningTrades = sellTrades.filter(t => Number(t.pnl || 0) > 0);
        const winRate = sellTrades.length ? winningTrades.length / sellTrades.length : null;

        this.setText('total-assets', this.money(account.total_assets));
        this.setText('available-cash', this.money(account.cash));
        this.setText('position-value', this.money(positionValue));
        this.setText('position-count', String(positions.length));
        this.setText('trade-count', String(tradesData.total || tradesData.trades.length || 0));
        this.setText('win-rate', winRate === null ? '-' : `${(winRate * 100).toFixed(1)}%`);

        const pnlClass = totalPnl >= 0 ? 'green-text' : 'red-text';
        this.setText('total-pnl', `${totalPnl >= 0 ? '+' : ''}${this.money(Math.abs(totalPnl)).replace('￥', '￥')} (${this.pct(totalPnlPct, 2)})`);
        this.setClass('total-pnl', `stat-val font-outfit ${pnlClass}`);

        this.setText('daily-pnl', `${dailyPnl >= 0 ? '+' : ''}${this.money(Math.abs(dailyPnl)).replace('￥', '￥')}`);
        this.setClass('daily-pnl', `stat-val font-outfit ${dailyPnl >= 0 ? 'green-text' : 'red-text'}`);

        this.setText('loop-count', String(data.loop_count ?? '-'));
        this.setText('last-loop-time', this.formatTime(data.last_loop_time));
    }

    renderCockpit(data, positions) {
        const account = data.account || {};
        const totalAssets = Number(account.total_assets || 0);
        const cash = Number(account.cash || 0);
        const positionValue = positions.reduce((sum, pos) => sum + Number(pos.market_value || 0), 0);
        const cashRatio = totalAssets > 0 ? cash / totalAssets : 0;
        const exposure = totalAssets > 0 ? positionValue / totalAssets : 0;

        this.setText('cash-ratio', `${(cashRatio * 100).toFixed(1)}%`);
        this.setText('exposure-ratio', `${(exposure * 100).toFixed(1)}%`);
        this.setText('public-mode-label', data.public_mode ? '只读公开驾驶舱' : '内部驾驶舱');

        const wdDot = document.getElementById('cockpit-watchdog');
        const wdText = document.getElementById('cockpit-watchdog-text');
        if (wdDot && wdText) {
            if (data.watchdog?.ok) {
                wdDot.className = 'status-indicator-dot green-dot';
                wdText.textContent = '守护正常';
                wdText.className = 'pos-value green-text';
            } else {
                wdDot.className = 'status-indicator-dot red-dot pulse-dot';
                wdText.textContent = '守护异常';
                wdText.className = 'pos-value red-text';
            }
        }

        const trDot = document.getElementById('cockpit-trading');
        const trText = document.getElementById('cockpit-trading-text');
        if (trDot && trText) {
            if (!data.control?.paused) {
                trDot.className = 'status-indicator-dot green-dot';
                trText.textContent = '自动驾驶中';
                trText.className = 'pos-value green-text';
            } else {
                trDot.className = 'status-indicator-dot orange-dot pulse-dot';
                trText.textContent = '交易暂停';
                trText.className = 'pos-value orange-text';
            }
        }

        ['prefetch', 'scan', 'execute', 'review'].forEach(step => {
            const el = document.getElementById(`step-${step}`);
            if (el) el.classList.toggle('completed', Boolean(data.pipeline_progress?.[step]));
        });

        if (window.lucide) window.lucide.createIcons();
    }

    renderWarnings(data) {
        const riskList = document.getElementById('risk-warnings-list');
        if (!riskList) return;

        riskList.innerHTML = '';
        const warnings = data.risk_warnings || [];
        if (!warnings.length) {
            riskList.innerHTML = '<div class="empty-state compact">运行状况正常</div>';
            return;
        }

        warnings.forEach(warn => {
            const item = document.createElement('div');
            item.className = 'risk-warning-item';
            item.innerHTML = `<i data-lucide="alert-circle" class="inline-icon danger"></i><span>${this.escape(warn)}</span>`;
            riskList.appendChild(item);
        });
        if (window.lucide) window.lucide.createIcons();
    }

    renderPublicEvents(data) {
        const logList = document.getElementById('events-list');
        if (!logList) return;

        logList.innerHTML = '';
        const logs = data.recent_logs || [];
        if (!logs.length) {
            logList.innerHTML = '<div class="empty-state compact">暂无公开动作流</div>';
            return;
        }

        logs.slice(0, 8).forEach(log => {
            const item = document.createElement('div');
            item.className = 'log-item';
            const timeText = log.time ? `[${this.escape(log.time)}]` : '';
            const statusText = log.status ? `(${this.escape(log.status)})` : '';
            const typeText = this.escape(log.type || 'cycle');
            const actionText = this.escape(log.action || '-');
            const errText = log.error ? `<span class="red-text"> ${this.escape(log.error)}</span>` : '';
            item.innerHTML = `${timeText} <span class="log-type">${typeText}</span>${statusText}: ${actionText}${errText}`;
            logList.appendChild(item);
        });
    }

    renderPositions(positions) {
        const container = document.getElementById('positions-list');
        if (!container) return;

        container.innerHTML = '';
        if (!positions.length) {
            container.innerHTML = '<div class="empty-state">当前账户无持仓</div>';
            return;
        }

        positions.forEach(pos => {
            const pnl = Number(pos.pnl || 0);
            const pnlClass = pnl >= 0 ? 'green-text' : 'red-text';
            const latestDecision = pos.latest_decision || {};
            const action = latestDecision.action || 'HOLD';
            const actionClass = action === 'BUY' ? 'badge-success' : (action === 'SELL' ? 'badge-danger' : 'badge-neutral');
            const confidence = latestDecision.confidence !== undefined ? ` (${(Number(latestDecision.confidence) * 100).toFixed(0)}%)` : '';
            const decisionTitle = this.escape(latestDecision.reasoning || '-');

            const item = document.createElement('div');
            item.className = 'position-item-card';
            item.innerHTML = `
                <div class="pos-card-header">
                    <div class="pos-title-block">
                        <span class="pos-name">${this.escape(pos.name || pos.code)}</span>
                        <span class="pos-code">${this.escape(pos.code)}</span>
                    </div>
                    <div class="pos-badges">
                        <span class="badge weight-badge">权重 ${(Number(pos.weight || 0) * 100).toFixed(1)}%</span>
                        <span class="badge ${actionClass}" title="最新决策原因: ${decisionTitle}">${this.actionText(action)}${confidence}</span>
                    </div>
                </div>
                <div class="pos-card-body">
                    <div><span>持股数</span><b>${Number(pos.shares || 0).toLocaleString('zh-CN')}股</b></div>
                    <div><span>买入成本</span><b>${this.money(pos.buy_price, 2)}</b></div>
                    <div><span>当前价格</span><b>${this.money(pos.current_price, 2)}</b></div>
                    <div><span>浮动盈亏</span><b class="${pnlClass}">${this.money(pnl)} (${this.pct(pos.pnl_pct, 2)})</b></div>
                    <div><span>移动止损</span><b class="orange-text">${pos.trailing_stop_price ? this.money(pos.trailing_stop_price, 2) : '-'}</b></div>
                    <div><span>目标止盈</span><b class="green-text">${pos.take_profit_price ? this.money(pos.take_profit_price, 2) : '-'}</b></div>
                </div>
            `;
            container.appendChild(item);
        });
    }

    renderDecisionSummary(decisions) {
        const feed = document.getElementById('decision-feed');
        if (!feed) return;

        feed.innerHTML = '';
        if (!decisions.length) {
            feed.innerHTML = '<div class="empty-state compact">暂无决策摘要</div>';
            this.setText('latest-decision', '-');
            this.setText('avg-confidence', '-');
            return;
        }

        const avgConfidence = decisions.reduce((sum, d) => sum + Number(d.confidence || 0), 0) / decisions.length;
        const latest = decisions[0];
        this.setText('latest-decision', `${this.escape(latest.code)} ${this.actionText(latest.action)}`);
        this.setText('avg-confidence', `${(avgConfidence * 100).toFixed(0)}%`);

        decisions.slice(0, 4).forEach(d => {
            const row = document.createElement('div');
            row.className = 'feed-row';
            const actionClass = d.action === 'BUY' ? 'green-text' : (d.action === 'SELL' ? 'red-text' : '');
            row.innerHTML = `
                <div class="feed-main">
                    <span class="font-outfit">${this.escape(d.code)}</span>
                    <span class="${actionClass}">${this.actionText(d.action)}</span>
                    <span>${(Number(d.confidence || 0) * 100).toFixed(0)}%</span>
                </div>
                <p>${this.escape(d.reasoning || '暂无公开摘要')}</p>
            `;
            feed.appendChild(row);
        });
    }

    renderStrategySnapshot(data) {
        const adaptive = data.adaptive || {};
        this.setText('strategy-buy-threshold', adaptive.buy_threshold ?? '-');
        this.setText('strategy-min-score', adaptive.min_score ?? '-');
        this.setText('strategy-top-k', adaptive.top_k_delta ?? '-');
        this.setText('strategy-position-scale', adaptive.position_scale ?? '-');
    }

    renderPerformanceChart(performance) {
        const chartDom = document.getElementById('perf-chart');
        if (!chartDom) return;

        if (!performance.length) {
            chartDom.innerHTML = '<div class="empty-state" style="padding:40px;">暂无近15天业绩对比数据</div>';
            return;
        }

        if (!this.chart) this.chart = echarts.init(chartDom);

        const dates = performance.map(item => item.date);
        const returns = performance.map(item => (Number(item.cumulative_pnl_pct || 0) * 100).toFixed(2));
        const benchmarks = performance.map(item => (Number(item.benchmark_pnl_pct || 0) * 100).toFixed(2));

        this.chart.setOption({
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
            grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
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
        });

        if (!this.resizeBound) {
            window.addEventListener('resize', () => this.chart?.resize());
            this.resizeBound = true;
        }
    }

    renderRecentTrades(tradesData) {
        const tbody = document.getElementById('trades-table-body');
        if (!tbody) return;

        const trades = tradesData.trades || [];
        tbody.innerHTML = '';
        if (!trades.length) {
            tbody.innerHTML = '<tr><td colspan="8" class="text-center">暂无历史成交记录</td></tr>';
            return;
        }

        trades.slice(0, 8).forEach(t => {
            const tr = document.createElement('tr');
            const actionClass = t.action === 'BUY' ? 'green-text' : 'red-text';
            const pnl = Number(t.pnl || 0);
            const pnlClass = pnl >= 0 ? 'green-text' : 'red-text';
            const pnlText = t.action === 'SELL' ? `${pnl >= 0 ? '+' : ''}${this.money(Math.abs(pnl)).replace('￥', '￥')} (${this.pct(t.pnl_pct, 2)})` : '-';
            const reason = this.escape(t.reason || '-');

            tr.innerHTML = `
                <td>${this.escape(t.date)}</td>
                <td><span class="table-stock-name">${this.escape(t.name || t.code)}</span><br><span class="table-stock-code">${this.escape(t.code)}</span></td>
                <td class="${actionClass}">${this.actionText(t.action)}</td>
                <td class="font-outfit">${this.money(t.price, 2)}</td>
                <td class="font-outfit">${Number(t.shares || 0).toLocaleString('zh-CN')}股</td>
                <td class="font-outfit">${this.money(t.fee, 2)}</td>
                <td class="${pnlClass} font-outfit">${pnlText}</td>
                <td class="table-reason" title="${reason}">${reason}</td>
            `;
            tbody.appendChild(tr);
        });
    }

    formatTime(value) {
        if (!value || value === '-') return '-';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return this.text(value);
        return date.toLocaleString('zh-CN', { hour12: false });
    }
}
