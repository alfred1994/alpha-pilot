import { chartColors } from '../chart-theme.js?v=2026100901';
// 漏斗是通过人数；拒绝筛选另设明确入口，避免把通过人数映射到拒绝原因。
const FUNNEL_STAGES = [
    { key: 'candidates', label: '候选观察' },
    { key: 'scored', label: '完成打分' },
    { key: 'llm_evaluated', label: 'LLM 判断' },
    { key: 'observations', label: '观察结论' },
    { key: 'signals', label: '交易信号' },
    { key: 'planned_orders', label: '待执行计划' },
    { key: 'filled', label: '模拟成交' },
];

const FUNNEL_COLORS = ['#c0dfa1', '#b2d095', '#a5c6a4', '#99bca8', '#92b7b2', '#89adbd', '#8c9faf'];

export class DashboardTab {
    constructor(app) {
        this.app = app;
        this.chart = null;
        this.funnelChart = null;
        this.requestId = 0;
        document.getElementById('journey-all-candidates')?.addEventListener('click', () => this.jumpToResearchLayer(''));
        document.getElementById('journey-rejected-candidates')?.addEventListener('click', () => this.jumpToResearchLayer('score_gate'));
        window.addEventListener('resize', () => {
            this.chart?.resize();
            this.funnelChart?.resize();
        });
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
        if (!this.isKnownNumber(value)) return '未知';
        const number = Number(value);
        return `￥${number.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
    }

    signedMoney(value, digits = 0) {
        if (!this.isKnownNumber(value)) return '未知';
        const number = Number(value);
        return `${number > 0 ? '+' : number < 0 ? '-' : ''}${this.money(Math.abs(number), digits)}`;
    }

    pnlClass(value) {
        return Number(value || 0) > 0 ? 'red-text' : Number(value || 0) < 0 ? 'green-text' : '';
    }

    pct(value, digits = 1) {
        if (!this.isKnownNumber(value)) return '未知';
        const number = Number(value) * 100;
        return `${number > 0 ? '+' : ''}${number.toFixed(digits)}%`;
    }

    actionText(action) {
        return { BUY: '买入', SELL: '卖出', HOLD: '观察' }[action] || this.escape(action);
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
            if (!response.ok) return null;
            const data = await response.json();
            return data.success === false ? null : data;
        } catch (error) {
            console.error(`Failed to load ${url}:`, error);
            return null;
        }
    }

    async load() {
        const requestId = ++this.requestId;
        const data = this.app.globalData;
        if (!data || !data.account) return;
        const [positionsData, performanceData, tradesData] = await Promise.all([
            this.fetchJson(`${this.app.apiBase}/positions`, { positions: [] }),
            this.fetchJson(`${this.app.apiBase}/performance?days=30`, { performance: [] }),
            this.fetchJson(`${this.app.apiBase}/trades?limit=30`, { total: 0, trades: [] }),
        ]);
        if (requestId !== this.requestId || this.app.currentTab !== 'dashboard' || this.app.statusUnavailable) return;
        const positions = positionsData?.positions || [];
        const performance = performanceData?.performance || [];
        const trades = tradesData?.trades || [];

        this.renderTraderBrief(data);
        this.renderCapabilities(data.capabilities || []);
        this.renderJourney(data.daily_trader || {});
        this.renderAudit(data.daily_trader || {});
        this.renderStrategyHandoff(data);
        this.renderAccount(data, positions, performance, tradesData?.total ?? trades.length);
        this.renderAllocation(data.account, positionsData ? positions : null);
        this.renderPositions(positions);
        this.renderTrades(trades);
        this.renderPerformance(performance);
        if (!positionsData) {
            this.setText('positions-list', '持仓读取失败，不能判定为空仓');
            this.setText('position-count', '不可用');
            this.setText('position-value', '持仓市值未知');
        }
        if (!tradesData) {
            const body = document.getElementById('trades-table-body');
            if (body) body.innerHTML = '<tr><td colspan="7">成交读取失败，请重试</td></tr>';
            this.setText('trade-count', '成交数未知');
        }
        if (!performanceData || !performance.length) this.setText('daily-pnl', '数据不可用');
        if (!performanceData && this.chart) {
            this.setText('performance-status', '净值读取失败，请稍后刷新');
            this.chart.clear();
            this.chart.setOption({ title: { text: '业绩读取失败', left: 'center' } });
        }
        if (data.account.available === false) {
            ['total-assets', 'total-pnl', 'available-cash', 'cash-ratio'].forEach(id => this.setText(id, '账户不可用'));
        }
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
        const briefDot = document.querySelector('.brief-live-dot');
        if (briefDot) briefDot.classList.toggle('needs-attention', ['paused', 'attention'].includes(brief.state));
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

    funnelValue(funnel, key) {
        if (key === 'signals') return this.isKnownNumber(funnel.buy_signals) && this.isKnownNumber(funnel.sell_signals)
            ? Number(funnel.buy_signals) + Number(funnel.sell_signals) : null;
        return this.isKnownNumber(funnel[key]) ? Number(funnel[key]) : null;
    }

    renderJourney(brief) {
        const funnel = brief.funnel || {};
        const data = FUNNEL_STAGES.map(stage => ({ ...stage, value: this.funnelValue(funnel, stage.key) }));
        const signals = data.find(item => item.key === 'signals')?.value;
        const formatCount = value => this.isKnownNumber(value) ? String(value) : '未知';
        const footer = brief.is_trading_day === false
            ? '休市日不执行扫描和交易，所有阶段均为不适用。'
            : `今日扫描 ${formatCount(funnel.scan_cycles)} 轮；BUY ${formatCount(funnel.buy_signals)}，SELL ${formatCount(funnel.sell_signals)}，HOLD ${formatCount(funnel.observations)}。${signals === 0 ? '当前没有可执行交易信号。' : '交易信号仍需经过计划、风控和执行。'}`;
        this.setText('journey-foot', footer);
        const list = document.getElementById('journey-values');
        if (list) list.innerHTML = data.map(stage => `<li><span>${stage.label}</span><strong>${formatCount(stage.value)}</strong></li>`).join('');
        this.renderFunnel(data, brief.is_trading_day === false);
    }

    renderFunnel(data, isClosedDay) {
        const dom = document.getElementById('journey-funnel');
        if (!dom) return;
        if (!window.echarts) {
            dom.innerHTML = '<div class="empty-state">图表组件不可用，数值见下方说明</div>';
            return;
        }
        const empty = data.every(item => item.value === 0 || item.value === null);
        dom.classList.toggle('is-empty', empty);
        if (empty) { this.funnelChart?.clear(); return; }
        if (!this.funnelChart) this.funnelChart = window.echarts.init(dom, 'alphapilot');
        this.funnelChart.off('click');
        const knownStages = data.filter(item => item.value !== null);
        this.funnelChart.setOption({
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'item',
                backgroundColor: chartColors.surface, borderWidth: 0,
                textStyle: { color: chartColors.ink, fontSize: 12 },
                formatter: params => {
                    const stage = knownStages[params.dataIndex];
                    return `${stage?.label || params.name}：${stage?.value === undefined ? '未知' : `${stage.value} 次`}`;
                },
            },
            series: [{
                type: 'funnel',
                sort: 'descending',
                minSize: '14%',
                maxSize: '100%',
                gap: 4,
                left: 30, right: 30, top: 8, bottom: 8,
                label: { show: true, position: 'inside', formatter: '{b}　{c}', color: '#2f392d', fontSize: 12, fontWeight: 600 },
                labelLine: { show: false },
                itemStyle: { borderColor: 'transparent', opacity: 0.9 },
                emphasis: { label: { fontSize: 12 } },
                data: knownStages.map(item => ({
                    name: item.label,
                    value: item.value,
                    itemStyle: { color: FUNNEL_COLORS[FUNNEL_STAGES.findIndex(stage => stage.key === item.key)] },
                })),
            }],
        }, true);
        setTimeout(() => this.funnelChart?.resize(), 50);
    }

    jumpToResearchLayer(layer) {
        const select = document.getElementById('research-layer');
        if (select) select.value = layer;
        const today = this.app.globalData?.daily_trader?.date;
        if (today) {
            const start = document.getElementById('research-start');
            const end = document.getElementById('research-end');
            if (start) start.value = today;
            if (end) end.value = today;
        }
        this.app.tabs.research.page = 1;
        this.app.switchTab('research');
    }

    renderAudit(brief) {
        const funnel = brief.funnel || {};
        this.setText('audit-blocked', this.text(funnel.blocked, '未知'));
        this.setText('audit-skipped', this.text(funnel.skipped, '未知'));
        this.setText('audit-failed', this.text(funnel.failed, '未知'));
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
            ? `${item.effective_date || '-'} · 每轮买入上限 ${item.params?.top_k ?? '-'} · 最低分 ${item.params?.min_score ?? '-'} · 单票 ${this.isKnownNumber(item.params?.max_weight) ? `${(Number(item.params.max_weight) * 100).toFixed(0)}%` : '-'}`
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
        const totalPnl = this.isKnownNumber(account.total_pnl) ? Number(account.total_pnl)
            : this.isKnownNumber(account.total_assets) && this.isKnownNumber(account.initial_capital)
                ? Number(account.total_assets) - Number(account.initial_capital) : null;
        const positionValue = positions.reduce((sum, item) => sum + Number(item.market_value || 0), 0);
        const cashRatio = Number(account.total_assets || 0) > 0 ? Number(account.cash || 0) / Number(account.total_assets) : 0;
        this.setText('total-assets', this.money(account.total_assets));
        this.setText('total-pnl', `${this.signedMoney(totalPnl)} · ${this.pct(account.total_pnl_pct, 2)}`);
        this.setText('daily-pnl', this.signedMoney(latest.daily_pnl));
        this.setText('available-cash', this.money(account.cash));
        this.setText('cash-ratio', `现金占比 ${(cashRatio * 100).toFixed(1)}%`);
        this.setText('position-count', `${positions.length} 只`);
        this.setText('position-value', `持仓市值 ${this.money(positionValue)}`);
        this.setText('trade-count', `历史成交 ${totalTrades} 笔`);
        const daily = document.getElementById('daily-pnl');
        if (daily) daily.className = this.pnlClass(latest.daily_pnl);
    }

    renderAllocation(account, positions) {
        const available = account?.available !== false && this.isKnownNumber(account?.cash)
            && this.isKnownNumber(account?.total_assets) && Number(account.total_assets) > 0 && positions !== null;
        const cash = available ? Number(account.cash) : null;
        const invested = available ? Number(account.total_assets) - cash : null;
        const ratio = available ? Math.min(100, Math.max(0, invested / Number(account.total_assets) * 100)) : null;
        this.setText('allocation-ratio', ratio === null ? '—' : `${ratio.toFixed(1)}%`);
        this.setText('allocation-cash', this.money(cash));
        this.setText('allocation-invested', this.money(invested));
        const ring = document.getElementById('allocation-ring');
        if (ring) {
            ring.style.setProperty('--invested', `${ratio || 0}%`);
            ring.setAttribute('aria-label', available ? `资金使用率 ${ratio.toFixed(1)}%，现金 ${this.money(cash)}，持仓 ${this.money(invested)}` : '资金分布不可用');
        }
        const fill = document.getElementById('cash-track-fill');
        if (fill) fill.style.width = available ? `${Math.min(100, Math.max(0, cash / Number(account.total_assets) * 100))}%` : '0%';
    }

    // 风险线：优先展示移动止损（更贴近当前行情），否则展示建仓时止损线。
    // 返回 null 表示两条线都不可用，不渲染风险行。
    riskLineCell(label, price, current, kind) {
        if (!this.isKnownNumber(price) || !current) return null;
        const value = Number(price);
        // 止损线在现价下方：距离 = (现价-线)/现价，<=0 表示已跌破；
        // 止盈线在现价上方：距离 = (线-现价)/现价，<=0 表示已达线。
        const distance = kind === 'stop' ? (current - value) / current : (value - current) / current;
        let state = '';
        let note;
        if (kind === 'stop') {
            if (distance <= 0) { state = 'breached'; note = '已触及'; }
            else if (distance <= 0.02) { state = 'near'; note = `距线 ${(distance * 100).toFixed(1)}%`; }
            else note = `距线 ${(distance * 100).toFixed(1)}%`;
        } else {
            if (distance <= 0) { state = 'near'; note = '已达线'; }
            else note = `距线 ${(distance * 100).toFixed(1)}%`;
        }
        return `<div class="${state}"><span>${label}</span><b>${this.money(value, 2)} · ${note}</b></div>`;
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
            const current = Number(pos.current_price || 0);
            const trailing = this.isKnownNumber(pos.trailing_stop_price) ? Number(pos.trailing_stop_price) : null;
            const stopCell = this.riskLineCell(trailing ? '移动止损' : '止损线', trailing ?? pos.stop_loss_price, current, 'stop');
            const takeCell = this.riskLineCell('止盈线', pos.take_profit_price, current, 'take');
            const riskRow = (stopCell || takeCell)
                ? `<div class="position-risk">${stopCell || `<div><span>止损线</span><b>未记录</b></div>`}${takeCell || `<div><span>止盈线</span><b>未记录</b></div>`}</div>`
                : '';
            return `
                <article class="position-card">
                    <div class="position-head"><strong>${this.escape(pos.name || pos.code)}</strong><small>${this.escape(pos.code)}</small></div>
                    <div class="position-numbers">
                        <div><span>持股</span><b>${Number(pos.shares || 0).toLocaleString('zh-CN')} 股</b></div>
                        <div><span>${pos.price_fresh ? '实时价格' : '存储价格 · 非实时'}</span><b>${this.money(pos.current_price, 2)}</b><small>${this.escape(pos.price_as_of || '报价时间未知')}</small></div>
                        <div><span>浮动盈亏</span><b class="${this.pnlClass(pos.pnl)}">${this.signedMoney(pos.pnl)} ${this.pct(pos.pnl_pct, 2)}</b></div>
                    </div>
                    ${riskRow}
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
        if (!this.chart) this.chart = window.echarts.init(dom, 'alphapilot');
        const values = performance.map(item => this.isKnownNumber(item.total_assets) ? Number(item.total_assets) : null);
        const known = values.filter(value => value !== null);
        this.setText('performance-status', known.length === 1 ? '已记录 1 个净值快照，更多交易日后展示完整走势。' : known.length ? `已记录 ${known.length} 个净值快照` : '暂无净值快照，等待账户数据。');
        if (!known.length) {
            this.chart.clear();
            this.chart.setOption({ title: { text: '等待首个净值快照', left: 'center', top: 'middle' } });
            return;
        }
        const firstAssets = values.find(v => v > 0) || 0;
        const hasBenchmark = performance.some(item => this.isKnownNumber(item.benchmark_pnl_pct));
        const benchmarkValues = hasBenchmark && firstAssets > 0
            ? performance.map(item => this.isKnownNumber(item.benchmark_pnl_pct)
                ? firstAssets * (1 + Number(item.benchmark_pnl_pct)) : null)
            : [];
        const series = [
            {
                name: '账户净值',
                type: 'line',
                data: values,
                smooth: false,
                symbol: 'circle', symbolSize: known.length === 1 ? 9 : 5, showSymbol: known.length < 8,
                itemStyle: { color: chartColors.accent, borderColor: '#19201b', borderWidth: 2 },
                lineStyle: { color: chartColors.accent, width: 2 },
                areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: '#c0dfa122' }, { offset: 1, color: '#c0dfa100' }] } },
            },
        ];
        if (benchmarkValues.length) {
            series.push({
                name: '沪深300基准',
                type: 'line',
                data: benchmarkValues,
                smooth: false,
                symbol: 'none',
                lineStyle: { color: chartColors.benchmark, width: 1.5, type: 'dashed' },
            });
        }
        this.chart.setOption({
            backgroundColor: 'transparent',
            legend: hasBenchmark
                ? { data: ['账户净值', '沪深300基准'], textStyle: { color: chartColors.text, fontSize: 10 }, top: 0, right: 0 }
                : undefined,
            tooltip: { trigger: 'axis', backgroundColor: chartColors.surface, borderWidth: 0, textStyle: { color: chartColors.ink }, valueFormatter: value => this.money(value, 2) },
            grid: { left: 0, right: 16, top: hasBenchmark ? 36 : 22, bottom: 16, containLabel: true },
            xAxis: { type: 'category', data: performance.map(item => item.date?.slice(5)), axisLine: { lineStyle: { color: chartColors.line } }, axisLabel: { color: chartColors.text, fontSize: 10 } },
            yAxis: { type: 'value', scale: true, splitNumber: 3,
                min: known.length === 1 ? known[0] * .99 : undefined,
                max: known.length === 1 ? known[0] * 1.01 : undefined,
                axisLabel: { color: chartColors.text, fontSize: 10, formatter: value => `${Number((value / 10000).toFixed(2))}万` }, splitLine: { lineStyle: { color: chartColors.line, type: 'dashed' } } },
            series,
        }, true);
        setTimeout(() => this.chart?.resize(), 50);
    }

    formatTime(value) {
        if (!value || value === '-') return '-';
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? this.text(value) : date.toLocaleString('zh-CN', { hour12: false });
    }
}
