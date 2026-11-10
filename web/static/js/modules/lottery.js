const known = value => typeof value === 'number' && Number.isFinite(value);
const pct = (value, digits = 1) => known(value) ? `${(value * 100).toFixed(digits)}%` : '—';
const signed = (value, digits = 2) => known(value) ? `${value > 0 ? '+' : ''}${value.toFixed(digits)}` : '—';
const line = value => known(value) ? `${value > 0 ? '+' : ''}${value.toFixed(1)}` : '—';
const total = value => known(value) ? value.toFixed(1) : '—';
const LABELS = { sharp: '价格偏离（主信号）', sharp_late: '价格偏离（停售前，影子）', all_games: '全场预测（每场必出）', model_legacy: '模型对照组（已停用）' };
const odds = values => (values || []).map(v => known(v) ? v.toFixed(2) : '—').join('/');
const minutes = value => known(value) ? (value >= 60 ? `${(value / 60).toFixed(1)} 小时` : `${Math.round(value)} 分钟`) : '—';
// 每日比赛页展示的策略：停售前影子只在战绩统计里对比
const DAILY_STRATEGIES = ['sharp', 'all_games', 'model_legacy'];
const RESULT_KIND = { 赢: 'win', 输: 'loss', 走: 'push' };
const MARKETS = ['让分', '大小分'];
const BJ_PARTS = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });

/** 北京时间的 { day: 'YYYY-MM-DD', md: 'MM-DD', hm: 'HH:MM' }；无效时间返回 null。 */
function beijingParts(value) {
    const date = value instanceof Date ? value : new Date(value);
    if (!value || Number.isNaN(date.getTime())) return null;
    const p = Object.fromEntries(BJ_PARTS.formatToParts(date).map(part => [part.type, part.value]));
    return { day: `${p.year}-${p.month}-${p.day}`, md: `${p.month}-${p.day}`, hm: `${p.hour}:${p.minute}` };
}

function edgeText(value) {
    if (!known(value)) return '—';
    const v = Math.abs(value) < 0.05 ? 0 : value;
    return `${v > 0 ? '+' : ''}${v.toFixed(1)} 分`;
}

function el(tagName, className, text) {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

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

/** 体彩四个页签共用一个模块：每日比赛 / 战绩统计 / 历史注单 / 方法与数据。 */
export class LotteryTabs {
    constructor(app) {
        this.app = app;
        this.chart = null;
        this.ledgerOffset = 0;
        this.ledgerLimit = 30;
        this.requestId = 0;
        this.gamesRequestId = 0;
        this.gamesDate = null;          // null = 最新销售日
        this.gamesNav = {};
        this.renderedDate = null;
        this.openDetails = new Set();   // 自动刷新时保留已展开的场次
        const go = day => { this.gamesDate = day; this.loadGames(); };
        document.getElementById('lottery-games-date')?.addEventListener('change', event => { if (event.target.value) go(event.target.value); });
        document.getElementById('lottery-day-prev')?.addEventListener('click', () => { if (this.gamesNav.prev) go(this.gamesNav.prev); });
        document.getElementById('lottery-day-next')?.addEventListener('click', () => { if (this.gamesNav.next) go(this.gamesNav.next); });
        document.getElementById('lottery-day-today')?.addEventListener('click', () => go(beijingParts(new Date()).day));
        document.getElementById('lottery-day-latest')?.addEventListener('click', () => go(null));
        for (const id of ['lottery-ledger-strategy', 'lottery-ledger-market', 'lottery-ledger-single']) {
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
        const today = data.latest_sale_date ? ` · ${data.latest_sale_date} 全场预测 ${latest.all_games ?? 0} 条、主信号研究票 ${latest.sharp ?? 0} 张、停售前影子 ${latest.sharp_late ?? 0} 张` : '';
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
            ['主信号每日上限', `${t.max_picks_per_day ?? '—'} 条腿 × 1 单位（1% 本金）。没开单关的腿合成一张 2 串 1 或 3 串 1`],
            ['未开单关的研究口径', '组合胜率 = 各腿胜率相乘，组合赔率 = 各腿 SP 相乘，组合期望 = 组合胜率 × 组合赔率 − 1。任一腿输，整张票记 -1'],
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

    showDay(day) {
        this.gamesDate = day;
        this.app.switchTab('lottery-games');
    }

    async loadGames() {
        const id = ++this.gamesRequestId;
        const wanted = this.gamesDate;
        const query = wanted ? `?sale_date=${encodeURIComponent(wanted)}` : '';
        if (wanted !== this.renderedDate) this.text('lottery-games-status', '读取中');
        try {
            const data = await getJson(`${this.app.apiBase}/lottery/games${query}`);
            if (id !== this.gamesRequestId) return;
            this.renderGames(data, wanted);
        } catch {
            if (id === this.gamesRequestId) this.text('lottery-games-status', '读取失败，请稍后重试');
        }
    }

    renderGames(data, wanted) {
        if (data.disclaimer) this.text('lottery-daily-disclaimer', data.disclaimer);
        const day = data.sale_date || wanted;
        this.renderedDate = wanted;
        this.gamesNav = { prev: data.prev_sale_date, next: data.next_sale_date };
        const input = document.getElementById('lottery-games-date');
        if (input && day) input.value = day;
        document.getElementById('lottery-day-prev').disabled = !data.prev_sale_date;
        document.getElementById('lottery-day-next').disabled = !data.next_sale_date;
        const latest = document.getElementById('lottery-day-latest');
        if (latest) latest.disabled = !data.latest_sale_date || day === data.latest_sale_date;
        const list = document.getElementById('lottery-games-list');
        const games = data.games || [];
        const groups = games.map(g => this.groupPicks(g.picks || []));
        this.renderDaySummary(games, groups, data.day_summary || []);
        if (!data.available || !games.length) {
            this.showTickets(document.getElementById('lottery-games-parlay-box'), []);
            this.text('lottery-games-status', day ? `销售日 ${day} · 没有场次` : '纸面账本尚未生成');
            const empty = el('div', 'empty-state', day
                ? `${day} 没有竞彩 NBA 场次记录。当天的场次在北京时间 16:00 出单任务运行后出现；没有 NBA 比赛或竞彩未开售的日子不会有记录。`
                : '首个出单任务运行后显示。');
            list?.replaceChildren(empty);
            return;
        }
        const tips = games.map(g => beijingParts(g.tip_at)).filter(Boolean).sort((a, b) => `${a.day}${a.hm}`.localeCompare(`${b.day}${b.hm}`));
        const first = tips[0], last = tips[tips.length - 1];
        const range = first ? `北京时间 ${first.md} ${first.hm}${last && (last.day !== first.day || last.hm !== first.hm) ? ` – ${last.day !== first.day ? `${last.md} ` : ''}${last.hm}` : ''} 开赛` : '';
        this.text('lottery-games-status', `销售日 ${day} · ${games.length} 场${range ? ` · ${range}` : ''}`);
        this.renderDayNote(games);
        this.showTickets(document.getElementById('lottery-games-parlay-box'), data.tickets || data.parlay_recommendations);
        list?.replaceChildren(...games.map((g, index) => this.gameCard(g, groups[index], day, data.thresholds || {})));
    }

    /** 按玩法合并同一选择：主信号与全场预测选同一边时只显示一行。 */
    groupPicks(picks) {
        const out = Object.fromEntries(MARKETS.map(m => [m, []]));
        for (const p of picks) {
            if (!DAILY_STRATEGIES.includes(p.strategy)) continue;
            const list = out[p.market] || (out[p.market] = []);
            const same = list.find(e => e.selection === p.selection && e.odds === p.odds);
            if (same) same.strategies.push(p.strategy);
            else list.push({ ...p, strategies: [p.strategy] });
        }
        for (const list of Object.values(out)) list.sort((a, b) => b.strategies.includes('sharp') - a.strategies.includes('sharp'));
        return out;
    }

    renderDaySummary(games, groups, summary) {
        const finished = games.filter(g => known(g.home_score)).length;
        const live = games.filter(g => g.started && !known(g.home_score)).length;
        this.text('lottery-day-games', games.length ? `${games.length} 场` : '0 场');
        this.text('lottery-day-games-note', games.length ? `已完赛 ${finished} · 进行中 ${live} · 未开赛 ${games.length - finished - live}` : '—');
        const byKey = Object.fromEntries(summary.map(s => [s.key, s]));
        const fill = (key, valueId, noteId, none) => {
            const s = byKey[key];
            if (!s || !s.bets) { this.text(valueId, '0 注'); this.text(noteId, none); return; }
            this.text(valueId, `${s.bets} 注`);
            const record = s.settled ? `${s.wins}-${s.losses}-${s.pushes}（赢-输-走）· 盈亏 ${signed(s.pnl_units)} 注` : '尚未结算';
            this.text(noteId, `${record}${s.settled && s.pending ? ` · 待结算 ${s.pending}` : ''}`);
        };
        fill('sharp', 'lottery-day-sharp', 'lottery-day-sharp-note', games.length ? '当天没有达到门槛的选择' : '—');
        fill('all_games', 'lottery-day-all', 'lottery-day-all-note', games.length ? '当天没有全场预测记录' : '—');
        const entries = groups.flatMap(g => Object.values(g).flat());
        const pending = entries.filter(e => !e.result).length;
        this.text('lottery-day-pending', `${pending} 条`);
        this.text('lottery-day-pending-note', entries.length ? (pending ? '赛后自动补比分和输赢' : '当天选择已全部结算') : '—');
    }

    renderDayNote(games) {
        const note = document.getElementById('lottery-day-note');
        if (!note) return;
        const notes = [];
        if (games.every(g => !g.captured_at)) notes.push('这一天的记录从旧账本导入，没有盘口快照，只显示当时的选择和结果。');
        const listed = games.filter(g => g.single_spread !== null || g.single_total !== null);
        if (listed.length) {
            const spread = listed.filter(g => g.single_spread).length;
            const totals = listed.filter(g => g.single_total).length;
            notes.push(spread || totals
                ? `单关开售：让分 ${spread}/${listed.length} 场，大小分 ${totals}/${listed.length} 场。开了单关的主信号按单场研究；没开的合成 2 串 1 或 3 串 1，胜率、赔率和期望按整张票重算。`
                : '这一天让分、大小分单关都未开售。主信号按 2 串 1 或 3 串 1 研究，胜率、赔率和期望按整张票重算。');
        }
        note.hidden = !notes.length;
        note.textContent = notes.join(' ');
    }

    gameStatus(g) {
        if (known(g.home_score) && known(g.away_score)) return [`已完赛 ${g.away} ${g.away_score} : ${g.home_score} ${g.home}`, 'done'];
        if (g.started) return ['进行中', 'pending'];
        if (g.sale_closed) return ['已停售 · 待开赛', ''];
        return ['销售中', 'pass'];
    }

    gameCard(g, groups, day, thresholds) {
        const card = el('article', 'card lottery-game');
        const head = el('header', 'lottery-game-head');
        const title = el('div', 'lottery-game-title');
        title.append(el('span', 'lottery-matchnum', g.matchnum || '—'), el('strong', '', `${g.away}（客） @ ${g.home}（主）`));
        const tip = beijingParts(g.tip_at), end = beijingParts(g.buy_end);
        const times = el('span', 'lottery-game-times', `开赛 ${tip ? `${tip.md} ${tip.hm}` : '—'} · 竞彩停售 ${end ? `${end.md} ${end.hm}` : '—'}`);
        const [statusText, statusKind] = this.gameStatus(g);
        head.append(title, times, tag(statusText, statusKind));

        const table = el('table', 'lottery-game-table');
        const caption = el('caption', '', `${g.away} @ ${g.home} 盘口与系统选择`);
        const thead = el('thead');
        const headRow = el('tr');
        for (const name of ['玩法', '竞彩（16:00 出单时）', '国际盘', '价格优势', '系统选择', '结果']) {
            const th = el('th', '', name);
            th.scope = 'col';
            headRow.append(th);
        }
        thead.append(headRow);
        const tbody = el('tbody');
        for (const market of MARKETS) {
            const entries = groups[market] || [];
            const jc = this.jcCell(g, market);
            if (!entries.length && !jc) continue;
            const rows = entries.length ? entries : [null];
            rows.forEach((entry, index) => {
                const tr = el('tr');
                if (index === 0) {
                    const span = rows.length;
                    const th = el('th', '', market);
                    th.scope = 'row';
                    th.rowSpan = span;
                    const jcTd = this.stacked(jc || ['未开售']);
                    jcTd.rowSpan = span;
                    const refTd = this.stacked(this.refCell(g, market));
                    refTd.rowSpan = span;
                    tr.append(th, jcTd, refTd);
                }
                if (!entry) {
                    tr.append(this.stacked(['—']), this.stacked(['未出单', '出单时该玩法未开售或已停售']), this.stacked(['—']));
                } else {
                    tr.append(this.stacked([edgeText(entry.edge), known(entry.p_win) ? `估算胜率 ${pct(entry.p_win)} · 期望 ${pct(entry.ev)}` : '无国际盘，无法估算']));
                    const choice = this.stacked([`${entry.selection} @${known(entry.odds) ? entry.odds.toFixed(2) : '—'}`]);
                    const [label, kind] = this.choiceTag(entry, thresholds.min_ev);
                    choice.append(tag(label, kind));
                    if (entry.single_open === true) {
                        choice.append(tag('单关', 'sharp'));
                    } else if (entry.single_open === false) {
                        choice.append(tag('仅串关', 'push'));
                    }
                    tr.append(choice, this.resultCell(entry, g));
                }
                tbody.append(tr);
            });
        }
        if (!tbody.children.length) {
            const tr = el('tr');
            const td = el('td', '', '没有盘口记录');
            td.colSpan = 6;
            tr.append(td);
            tbody.append(tr);
        }
        table.append(caption, thead, tbody);
        const wrap = el('div', 'table-wrap');
        wrap.append(table);

        const key = `${day}|${g.matchnum}|${g.home}`;
        const details = el('details', 'lottery-detail');
        details.open = this.openDetails.has(key);
        details.addEventListener('toggle', () => { if (details.open) this.openDetails.add(key); else this.openDetails.delete(key); });
        const changes = g.injuries?.changes?.length || 0;
        details.append(el('summary', '', `盘口变化（${(g.timeline || []).length} 条） · 伤停${changes ? `（出单后变化 ${changes} 条）` : ''}`));
        details.append(...this.gameDetail(g));
        card.append(head, wrap, details);
        return card;
    }

    stacked(lines) {
        const td = el('td');
        lines.filter(Boolean).forEach((text, index) => td.append(el(index ? 'small' : 'span', index ? 'lottery-sub' : '', text)));
        return td;
    }

    jcCell(g, market) {
        const latest = g.jc_latest;
        const when = latest ? beijingParts(latest.captured_at) : null;
        const label = g.sale_closed ? '停售前最后' : `最新 ${when ? when.hm : ''}`.trim();
        if (market === '让分') {
            if (!known(g.jc_line)) return null;
            const [home, away] = g.jc_odds || [];
            const out = [`${g.home} ${line(g.jc_line)}`, `主 ${odds([home])} / 客 ${odds([away])}`];
            if (latest && known(latest.jc_line) && (latest.jc_line !== g.jc_line || odds(latest.jc_odds) !== odds(g.jc_odds))) {
                out.push(`${label}：${g.home} ${line(latest.jc_line)}（${odds(latest.jc_odds)}）`);
            }
            return out;
        }
        if (!known(g.jc_total)) return null;
        const [over, under] = g.jc_total_odds || [];
        const out = [total(g.jc_total), `大 ${odds([over])} / 小 ${odds([under])}`];
        if (latest && known(latest.jc_total) && (latest.jc_total !== g.jc_total || odds(latest.jc_total_odds) !== odds(g.jc_total_odds))) {
            out.push(`${label}：${total(latest.jc_total)}（${odds(latest.jc_total_odds)}）`);
        }
        return out;
    }

    refCell(g, market) {
        const spread = market === '让分';
        const open = spread ? g.ref_line : g.ref_total;
        const close = spread ? g.close_ref_line : g.close_ref_total;
        const show = value => spread ? `${g.home} ${line(value)}` : total(value);
        if (!known(open)) return [known(close) ? show(close) : '—', known(close) ? '开赛前' : '出单时未挂出'];
        return [show(open), known(close) && close !== open ? `开赛前 ${show(close)}` : null];
    }

    choiceTag(entry, minEv) {
        if (entry.strategies.includes('sharp')) return ['★ 主信号', 'sharp'];
        if (!entry.strategies.includes('all_games')) return [entry.strategy_label || '对照组', ''];
        if (!known(entry.ev)) return ['无国际盘参照 · 仅记录', ''];
        if (entry.ev <= 0) return ['无优势 · 仅记录', ''];
        if (known(minEv) && entry.ev >= minEv) return ['未入选主信号 · 仅记录', ''];
        return ['优势不足 · 仅记录', ''];
    }

    resultCell(entry, g) {
        const td = el('td');
        if (entry.result) {
            td.append(tag(entry.result, RESULT_KIND[entry.result] || ''));
            if (known(entry.pnl)) {
                const pnl = el('span', `lottery-pnl ${entry.pnl > 0 ? 'red-text' : entry.pnl < 0 ? 'green-text' : ''}`, ` ${signed(entry.pnl)} 注`);
                td.append(pnl);
            }
            return td;
        }
        td.append(tag(known(g.home_score) ? '待结算' : g.started ? '进行中' : '待开赛', 'pending'));
        return td;
    }

    gameDetail(g) {
        const box = [];
        const heading = text => el('h4', '', text);
        const note = text => el('p', '', text);

        box.push(heading('盘口变化（北京时间，开赛前）'));
        const timeline = g.timeline || [];
        if (timeline.length) {
            const table = document.createElement('table');
            const head = document.createElement('tr');
            for (const name of ['时间', '记录', `竞彩让分（${g.home}，主/客 SP）`, `国际让分（${g.home}）`, '竞彩总分（大/小 SP）', '国际总分']) {
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
            const wrap = el('div', 'table-wrap');
            wrap.append(table);
            box.push(wrap, note('相同盘口的连续记录已合并。竞彩停售后只记录国际盘，竞彩一栏为空。'));
        } else {
            box.push(note('暂无盘口快照'));
        }

        box.push(heading('伤停'));
        const inj = g.injuries;
        if (!inj) {
            box.push(note('这场比赛没有伤停记录（伤停名单上线前的场次没有数据）。'));
        } else {
            const roster = (team, rows) => {
                if (rows === null || rows === undefined) return `${team}：出单时尚未记录`;
                if (!rows.length) return `${team}：无伤停`;
                return `${team}：${rows.map(r => `${r.player} ${r.status}${r.type ? `（${r.type}）` : ''}`).join('、')}`;
            };
            box.push(note(`出单时 · ${roster(g.away, inj.away)}`), note(`出单时 · ${roster(g.home, inj.home)}`));
            if (inj.changes?.length) {
                for (const c of inj.changes) box.push(note(`${beijingTime(c.at)} · ${c.team} ${c.player}：${c.from} → ${c.to}`));
            } else {
                box.push(note('出单后到开赛前名单无变化'));
            }
        }
        return box;
    }

    async loadLedger() {
        const params = new URLSearchParams({ limit: this.ledgerLimit, offset: this.ledgerOffset });
        const strategy = document.getElementById('lottery-ledger-strategy')?.value;
        const market = document.getElementById('lottery-ledger-market')?.value;
        const single = document.getElementById('lottery-ledger-single')?.value;
        if (strategy) params.set('strategy', strategy);
        if (market) params.set('market', market);
        if (single) params.set('single_open', single);
        const body = document.getElementById('lottery-ledger-rows');
        try {
            const data = await getJson(`${this.app.apiBase}/lottery/ledger?${params}`);
            body?.replaceChildren();
            const end = Math.min(data.total, this.ledgerOffset + data.items.length);
            this.text('lottery-ledger-page', data.total ? `${this.ledgerOffset + 1}–${end} / ${data.total}` : '暂无记录');
            document.getElementById('lottery-ledger-prev').disabled = this.ledgerOffset === 0;
            document.getElementById('lottery-ledger-next').disabled = end >= data.total;
            this.showTickets(document.getElementById('lottery-parlay-box'), data.tickets || data.parlay_recommendations);
            for (const p of data.items) {
                const row = document.createElement('tr');
                const dayCell = cell(row, '');
                const link = el('button', 'lottery-link', p.sale_date);
                link.type = 'button';
                link.title = '查看当天的每日比赛';
                link.addEventListener('click', () => this.showDay(p.sale_date));
                dayCell.append(link);
                cell(row, p.matchnum);
                cell(row, p.matchup);
                cell(row, p.strategy_label);
                cell(row, `${p.market} ${p.selection}`);
                const singleCell = cell(row, '');
                if (p.single_open === true) {
                    singleCell.append(tag('单关', 'sharp'));
                } else if (p.single_open === false) {
                    singleCell.append(tag('串关', 'push'));
                } else {
                    singleCell.textContent = '—';
                }
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


    showTickets(container, tickets) {
        if (!container) return;
        if (!tickets || !tickets.length) {
            container.style.display = 'none';
            container.replaceChildren();
            return;
        }
        container.style.display = 'block';
        this.renderParlays(container, tickets);
    }

    renderParlays(container, tickets) {
        container.replaceChildren();
        const head = el('div', 'section-heading compact');
        const titleDiv = el('div');
        titleDiv.append(el('span', 'eyebrow', '研究票'));
        titleDiv.append(el('h3', '', '按能否单关研究'));
        head.append(titleDiv);
        container.append(head);
        container.append(el('p', 'lottery-ticket-note', '开了单关的主信号单独成票。没开单关的合成 2 串 1 或 3 串 1：组合胜率、赔率、期望和盈亏都按整张票重算。标成备选的票不要和推荐票一起计入。'));
        const list = el('div', 'parlay-list');
        for (const item of tickets) list.append(this.ticketCard(item));
        container.append(list);
    }

    ticketCard(item) {
        const card = el('article', 'lottery-ticket');
        const head = el('div', 'lottery-ticket-head');
        head.append(tag(item.type, item.type === '无法出票' ? 'stop' : 'sharp'));
        head.append(el('strong', '', item.label || '—'));
        if (item.role === '备选') head.append(tag('备选', 'pass'));
        else if (item.role === '研究' && item.type !== '无法出票') head.append(tag('研究', 'sharp'));
        if (item.result) head.append(tag(item.result, RESULT_KIND[item.result] || ''));
        card.append(head);
        for (const leg of item.legs || []) {
            const own = known(leg.ev) ? ` · 本腿期望 ${leg.ev > 0 ? '+' : ''}${pct(leg.ev)}` : '';
            const mark = leg.companion ? '搭配腿' : leg.market || '腿';
            card.append(el('p', '', `${mark} ${leg.selection || '—'} @${known(leg.odds) ? leg.odds.toFixed(2) : '—'} · 胜率 ${pct(leg.p_win)}${own}`));
        }
        if (known(item.odds)) {
            const metrics = el('p', 'lottery-ticket-metrics', '');
            metrics.append(el('span', '', `组合赔率 ${item.odds.toFixed(2)}`));
            metrics.append(el('span', '', `组合胜率 ${pct(item.p_win)}`));
            metrics.append(el('span', '', `保本 ${pct(item.breakeven)}`));
            const ev = el('span', '', `组合期望 ${known(item.ev) ? `${item.ev > 0 ? '+' : ''}${pct(item.ev)}` : '—'}`);
            metrics.append(ev);
            if (known(item.pnl)) metrics.append(el('span', '', `盈亏 ${signed(item.pnl)} 注`));
            card.append(metrics);
        }
        if (item.note) card.append(el('p', '', item.note));
        return card;
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
