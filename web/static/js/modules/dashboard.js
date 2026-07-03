export class DashboardTab {
    constructor(app) {
        this.app = app;
        this.chart = null;
        this.distChart = null;
        this.resizeBound = false;
    }

    money(value, digits = 0) {
        const number = Number(value || 0);
        return `￥${number.toLocaleString('zh-CN', {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits
        })}`;
    }

    signedMoney(value, digits = 0) {
        const number = Number(value || 0);
        const prefix = number > 0 ? '+' : (number < 0 ? '-' : '');
        return `${prefix}${this.money(Math.abs(number), digits)}`;
    }

    pnlClass(value) {
        const number = Number(value || 0);
        if (number > 0) return 'red-text';
        if (number < 0) return 'green-text';
        return '';
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

    stockLabel(item) {
        const code = this.text(item?.code, '');
        const name = this.text(item?.name, '');
        if (name && name !== code) return `${name} ${code}`;
        return code || '-';
    }

    async load() {
        this.showSkeletons();
        const data = this.app.globalData;
        if (!data || !data.account) return;

        const [positions, performance, tradesData, decisions] = await Promise.all([
            this.loadPositions(),
            this.loadPerformance(),
            this.loadTrades(),
            this.loadDecisions()
        ]);

        this.hideSkeletons();
        this.renderKPIs(data, performance, positions, tradesData);
        this.renderAccount(data, positions, tradesData, performance);
        this.renderCockpit(data, positions);
        this.renderWarnings(data);
        this.renderPublicEvents(data);
        this.renderPositions(positions);
        this.renderDecisionSummary(decisions);
        this.renderStrategySnapshot(data);
        this.renderRiskMetrics(performance);
        this.renderPerformanceChart(performance, tradesData);
        this.renderPositionDistribution(positions);
        this.renderHeatmap(tradesData);
        this.renderRecentTrades(tradesData);
    }

    showSkeletons() {
        ['kpi-total-assets', 'kpi-daily-pnl', 'kpi-position-value', 'kpi-max-drawdown',
         'total-assets', 'available-cash', 'total-pnl', 'daily-pnl',
         'position-value', 'position-count', 'trade-count', 'win-rate'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.classList.add('skeleton', 'skeleton-text');
        });
    }

    hideSkeletons() {
        document.querySelectorAll('.skeleton').forEach(el => {
            el.classList.remove('skeleton', 'skeleton-text');
        });
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
            const data = await this.fetchJson(`${this.app.apiBase}/performance?days=30`);
            return data.success ? (data.performance || []) : [];
        } catch (e) {
            console.error('Failed to load performance:', e);
            return [];
        }
    }

    async loadTrades() {
        try {
            const data = await this.fetchJson(`${this.app.apiBase}/trades?limit=50`);
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

        const pnlClass = this.pnlClass(totalPnl);
        this.setText('total-pnl', `${this.signedMoney(totalPnl)} (${this.pct(totalPnlPct, 2)})`);
        this.setClass('total-pnl', `stat-val font-outfit ${pnlClass}`);

        this.setText('daily-pnl', this.signedMoney(dailyPnl));
        this.setClass('daily-pnl', `stat-val font-outfit ${this.pnlClass(dailyPnl)}`);

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
            const pnlClass = this.pnlClass(pnl);
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
                    <div><span>浮动盈亏</span><b class="${pnlClass}">${this.signedMoney(pnl)} (${this.pct(pos.pnl_pct, 2)})</b></div>
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
        this.setText('latest-decision', `${this.stockLabel(latest)} ${this.actionText(latest.action)}`);
        this.setText('avg-confidence', `${(avgConfidence * 100).toFixed(0)}%`);

        decisions.slice(0, 4).forEach(d => {
            const row = document.createElement('div');
            row.className = 'feed-row';
            const actionClass = d.action === 'BUY' ? 'green-text' : (d.action === 'SELL' ? 'red-text' : '');
            row.innerHTML = `
                <div class="feed-main">
                    <span class="font-outfit">${this.escape(this.stockLabel(d))}</span>
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

    renderKPIs(data, performance, positions, tradesData) {
        const account = data.account || {};
        const totalAssets = Number(account.total_assets || 0);
        const latest = performance.length ? performance[performance.length - 1] : {};
        const dailyPnl = Number(latest.daily_pnl || 0);
        const positionValue = positions.reduce((sum, pos) => sum + Number(pos.market_value || 0), 0);
        const exposure = totalAssets > 0 ? positionValue / totalAssets : 0;
        const risk = this.computeRiskMetrics(performance);

        // 账户净值
        this.setText('kpi-total-assets', this.money(totalAssets));

        // 资产趋势：计算近两天变化
        if (performance.length >= 2) {
            const prev = Number(performance[performance.length - 2].total_assets || totalAssets);
            const curr = Number(performance[performance.length - 1].total_assets || totalAssets);
            const changePct = prev > 0 ? ((curr - prev) / prev) * 100 : 0;
            const trendEl = document.getElementById('kpi-assets-trend');
            if (trendEl) {
                const arrow = trendEl.querySelector('.trend-arrow');
                const change = trendEl.querySelector('.trend-change');
                if (arrow) arrow.textContent = changePct > 0.001 ? '↑' : changePct < -0.001 ? '↓' : '→';
                if (change) change.textContent = `${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%`;
                trendEl.className = `${changePct > 0.001 ? 'trend-up' : changePct < -0.001 ? 'trend-down' : 'trend-flat'}`;
            }
        }

        // 今日盈亏
        this.setText('kpi-daily-pnl', this.signedMoney(dailyPnl));
        this.setClass('kpi-daily-pnl', `kpi-value font-outfit num-animate ${this.pnlClass(dailyPnl)}`);
        const pnlTrendEl = document.getElementById('kpi-pnl-trend');
        if (pnlTrendEl) {
            pnlTrendEl.className = `${dailyPnl > 0 ? 'trend-profit' : (dailyPnl < 0 ? 'trend-loss' : 'trend-flat')}`;
            const arrow = pnlTrendEl.querySelector('.trend-arrow');
            const change = pnlTrendEl.querySelector('.trend-change');
            if (arrow) arrow.textContent = dailyPnl > 0 ? '↑' : (dailyPnl < 0 ? '↓' : '→');
            if (change) change.textContent = dailyPnl > 0 ? '盈利' : (dailyPnl < 0 ? '亏损' : '持平');
        }

        // 持仓市值
        this.setText('kpi-position-value', this.money(positionValue));
        const posTrendEl = document.getElementById('kpi-pos-trend');
        if (posTrendEl) {
            posTrendEl.className = `${exposure > 0.8 ? 'trend-down' : 'trend-flat'}`;
            const arrow = posTrendEl.querySelector('.trend-arrow');
            const change = posTrendEl.querySelector('.trend-change');
            if (arrow) arrow.textContent = exposure > 0.8 ? '⚠' : '→';
            if (change) change.textContent = `${(exposure * 100).toFixed(1)}% 仓位`;
        }
        this.setText('kpi-exposure-text', `${(exposure * 100).toFixed(1)}%`);

        // 最大回撤
        const ddPct = risk.maxDrawdown * 100;
        this.setText('kpi-max-drawdown', `${ddPct.toFixed(2)}%`);
        const ddTrendEl = document.getElementById('kpi-dd-trend');
        if (ddTrendEl) {
            ddTrendEl.className = `${ddPct > 15 ? 'trend-down' : ddPct > 5 ? 'trend-flat' : 'trend-up'}`;
            const arrow = ddTrendEl.querySelector('.trend-arrow');
            const change = ddTrendEl.querySelector('.trend-change');
            if (arrow) arrow.textContent = ddPct > 15 ? '⚠' : ddPct > 5 ? '→' : '✓';
            if (change) change.textContent = ddPct > 15 ? '高风险' : ddPct > 5 ? '中风险' : '低风险';
        }
    }

    computeRiskMetrics(performance) {
        if (!performance || performance.length < 2) {
            return { maxDrawdown: 0, sharpe: 0, annualized: 0, volatility: 0 };
        }

        // 日收益率序列
        const returns = [];
        for (let i = 1; i < performance.length; i++) {
            const prev = Number(performance[i - 1].total_assets || 1000000);
            const curr = Number(performance[i].total_assets || prev);
            returns.push((curr - prev) / prev);
        }

        // 最大回撤
        let peak = Number(performance[0].total_assets || 1000000);
        let maxDD = 0;
        for (const p of performance) {
            const val = Number(p.total_assets || 1000000);
            if (val > peak) peak = val;
            const dd = (peak - val) / peak;
            if (dd > maxDD) maxDD = dd;
        }

        // 波动率（年化，假设 252 个交易日）
        const mean = returns.reduce((s, r) => s + r, 0) / returns.length;
        const variance = returns.reduce((s, r) => s + (r - mean) ** 2, 0) / returns.length;
        const dailyVol = Math.sqrt(variance);
        const annualVol = dailyVol * Math.sqrt(252);

        // 夏普比率（假设无风险利率 2%）
        const riskFreeDaily = 0.02 / 252;
        const excessReturns = returns.map(r => r - riskFreeDaily);
        const excessMean = excessReturns.reduce((s, r) => s + r, 0) / excessReturns.length;
        const sharpe = dailyVol > 0 ? (excessMean / dailyVol) * Math.sqrt(252) : 0;

        // 年化收益率
        const firstAsset = Number(performance[0].total_assets || 1000000);
        const lastAsset = Number(performance[performance.length - 1].total_assets || firstAsset);
        const totalReturn = firstAsset > 0 ? (lastAsset - firstAsset) / firstAsset : 0;
        const days = performance.length;
        const annualized = days > 0 ? (Math.pow(1 + totalReturn, 252 / days) - 1) : 0;

        return { maxDrawdown: maxDD, sharpe, annualized, volatility: annualVol };
    }

    renderRiskMetrics(performance) {
        const risk = this.computeRiskMetrics(performance);

        // 最大回撤
        this.setText('risk-max-drawdown', `${(risk.maxDrawdown * 100).toFixed(2)}%`);
        const ddStatus = document.getElementById('risk-dd-status');
        if (ddStatus) {
            if (risk.maxDrawdown < 0.05) {
                ddStatus.textContent = '低风险';
                ddStatus.className = 'metric-status status-good';
            } else if (risk.maxDrawdown < 0.15) {
                ddStatus.textContent = '中风险';
                ddStatus.className = 'metric-status status-medium';
            } else {
                ddStatus.textContent = '高风险';
                ddStatus.className = 'metric-status status-bad';
            }
        }

        // 夏普比率
        this.setText('risk-sharpe', risk.sharpe.toFixed(2));
        const sharpeStatus = document.getElementById('risk-sharpe-status');
        if (sharpeStatus) {
            if (risk.sharpe > 1) {
                sharpeStatus.textContent = '优秀';
                sharpeStatus.className = 'metric-status status-good';
            } else if (risk.sharpe > 0.5) {
                sharpeStatus.textContent = '良好';
                sharpeStatus.className = 'metric-status status-medium';
            } else {
                sharpeStatus.textContent = '较差';
                sharpeStatus.className = 'metric-status status-bad';
            }
        }

        // 年化收益率
        this.setText('risk-annualized', `${(risk.annualized * 100).toFixed(2)}%`);
        const annualStatus = document.getElementById('risk-annual-status');
        if (annualStatus) {
            if (risk.annualized > 0.10) {
                annualStatus.textContent = '优秀';
                annualStatus.className = 'metric-status status-good';
            } else if (risk.annualized > 0) {
                annualStatus.textContent = '一般';
                annualStatus.className = 'metric-status status-medium';
            } else if (risk.annualized === 0) {
                annualStatus.textContent = '持平';
                annualStatus.className = 'metric-status status-medium';
            } else {
                annualStatus.textContent = '亏损';
                annualStatus.className = 'metric-status status-bad';
            }
        }

        // 波动率
        this.setText('risk-volatility', `${(risk.volatility * 100).toFixed(2)}%`);
        const volStatus = document.getElementById('risk-vol-status');
        if (volStatus) {
            if (risk.volatility < 0.15) {
                volStatus.textContent = '低波动';
                volStatus.className = 'metric-status status-good';
            } else if (risk.volatility < 0.25) {
                volStatus.textContent = '中波动';
                volStatus.className = 'metric-status status-medium';
            } else {
                volStatus.textContent = '高波动';
                volStatus.className = 'metric-status status-bad';
            }
        }
    }

    renderPerformanceChart(performance, tradesData) {
        const chartDom = document.getElementById('perf-chart');
        if (!chartDom) return;

        if (!performance.length) {
            chartDom.innerHTML = '<div class="empty-state" style="padding:40px;">暂无业绩对比数据</div>';
            return;
        }

        if (!this.chart) this.chart = echarts.init(chartDom);

        const dates = performance.map(item => item.date);
        const returns = performance.map(item => (Number(item.cumulative_pnl_pct || 0) * 100).toFixed(2));
        const benchmarks = performance.map(item => (Number(item.benchmark_pnl_pct || 0) * 100).toFixed(2));

        // 计算回撤序列
        const assets = performance.map(item => Number(item.total_assets || 1000000));
        const drawdowns = [];
        let peak = assets[0];
        for (const val of assets) {
            if (val > peak) peak = val;
            const dd = peak > 0 ? ((peak - val) / peak) * 100 : 0;
            drawdowns.push(dd.toFixed(2));
        }

        // 提取买卖点标注
        const buyPoints = [];
        const sellPoints = [];
        const trades = (tradesData && tradesData.trades) || [];
        trades.forEach(t => {
            const tradeDate = (t.date || '').split(' ')[0];
            if (!tradeDate) return;
            const dateIdx = dates.indexOf(tradeDate);
            if (dateIdx < 0) return;
            const point = {
                coord: [tradeDate, Number(returns[dateIdx] || 0)],
                symbol: 'triangle',
                symbolSize: 12,
                label: { show: false }
            };
            if (t.action === 'BUY') {
                point.itemStyle = { color: '#5ce2a4' };
                point.symbolRotate = 0;
                buyPoints.push(point);
            } else if (t.action === 'SELL') {
                point.itemStyle = { color: '#e25c5c' };
                point.symbolRotate = 180;
                sellPoints.push(point);
            }
        });

        const series = [
            {
                name: 'AI交易员',
                type: 'line',
                yAxisIndex: 0,
                data: returns,
                smooth: true,
                itemStyle: { color: '#5ce2a4' },
                areaStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: 'rgba(92, 226, 164, 0.2)' },
                        { offset: 1, color: 'rgba(92, 226, 164, 0)' }
                    ])
                },
                markPoint: {
                    data: [...buyPoints, ...sellPoints]
                }
            },
            {
                name: '沪深300',
                type: 'line',
                yAxisIndex: 0,
                data: benchmarks,
                smooth: true,
                itemStyle: { color: '#5b79e2' }
            },
            {
                name: '回撤',
                type: 'line',
                yAxisIndex: 1,
                data: drawdowns,
                smooth: true,
                lineStyle: { width: 0 },
                itemStyle: { color: 'transparent' },
                areaStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: 'rgba(226, 92, 92, 0)' },
                        { offset: 1, color: 'rgba(226, 92, 92, 0.25)' }
                    ])
                },
                symbol: 'none'
            }
        ];

        this.chart.setOption({
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'axis',
                backgroundColor: 'rgba(25, 30, 45, 0.9)',
                borderWidth: 0,
                textStyle: { color: '#f0f3f8' }
            },
            legend: {
                data: ['AI交易员', '沪深300', '回撤'],
                textStyle: { color: '#8a96a8' }
            },
            grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
            xAxis: {
                type: 'category',
                data: dates,
                axisLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } },
                axisLabel: { color: '#8a96a8' }
            },
            yAxis: [
                {
                    type: 'value',
                    name: '收益率',
                    nameTextStyle: { color: '#8a96a8' },
                    axisLabel: { formatter: '{value}%', color: '#8a96a8' },
                    splitLine: { lineStyle: { color: 'rgba(255,255,255,0.04)' } }
                },
                {
                    type: 'value',
                    name: '回撤',
                    nameTextStyle: { color: '#8a96a8' },
                    axisLabel: { formatter: '-{value}%', color: '#8a96a8' },
                    splitLine: { show: false },
                    inverse: false,
                    max: function (value) {
                        return Math.max(value.max * 1.5, 5);
                    }
                }
            ],
            series
        }, true);

        if (!this.resizeBound) {
            window.addEventListener('resize', () => this.chart?.resize());
            this.resizeBound = true;
        }
    }

    renderPositionDistribution(positions) {
        const chartDom = document.getElementById('position-dist-chart');
        if (!chartDom || !window.echarts) return;

        if (!positions.length) {
            chartDom.innerHTML = '<div class="empty-state">暂无持仓数据</div>';
            return;
        }

        if (!this.distChart) this.distChart = echarts.init(chartDom);

        const pieData = positions.map(pos => ({
            name: pos.name || pos.code,
            value: Number(pos.market_value || 0)
        }));

        this.distChart.setOption({
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'item',
                backgroundColor: 'rgba(25, 30, 45, 0.9)',
                borderWidth: 0,
                textStyle: { color: '#f0f3f8' },
                formatter: '{b}: {d}%'
            },
            legend: {
                orient: 'vertical',
                right: '5%',
                top: 'center',
                textStyle: { color: '#8a96a8', fontSize: 12 }
            },
            series: [{
                type: 'pie',
                radius: ['40%', '70%'],
                center: ['40%', '50%'],
                avoidLabelOverlap: true,
                itemStyle: { borderRadius: 6, borderColor: 'rgba(25,30,45,0.8)', borderWidth: 2 },
                label: { show: false },
                emphasis: {
                    label: { show: true, fontSize: 14, fontWeight: 'bold', color: '#f0f3f8' }
                },
                data: pieData,
                color: ['#5b79e2', '#5ce2a4', '#f7ca5e', '#e25c5c', '#a78bfa', '#38bdf8', '#fb923c', '#e879f9']
            }]
        });

        window.addEventListener('resize', () => this.distChart?.resize());
    }

    renderHeatmap(tradesData) {
        const grid = document.getElementById('heatmap-grid');
        if (!grid) return;

        grid.innerHTML = '';
        const trades = tradesData.trades || [];

        // 按日期汇总盈亏
        const dailyPnl = {};
        trades.forEach(t => {
            const date = (t.date || '').split(' ')[0];
            if (!date) return;
            dailyPnl[date] = (dailyPnl[date] || 0) + Number(t.pnl || 0);
        });

        // 生成最近 35 天（5周）的热力图
        const today = new Date();
        for (let i = 34; i >= 0; i--) {
            const d = new Date(today);
            d.setDate(d.getDate() - i);
            const dateStr = d.toISOString().split('T')[0];
            const pnl = dailyPnl[dateStr] || 0;

            let level = 'level-0';
            if (pnl > 0) {
                level = pnl > 5000 ? 'level-pos-3' : pnl > 2000 ? 'level-pos-2' : 'level-pos-1';
            } else if (pnl < 0) {
                level = pnl < -5000 ? 'level-neg-3' : pnl < -2000 ? 'level-neg-2' : 'level-neg-1';
            }

            const cell = document.createElement('div');
            cell.className = `heatmap-cell ${level}`;
            cell.title = `${dateStr}: ${pnl >= 0 ? '+' : ''}${pnl.toFixed(0)}元`;
            grid.appendChild(cell);
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
            const pnlClass = this.pnlClass(pnl);
            const pnlText = t.action === 'SELL' ? `${this.signedMoney(pnl)} (${this.pct(t.pnl_pct, 2)})` : '-';
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
