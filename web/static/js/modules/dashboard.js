export class DashboardTab {
    constructor(app) {
        this.app = app;
        this.chart = null;
    }

    text(value, fallback = '-') {
        return value === null || value === undefined || value === '' ? fallback : String(value);
    }

    escape(value) {
        return this.text(value, '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    isKnownNumber(value) {
        return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
    }

    money(value, digits = 0) {
        const number = Number(value || 0);
        return `￥${number.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
    }

    signedMoney(value, digits = 0) {
        const number = Number(value || 0);
        return `${number > 0 ? '+' : number < 0 ? '-' : ''}${this.money(Math.abs(number), digits)}`;
    }

    pnlClass(value) {
        return Number(value || 0) > 0 ? 'red-text' : Number(value || 0) < 0 ? 'green-text' : '';
    }

    pct(value, digits = 1) {
        const number = Number(value || 0) * 100;
        return `${number > 0 ? '+' : ''}${number.toFixed(digits)}%`;
    }

    actionText(action) {
        return { BUY: '买入', SELL: '卖出', HOLD: '观察' }[action] || this.text(action);
    }

    tradePnlText(trade) {
        if (trade.action !== 'SELL') return '-';
        if (!this.isKnownNumber(trade.pnl) || !this.isKnownNumber(trade.pnl_pct)) return '未知盈亏';
        return `${this.signedMoney(trade.pnl)} (${this.pct(trade.pnl_pct, 2)})`;
    }

    setText(id, value) {
        const element = document.getElementById(id);
        if (element) element.textContent = value;
    }

    async fetchJson(url, fallback) {
        try {
            const response = await fetch(url);
            if (!response.ok) return fallback;
            const data = await response.json();
            return data.success === false ? fallback : data;
        } catch (error) {
            console.error(`Failed to load ${url}:`, error);
            return fallback;
        }
    }

    async load() {
        const data = this.app.globalData;
        if (!data || !data.account) return;
        const [positionsData, performanceData, tradesData] = await Promise.all([
            this.fetchJson(`${this.app.apiBase}/positions`, { positions: [] }),
            this.fetchJson(`${this.app.apiBase}/performance?days=30`, { performance: [] }),
            this.fetchJson(`${this.app.apiBase}/trades?limit=30`, { total: 0, trades: [] }),
        ]);
        const positions = positionsData.positions || [];
        const performance = performanceData.performance || [];
        const trades = tradesData.trades || [];

        this.renderTraderBrief(data);
        this.renderCapabilities(data.capabilities || []);
        this.renderJourney(data.daily_trader || {});
        this.renderAudit(data.daily_trader || {});
        this.renderStrategyHandoff(data);
        this.renderAccount(data, positions, performance, tradesData.total || trades.length);
        this.renderPositions(positions);
        this.renderTrades(trades);
        this.renderPerformance(performance);
        if (window.lucide) window.lucide.createIcons();
    }

    renderTraderBrief(data) {
        const brief = data.daily_trader || {};
        const labels = {
            closed: '休市待命', preparing: '盘前准备', waiting: '等待窗口', scanned: '扫描受限',
            observing: '主动观望', signal_pending: '信号待执行', executed: '已执行',
            reviewed: '复盘完成', attention: '需要关注', paused: '交易暂停',
        };
        this.setText('trader-state', labels[brief.state] || '状态读取中');
        const stateEl = document.getElementById('trader-state');
        if (stateEl) stateEl.className = `state-pill ${this.escape(brief.state || '')}`;
        this.setText('trader-date', brief.date || '-');
        this.setText('trader-market-status', brief.market_status || '-');
        this.setText('trader-headline', brief.headline || '每日交易员简报暂不可用');
        this.setText('trader-explanation', brief.explanation || '-');
        this.setText('trader-next-action', brief.next_action || '-');
        this.setText('trader-last-loop', `最后循环 ${this.formatTime(data.last_loop_time)}`);
    }

    renderCapabilities(capabilities) {
        const container = document.getElementById('capability-rail');
        if (!container) return;
        if (!capabilities.length) {
            container.innerHTML = '<div class="empty-state">暂无能力状态</div>';
            return;
        }
        container.innerHTML = capabilities.map(item => `
            <article class="capability-chip ${this.escape(item.status)}" title="${this.escape(item.summary)}">
                <header><span class="capability-state"></span><strong>${this.escape(item.label)}</strong></header>
                <p>${this.escape(item.summary)}</p>
            </article>
        `).join('');
    }

    renderJourney(brief) {
        const funnel = brief.funnel || {};
        const values = {
            'journey-candidates': funnel.candidates,
            'journey-scored': funnel.scored,
            'journey-evaluated': funnel.llm_evaluated,
            'journey-observations': funnel.observations,
            'journey-signals': Number(funnel.buy_signals || 0) + Number(funnel.sell_signals || 0),
            'journey-planned': funnel.planned_orders,
            'journey-filled': funnel.filled,
        };
        Object.entries(values).forEach(([id, value]) => this.setText(id, String(value || 0)));
        const signals = Number(funnel.buy_signals || 0) + Number(funnel.sell_signals || 0);
        const footer = brief.is_trading_day === false
            ? '休市日不执行扫描和交易，所有阶段均为不适用。'
            : `今日扫描 ${funnel.scan_cycles || 0} 轮；BUY ${funnel.buy_signals || 0}，SELL ${funnel.sell_signals || 0}，HOLD ${funnel.observations || 0}。${signals ? '交易信号仍需经过计划、风控和执行。' : '当前没有可执行交易信号。'}`;
        this.setText('journey-foot', footer);
    }

    renderAudit(brief) {
        const funnel = brief.funnel || {};
        this.setText('audit-blocked', String(funnel.blocked || 0));
        this.setText('audit-skipped', String(funnel.skipped || 0));
        this.setText('audit-failed', String(funnel.failed || 0));
        const container = document.getElementById('order-audit-list');
        const audits = brief.order_audit || [];
        if (!container) return;
        if (!audits.length) {
            container.innerHTML = '<div class="empty-state">今日没有订单执行记录</div>';
            return;
        }
        const statusLabels = { filled: '成交', blocked: '阻断', skipped: '跳过', failed: '失败' };
        container.innerHTML = audits.map(item => `
            <div class="fact-item">
                <span class="fact-badge ${this.escape(item.status)}">${statusLabels[item.status] || this.escape(item.status)}</span>
                <div><strong>${this.escape(item.name || item.code)} · ${this.actionText(item.action)}</strong><p>${this.escape(item.reason || '未提供原因')}</p></div>
            </div>
        `).join('');
    }

    renderStrategyHandoff(data) {
        const strategy = data.daily_trader?.strategy || {};
        const current = strategy.current || data.strategy_directive || null;
        const pending = strategy.pending || data.pending_strategy_directive || null;
        const formatMeta = item => item
            ? `${item.effective_date || '-'} · Top ${item.params?.top_k ?? '-'} · 最低分 ${item.params?.min_score ?? '-'} · 单票 ${this.isKnownNumber(item.params?.max_weight) ? `${(Number(item.params.max_weight) * 100).toFixed(0)}%` : '-'}`
            : '-';
        this.setText('current-strategy-intent', current?.intent || '等待策略');
        this.setText('current-strategy-meta', formatMeta(current));
        this.setText('pending-strategy-intent', pending?.intent || '尚未生成');
        this.setText('pending-strategy-meta', formatMeta(pending));
        this.renderDiff('strategy-diff-list', strategy.diff || []);
    }

    renderDiff(id, changes) {
        const container = document.getElementById(id);
        if (!container) return;
        if (!changes.length) {
            container.innerHTML = '<div class="empty-state">当前与下一策略没有参数变化</div>';
            return;
        }
        const format = (key, value) => key === 'max_weight' && this.isKnownNumber(value)
            ? `${(Number(value) * 100).toFixed(0)}%` : this.text(value);
        container.innerHTML = changes.map(item => `
            <div class="strategy-diff"><span>${this.escape(item.label)}</span><span>${this.escape(format(item.key, item.before))}</span><b>→ ${this.escape(format(item.key, item.after))}</b></div>
        `).join('');
    }

    renderAccount(data, positions, performance, totalTrades) {
        const account = data.account || {};
        const latest = performance.length ? performance[performance.length - 1] : {};
        const totalPnl = account.total_pnl !== undefined ? Number(account.total_pnl) : Number(account.total_assets || 0) - Number(account.initial_capital || 0);
        const positionValue = positions.reduce((sum, item) => sum + Number(item.market_value || 0), 0);
        const cashRatio = Number(account.total_assets || 0) > 0 ? Number(account.cash || 0) / Number(account.total_assets) : 0;
        this.setText('total-assets', this.money(account.total_assets));
        this.setText('total-pnl', `${this.signedMoney(totalPnl)} · ${this.pct(account.total_pnl_pct, 2)}`);
        this.setText('daily-pnl', this.signedMoney(latest.daily_pnl || 0));
        this.setText('available-cash', this.money(account.cash));
        this.setText('cash-ratio', `现金占比 ${(cashRatio * 100).toFixed(1)}%`);
        this.setText('position-count', `${positions.length} 只`);
        this.setText('position-value', `持仓市值 ${this.money(positionValue)}`);
        this.setText('trade-count', `历史成交 ${totalTrades} 笔`);
    }

    renderPositions(positions) {
        const container = document.getElementById('positions-list');
        if (!container) return;
        if (!positions.length) {
            container.innerHTML = '<div class="empty-state">当前账户为空仓</div>';
            return;
        }
        container.innerHTML = positions.map(pos => {
            const decision = pos.latest_decision || {};
            const confidence = this.isKnownNumber(decision.confidence) ? `${(Number(decision.confidence) * 100).toFixed(0)}%` : '-';
            return `
                <article class="position-card">
                    <div class="position-head"><strong>${this.escape(pos.name || pos.code)}</strong><small>${this.escape(pos.code)}</small></div>
                    <div class="position-numbers">
                        <div><span>持股</span><b>${Number(pos.shares || 0).toLocaleString('zh-CN')} 股</b></div>
                        <div><span>当前价格</span><b>${this.money(pos.current_price, 2)}</b></div>
                        <div><span>浮动盈亏</span><b class="${this.pnlClass(pos.pnl)}">${this.signedMoney(pos.pnl)} ${this.pct(pos.pnl_pct, 2)}</b></div>
                    </div>
                    <div class="decision-confidence">最新观察：${this.actionText(decision.action || 'HOLD')} · 决策置信度 ${confidence}</div>
                </article>`;
        }).join('');
    }

    renderTrades(trades) {
        const body = document.getElementById('trades-table-body');
        if (!body) return;
        if (!trades.length) {
            body.innerHTML = '<tr><td colspan="7" class="empty-state">暂无模拟成交</td></tr>';
            return;
        }
        body.innerHTML = trades.map(trade => `
            <tr>
                <td>${this.escape(trade.date)}</td><td>${this.escape(trade.name || trade.code)}<br><small>${this.escape(trade.code)}</small></td>
                <td>${this.actionText(trade.action)}</td><td>${this.money(trade.price, 2)}</td><td>${Number(trade.shares || 0).toLocaleString('zh-CN')}</td>
                <td class="${this.pnlClass(trade.pnl)}">${this.tradePnlText(trade)}</td><td>${this.escape(trade.reason || '-')}</td>
            </tr>`).join('');
    }

    renderPerformance(performance) {
        const dom = document.getElementById('perf-chart');
        if (!dom || !window.echarts) return;
        if (!this.chart) this.chart = window.echarts.init(dom);
        const values = performance.map(item => Number(item.total_assets || 0));
        this.chart.setOption({
            backgroundColor: 'transparent',
            tooltip: { trigger: 'axis', backgroundColor: '#344234', borderWidth: 0, textStyle: { color: '#e8dcc7' } },
            grid: { left: 10, right: 16, top: 28, bottom: 18, containLabel: true },
            xAxis: { type: 'category', data: performance.map(item => item.date?.slice(5)), axisLine: { lineStyle: { color: 'rgba(52,66,52,.18)' } }, axisLabel: { color: '#6f725e', fontSize: 9 } },
            yAxis: { type: 'value', scale: true, axisLabel: { color: '#6f725e', fontSize: 9, formatter: value => `${(value / 10000).toFixed(0)}万` }, splitLine: { lineStyle: { color: 'rgba(52,66,52,.1)' } } },
            series: [{ type: 'line', data: values, smooth: true, symbol: 'none', lineStyle: { color: '#c66b3d', width: 3 }, areaStyle: { color: 'rgba(198,107,61,.12)' } }],
        });
        setTimeout(() => this.chart?.resize(), 50);
    }

    formatTime(value) {
        if (!value || value === '-') return '-';
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? this.text(value) : date.toLocaleString('zh-CN', { hour12: false });
    }
}
