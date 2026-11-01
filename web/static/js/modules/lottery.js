const known = value => typeof value === 'number' && Number.isFinite(value);
const pct = (value, digits = 1) => known(value) ? `${(value * 100).toFixed(digits)}%` : '—';
const signed = (value, digits = 2) => known(value) ? `${value > 0 ? '+' : ''}${value.toFixed(digits)}` : '—';
const line = value => known(value) ? `${value > 0 ? '+' : ''}${value.toFixed(1)}` : '—';
const total = value => known(value) ? value.toFixed(1) : '—';
const LABELS = { sharp: '价格偏离（主信号）', sharp_late: '价格偏离（停售前，影子）', all_games: '全场预测（每场必出）', model_legacy: '模型对照组（已停用）' };
const odds = values => (values || []).map(v => known(v) ? v.toFixed(2) : '—').join('/');
const minutes = value => known(value) ? (value >= 60 ? `${(value / 60).toFixed(1)} 小时` : `${Math.round(value)} 分钟`) : '—';

function cell(row, text, className) {
    const td = document.createElement('td');
    td.textContent = text ?? '—';
    if (className) td.className = className;
    row.append(td);
    return td;
}

function tag(text, kind) {
    const span = document.createElement('span');
    span.className = `lottery-tag ${kind || ''}`;
    span.textContent = text;
    return span;
}

function beijingTime(value) {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

async function getJson(url) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!data.success) throw new Error('unavailable');
    return data;
}

/** 体彩四个页签共用一个模块：总览 / 场次盘口 / 验证账本 / 方法与数据。 */
export class LotteryTabs {
    constructor(app) {
        this.app = app;
        this.chart = null;
        this.ledgerOffset = 0;
        this.ledgerLimit = 30;
        this.requestId = 0;
        document.getElementById('lottery-games-load')?.addEventListener('click', () => this.loadGames());
        for (const id of ['lottery-ledger-strategy', 'lottery-ledger-market']) {
            document.getElementById(id)?.addEventListener('change', () => { this.ledgerOffset = 0; this.loadLedger(); });
        }
        document.getElementById('lottery-ledger-prev')?.addEventListener('click', () => { this.ledgerOffset = Math.max(0, this.ledgerOffset - this.ledgerLimit); this.loadLedger(); });
        document.getElementById('lottery-ledger-next')?.addEventListener('click', () => { this.ledgerOffset += this.ledgerLimit; this.loadLedger(); });
        window.addEventListener('resize', () => this.chart?.resize());
    }

    view(name) {
        const loaders = { lottery: () => this.loadSummary(), 'lottery-games': () => this.loadGames(), 'lottery-ledger': () => this.loadLedger(), 'lottery-method': () => this.loadHealth() };
        return { load: loaders[name] };
    }

    text(id, value) { this.app.setText(id, value); }

    async loadSummary() {
        const id = ++this.requestId;
        try {
            const data = await getJson(`${this.app.apiBase}/lottery/summary`);
            if (id !== this.requestId) return;
            if (!data.available) { this.text('lottery-status', '纸面账本尚未生成，首个出单任务运行后显示。'); return; }
            this.renderSummary(data);
        } catch {
            if (id === this.requestId) this.text('lottery-status', '体彩数据暂时不可用，请稍后刷新。');
        }
    }

    renderSummary(data) {
        if (data.disclaimer) this.text('lottery-disclaimer', data.disclaimer);
        const sharp = (data.strategies || []).find(item => item.key === 'sharp') || { overall: {} };
        const o = sharp.overall || {};
        const latest = data.latest_day_picks || {};
        const today = data.latest_sale_date ? ` · ${data.latest_sale_date} 全场预测 ${latest.all_games ?? 0} 条、主信号 ${latest.sharp ?? 0} 注、停售前影子 ${latest.sharp_late ?? 0} 注` : '';
        this.text('lottery-status', `价格偏离信号的纸面记录${today}`);
        this.text('lottery-settled', o.settled ?? 0);
        this.text('lottery-pending', `待结算 ${o.pending ?? 0} 注`);
        this.text('lottery-hit', pct(o.hit_rate));
        const ci = o.hit_rate_ci95;
        this.text('lottery-hit-ci', ci ? `95% 区间 ${pct(ci[0])} – ${pct(ci[1])} · 保本约 ${pct(o.breakeven_hit_rate)}` : '样本不足');
        this.text('lottery-pnl', known(o.pnl_units) ? `${signed(o.pnl_units)} 注` : '—');
        this.text('lottery-roi', `收益率 ${pct(o.roi)}`);
        this.text('lottery-clv', known(o.avg_clv_points) ? `${signed(o.avg_clv_points, 2)} 分` : '—');
        this.text('lottery-clv-note', o.clv_samples ? `${o.clv_samples} 注有收盘线 · ${pct(o.positive_clv_ratio, 0)} 为正` : '相对开赛前国际线，正数=买得更便宜');
        this.renderStops(data.stop_rules || {});
        this.renderThresholds(data.thresholds || {});
        this.renderStrategyRows(data.strategies || []);
        this.renderInjuryGroups(data.injury_groups || []);
        this.renderCurve(data.curve || []);
    }

    renderInjuryGroups(groups) {
        const body = document.getElementById('lottery-injury-rows');
        if (!body) return;
        body.replaceChildren();
        for (const strategy of groups) {
            for (const group of strategy.groups || []) {
                for (const [label, s] of [[`有「${group.tag}」`, group.with], [`无「${group.tag}」`, group.without]]) {
                    const row = document.createElement('tr');
                    cell(row, strategy.label);
                    cell(row, label);
                    cell(row, `${s.settled ?? 0}${s.pending ? ` (+${s.pending}待结算)` : ''}`);
                    cell(row, pct(s.hit_rate));
                    cell(row, pct(s.roi));
                    cell(row, known(s.avg_clv_points) ? `${signed(s.avg_clv_points)} 分` : '—');
                    body.append(row);
                }
            }
        }
        if (!body.children.length) {
            const row = document.createElement('tr');
            const td = cell(row, '伤停标签从本次更新起随出单记录，尚无带标签的注单');
            td.colSpan = 6;
            body.append(row);
        }
    }

    renderStops(stop) {
        const list = document.getElementById('lottery-stop-list');
        if (!list) return;
        list.replaceChildren();
        const items = [...(stop.checks || [])];
        items.push({ label: `任一时点回撤达到 ${stop.drawdown_limit_units ?? 20} 注（本金 20%）→ 停`, status: stop.drawdown_triggered ? 'stop' : 'pass', progress: stop.max_drawdown_units, at: stop.drawdown_limit_units, drawdown: true });
        for (const check of items) {
            const box = document.createElement('div');
            box.className = 'lottery-stop';
            const head = document.createElement('header');
            const title = document.createElement('strong');
            title.textContent = check.label;
            const status = { pending: ['未到评估点', 'pending'], pass: ['未触发', 'pass'], stop: ['已触发 → 停止', 'stop'] }[check.status] || ['—', ''];
            head.append(title, tag(status[0], status[1]));
            const bar = document.createElement('div');
            bar.className = 'lottery-progress';
            const fill = document.createElement('span');
            const ratio = known(check.progress) && check.at ? Math.min(1, check.progress / check.at) : 0;
            fill.style.width = `${(ratio * 100).toFixed(0)}%`;
            bar.append(fill);
            const note = document.createElement('p');
            note.textContent = check.drawdown ? `当前最大回撤 ${signed(check.progress || 0)} 注` : `进度 ${check.progress ?? 0} / ${check.at} 注${known(check.value) ? ` · 评估值 ${pct(check.value)}` : ''}`;
            box.append(head, bar, note);
            list.append(box);
        }
    }

    renderThresholds(t) {
        const list = document.getElementById('lottery-thresholds');
        if (!list) return;
        list.replaceChildren();
        const facts = [
            ['期望值门槛', `≥ ${pct(t.min_ev, 0)}（按竞彩实际赔率与历史点值换算胜率）`],
            ['1.70 赔率下所需偏离', `让分 ≥ ${t.required_edge_at_1_70?.['让分'] ?? '—'} 分 · 大小分 ≥ ${t.required_edge_at_1_70?.['大小分'] ?? '—'} 分`],
            ['1.70 赔率保本命中率', pct(t.breakeven_at_1_70)],
            ['主信号每日上限', `${t.max_picks_per_day ?? '—'} 注 × 1 单位（1% 本金）`],
            ['全场预测', '每场让分、大小分各选期望值更高的一侧，不设门槛，单独统计'],
            ['停售前影子', `与主信号同一规则，在竞彩停售前 ${t.late_window_minutes ?? 45} 分钟内的最后一次跟踪时出单，单独统计，用来比较出单时点`],
            ['记录时点', '主信号与全场预测在北京时间 16:00 出单；之后每 30 分钟跟踪竞彩到停售。出单即公开，之后不改写'],
            ['伤停标签', '出单时记录双方伤停名单与标签，只用于赛后分组对照，不参与选单'],
        ];
        for (const [name, value] of facts) {
            const item = document.createElement('div');
            const strong = document.createElement('strong');
            strong.textContent = name;
            const p = document.createElement('p');
            p.textContent = value;
            item.append(strong, p);
            list.append(item);
        }
    }

    renderStrategyRows(strategies) {
        const body = document.getElementById('lottery-strategy-rows');
        if (!body) return;
        body.replaceChildren();
        for (const strategy of strategies) {
            for (const [market, s] of [['合计', strategy.overall], ...Object.entries(strategy.by_market || {})]) {
                if (!s || (!s.bets && market !== '合计')) continue;
                const row = document.createElement('tr');
                cell(row, strategy.label);
                cell(row, market);
                cell(row, `${s.settled ?? 0}${s.pending ? ` (+${s.pending}待结算)` : ''}`);
                cell(row, `${s.wins ?? 0}-${s.losses ?? 0}-${s.pushes ?? 0}`);
                cell(row, pct(s.hit_rate));
                cell(row, pct(s.breakeven_hit_rate));
                cell(row, known(s.pnl_units) ? signed(s.pnl_units) : '—', s.pnl_units > 0 ? 'red-text' : s.pnl_units < 0 ? 'green-text' : '');
                cell(row, pct(s.roi));
                cell(row, known(s.avg_clv_points) ? `${signed(s.avg_clv_points)} 分` : '—');
                body.append(row);
            }
        }
        if (!body.children.length) {
            const row = document.createElement('tr');
            const td = cell(row, '暂无已公开的纸面注单');
            td.colSpan = 9;
            body.append(row);
        }
    }

    renderCurve(points) {
        const container = document.getElementById('lottery-curve');
        if (!points.length) { container?.classList.add('is-empty'); this.text('lottery-curve-status', '暂无已结算注单'); return; }
        if (!window.echarts || !container) { this.text('lottery-curve-status', '图表组件不可用，请看对照表'); return; }
        container.classList.remove('is-empty');
        this.text('lottery-curve-status', '按销售日累计纸面盈亏；未结算的注单不计入。');
        if (!this.chart) this.chart = window.echarts.init(container, 'alphapilot');
        const keys = Object.keys(LABELS).filter(key => points.some(p => known(p[key])));
        let last = {};
        this.chart.setOption({
            tooltip: { trigger: 'axis', renderMode: 'richText' },
            legend: { data: keys.map(key => LABELS[key]) },
            grid: { left: 50, right: 20, top: 46, bottom: 40 },
            xAxis: { type: 'category', data: points.map(p => p.date) },
            yAxis: { type: 'value', name: '注' },
            series: keys.map(key => ({ name: LABELS[key], type: 'line', step: 'end', data: points.map(p => { if (known(p[key])) last[key] = p[key]; return last[key] ?? null; }) })),
        }, true);
        this.chart.resize();
    }

    async loadGames() {
        const input = document.getElementById('lottery-games-date');
        const query = input?.value ? `?sale_date=${encodeURIComponent(input.value)}` : '';
        const body = document.getElementById('lottery-games-rows');
        this.text('lottery-games-status', '读取中');
        try {
            const data = await getJson(`${this.app.apiBase}/lottery/games${query}`);
            body?.replaceChildren();
            if (!data.available || !data.games.length) { this.text('lottery-games-status', '该销售日没有竞彩 NBA 场次记录'); return; }
            if (input && !input.value) input.value = data.sale_date;
            this.text('lottery-games-status', `销售日 ${data.sale_date} · ${data.games.length} 场`);
            for (const g of data.games) {
                const row = document.createElement('tr');
                cell(row, g.matchnum);
                cell(row, `${g.away} @ ${g.home}`);
                cell(row, beijingTime(g.tip_at));
                cell(row, beijingTime(g.buy_end));
                const latest = g.jc_latest;
                const spreadMoved = latest && (latest.jc_line !== g.jc_line || odds(latest.jc_odds) !== odds(g.jc_odds));
                const totalMoved = latest && (latest.jc_total !== g.jc_total || odds(latest.jc_total_odds) !== odds(g.jc_total_odds));
                cell(row, `${line(g.jc_line)} (${odds(g.jc_odds)})${spreadMoved ? ` → 现 ${line(latest.jc_line)} (${odds(latest.jc_odds)})` : ''}`);
                cell(row, `${line(g.ref_line)}${known(g.close_ref_line) ? ` → 收盘 ${line(g.close_ref_line)}` : ''}`);
                cell(row, `${total(g.jc_total)} (${odds(g.jc_total_odds)})${totalMoved ? ` → 现 ${total(latest.jc_total)} (${odds(latest.jc_total_odds)})` : ''}`);
                cell(row, `${total(g.ref_total)}${known(g.close_ref_total) ? ` → 收盘 ${total(g.close_ref_total)}` : ''}`);
                cell(row, `让分${g.single_spread ? '开' : '未开'} · 总分${g.single_total ? '开' : '未开'}`);
                cell(row, g.score || (g.started ? '进行中' : '未开赛'));
                const analysis = cell(row, '', 'analysis');
                const parts = (g.analysis || []).map(a => `${a.market} 有利 ${known(a.edge) ? `${a.edge.toFixed(1)} 分` : '—'} · 估算胜率 ${pct(a.p_win)} · 期望 ${pct(a.ev)}`);
                const picks = (g.picks || []).map(p => `${p.strategy_label}：${p.selection} @${p.odds.toFixed(2)}${p.result ? ` ${p.result}` : ''}`);
                analysis.textContent = [...parts, ...picks].join('；') || '无可比盘口';
                const toggle = document.createElement('button');
                toggle.type = 'button';
                toggle.className = 'lottery-toggle';
                toggle.textContent = '展开';
                toggle.setAttribute('aria-expanded', 'false');
                cell(row, '').append(toggle);
                const detail = this.gameDetail(g);
                toggle.addEventListener('click', () => {
                    const open = detail.hidden;
                    detail.hidden = !open;
                    toggle.textContent = open ? '收起' : '展开';
                    toggle.setAttribute('aria-expanded', String(open));
                });
                body?.append(row, detail);
            }
        } catch {
            this.text('lottery-games-status', '读取失败，请稍后重试');
        }
    }

    gameDetail(g) {
        const row = document.createElement('tr');
        row.className = 'lottery-detail-row';
        row.hidden = true;
        const td = document.createElement('td');
        td.colSpan = 12;
        const box = document.createElement('div');
        box.className = 'lottery-detail';
        const heading = text => { const h = document.createElement('h4'); h.textContent = text; return h; };
        const note = text => { const p = document.createElement('p'); p.textContent = text; return p; };

        box.append(heading('盘口时间线（北京时间，开赛前）'));
        const timeline = g.timeline || [];
        if (timeline.length) {
            const table = document.createElement('table');
            const head = document.createElement('tr');
            for (const name of ['时间', '记录', '竞彩主队让分（主/客 SP）', '国际主队让分', '竞彩总分（大/小 SP）', '国际总分']) {
                const th = document.createElement('th');
                th.scope = 'col';
                th.textContent = name;
                head.append(th);
            }
            const thead = document.createElement('thead');
            thead.append(head);
            const tbody = document.createElement('tbody');
            for (const s of timeline) {
                const tr = document.createElement('tr');
                cell(tr, beijingTime(s.at));
                cell(tr, s.source);
                cell(tr, known(s.jc_line) ? `${line(s.jc_line)} (${odds([s.jc_sp_home, s.jc_sp_away])})` : '—');
                cell(tr, line(s.ref_line));
                cell(tr, known(s.jc_total) ? `${total(s.jc_total)} (${odds([s.jc_sp_over, s.jc_sp_under])})` : '—');
                cell(tr, total(s.ref_total));
                tbody.append(tr);
            }
            table.append(thead, tbody);
            box.append(table, note('相同盘口的连续记录已合并。竞彩停售后只采集国际线，竞彩一栏为空。'));
        } else {
            box.append(note('暂无快照'));
        }

        box.append(heading('伤停'));
        const inj = g.injuries;
        if (!inj) {
            box.append(note('伤停名单从本次更新起记录，此前的场次没有数据。'));
        } else {
            const roster = (team, rows) => {
                if (rows === null || rows === undefined) return `${team}：出单时尚未记录`;
                if (!rows.length) return `${team}：无伤停`;
                return `${team}：${rows.map(r => `${r.player} ${r.status}${r.type ? `（${r.type}）` : ''}`).join('、')}`;
            };
            box.append(note(`出单时 · ${roster(g.away, inj.away)}`), note(`出单时 · ${roster(g.home, inj.home)}`));
            if (inj.changes?.length) {
                for (const c of inj.changes) box.append(note(`${beijingTime(c.at)} · ${c.team} ${c.player}：${c.from} → ${c.to}`));
            } else {
                box.append(note('出单后到开赛前名单无变化'));
            }
        }
        td.append(box);
        row.append(td);
        return row;
    }

    async loadLedger() {
        const params = new URLSearchParams({ limit: this.ledgerLimit, offset: this.ledgerOffset });
        const strategy = document.getElementById('lottery-ledger-strategy')?.value;
        const market = document.getElementById('lottery-ledger-market')?.value;
        if (strategy) params.set('strategy', strategy);
        if (market) params.set('market', market);
        const body = document.getElementById('lottery-ledger-rows');
        try {
            const data = await getJson(`${this.app.apiBase}/lottery/ledger?${params}`);
            body?.replaceChildren();
            const end = Math.min(data.total, this.ledgerOffset + data.items.length);
            this.text('lottery-ledger-page', data.total ? `${this.ledgerOffset + 1}–${end} / ${data.total}` : '暂无记录');
            document.getElementById('lottery-ledger-prev').disabled = this.ledgerOffset === 0;
            document.getElementById('lottery-ledger-next').disabled = end >= data.total;
            for (const p of data.items) {
                const row = document.createElement('tr');
                cell(row, p.sale_date);
                cell(row, p.matchnum);
                cell(row, p.matchup);
                cell(row, p.strategy_label);
                cell(row, `${p.market} ${p.selection}`);
                cell(row, known(p.odds) ? p.odds.toFixed(2) : '—');
                cell(row, known(p.edge) ? `${p.edge.toFixed(1)} 分` : '—');
                cell(row, pct(p.p_win));
                cell(row, pct(p.ev));
                cell(row, p.score || '—');
                const result = cell(row, '');
                result.append(tag(p.result || '待结算', p.result === '赢' ? 'win' : p.result === '输' ? 'loss' : 'pending'));
                cell(row, known(p.pnl) ? signed(p.pnl) : '—');
                cell(row, known(p.clv) ? `${signed(p.clv, 1)} 分` : '—');
                cell(row, p.injury_tags === null || p.injury_tags === undefined ? '未记录' : (p.injury_tags.join('、') || '无'));
                body?.append(row);
            }
        } catch {
            this.text('lottery-ledger-page', '读取失败，请稍后重试');
        }
    }

    async loadHealth() {
        const list = document.getElementById('lottery-health-list');
        try {
            const data = await getJson(`${this.app.apiBase}/lottery/health`);
            list?.replaceChildren();
            if (!data.available) { list.textContent = '体彩任务尚未运行'; return; }
            const names = { picks: '出单任务（每天 16:00）', capture: '竞彩跟踪、收盘线与比分采集（每 30 分钟）' };
            const facts = Object.entries(data.jobs || {}).map(([job, info]) => [names[job] || job,
                `最近运行 ${beijingTime(info.last_started_at)} · ${info.last_status === 'ok' ? '成功' : info.last_status || '未运行'} · 近 7 天失败 ${info.failures_7d} 次`]);
            facts.push(['最新盘口快照', beijingTime(data.latest_snapshot_at)]);
            const s = data.single_bet_7d || {};
            facts.push(['近 7 天单关开售', s.snapshots ? `让分 ${s.spread_open}/${s.snapshots} · 大小分 ${s.total_open}/${s.snapshots}` : '暂无快照']);
            const c = data.reference_coverage_7d || {};
            facts.push(['国际盘口覆盖', c.snapshots ? `${c.with_reference}/${c.snapshots} 场有国际让分` : '暂无快照']);
            for (const [market, m] of Object.entries(data.jc_movement_30d || {})) {
                if (!m.games) { facts.push([`近 30 天竞彩${market}调线`, '跟踪样本不足（16:00 之后至少再抓到一次才计入）']); continue; }
                const dev = m.deviation_pairs ? ` · 与国际线偏离均值 ${m.avg_deviation_first.toFixed(2)} → ${m.avg_deviation_last.toFixed(2)} 分（缩小 ${m.deviation_narrowed} 场、扩大 ${m.deviation_widened} 场）` : '';
                facts.push([`近 30 天竞彩${market}调线`, `${m.games} 场中线变动 ${m.line_changed} 场、SP 变动 ${m.odds_changed} 场 · 首次变线中位 ${minutes(m.median_minutes_to_change)}${dev}`]);
            }
            const inj = data.injuries || {};
            facts.push(['伤停名单记录', inj.reports ? `${inj.reports} 条球队名单变化 · 最近 ${beijingTime(inj.latest_change_at)}` : '尚未记录']);
            for (const source of data.sources || []) facts.push([source.name, source.detail]);
            for (const [name, value] of facts) {
                const item = document.createElement('div');
                const strong = document.createElement('strong');
                strong.textContent = name;
                const p = document.createElement('p');
                p.textContent = value;
                item.append(strong, p);
                list?.append(item);
            }
        } catch {
            if (list) list.textContent = '读取失败，请稍后重试';
        }
    }
}
