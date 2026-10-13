(() => {
    'use strict';

    const API = '/api/workspace';
    const VIEWS = {
        overview: ['账户总览', '让每次交易和每个策略，都有据可循。'],
        performance: ['收益表现', '从真实净值、回撤与基准比较中复盘账户表现。'],
        trading: ['模拟交易', '观察行情，用模拟资金验证你的交易想法。'],
        strategies: ['我的策略', '设置执行边界，追踪每次自动模拟交易。'],
        billing: ['套餐与用量', '查看你的账户额度与产品服务规划。'],
        security: ['账号安全', '验证邮箱、管理密码与登录会话。'],
    };
    const MARKETS = { CN: ['A 股', 'CNY'], US: ['美股', 'USD'], CRYPTO: ['数字资产', 'USD'] };
    const state = {
        user: null, csrf: '', limits: { accounts: 3 }, accounts: [], markets: [],
        market: 'CN', account: null, detail: null, instrumentId: '', quote: null,
        quoteError: '', quoteLoading: false, quoteFetchedAt: 0, quoteSequence: 0,
        selectionSequence: 0, currentView: 'overview', authMode: 'login',
        refreshing: false, mutation: false, strategyDirty: false, pendingOrder: null,
        theme: 'system', timer: null, requests: new Set(),
        capabilities: null, recoveryMode: '', mailAction: null, securityLoading: false, securityRefreshRequested: false,
        performance: null, performanceLoading: false, performanceSequence: 0, performanceOffset: 0,
        charts: new Map(), sessionBusy: false, exportBusy: false,
    };

    // Keep one-use email tokens only in memory and remove them from the address before any API call.
    function captureMailAction() {
        const fragment = location.hash.slice(1);
        const match = /^(verify-email|reset-password)=([^&]*)$/.exec(fragment);
        if (!match) return false;
        history.replaceState(null, '', `${location.pathname}${location.search}`);
        let token = '';
        try { token = decodeURIComponent(match[2]); } catch (_) { /* Invalid tokens are reported by the confirmation form. */ }
        state.mailAction = { kind: match[1] === 'verify-email' ? 'verify' : 'reset', token: token.length <= 2048 ? token : '' };
        return true;
    }
    captureMailAction();

    const byId = id => document.getElementById(id);
    const all = selector => Array.from(document.querySelectorAll(selector));
    const setText = (id, value) => { byId(id).textContent = value == null ? '—' : String(value); };
    const number = value => {
        if (value == null || value === '' || typeof value === 'boolean') return null;
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : null;
    };
    const formatNumber = (value, digits = 2) => {
        const parsed = number(value);
        return parsed == null ? '—' : new Intl.NumberFormat('zh-CN', {
            minimumFractionDigits: digits, maximumFractionDigits: digits,
        }).format(parsed);
    };
    const formatQuantity = value => {
        const parsed = number(value);
        return parsed == null ? '—' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 8 }).format(parsed);
    };
    const currency = () => state.account?.currency || MARKETS[state.market][1];
    const money = (value, unit = currency()) => number(value) == null ? '—' : `${formatNumber(value)} ${unit}`;
    const dateTime = value => {
        if (value == null || value === '') return '尚无记录';
        const normalized = typeof value === 'number' && value < 1e12 ? value * 1000 : value;
        const date = new Date(normalized);
        if (Number.isNaN(date.getTime())) return '时间不可用';
        return new Intl.DateTimeFormat('zh-CN', {
            timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
        }).format(date);
    };

    function notice(id, message = '', kind = '') {
        const element = byId(id);
        element.textContent = message;
        element.hidden = !message;
        element.className = `${element.classList.contains('inline-status') ? 'inline-status' : 'notice'}${kind ? ` ${kind}` : ''}`;
    }

    function signed(id, value) {
        const parsed = number(value);
        const element = byId(id);
        element.textContent = parsed == null ? '—' : `${parsed > 0 ? '+' : ''}${formatNumber(parsed)}`;
        element.classList.toggle('positive', parsed > 0);
        element.classList.toggle('negative', parsed < 0);
    }

    function statusPill(id, text, kind = '') {
        const element = byId(id);
        element.textContent = text;
        element.className = `status-pill${kind ? ` ${kind}` : ''}`;
    }

    function icon(name) {
        const element = document.createElement('i');
        element.setAttribute('data-lucide', name);
        element.setAttribute('aria-hidden', 'true');
        return element;
    }

    function renderIcons() {
        if (window.lucide) window.lucide.createIcons();
    }

    function buttonText(id, value) {
        const button = byId(id);
        const label = button.querySelector('span');
        if (label) label.textContent = value;
        else button.textContent = value;
    }

    function setBusy(id, busy) {
        const element = byId(id);
        element.disabled = busy;
        element.setAttribute('aria-busy', String(busy));
    }

    function errorText(data, fallback) {
        const detail = data?.detail ?? data?.error ?? data?.message;
        if (typeof detail === 'string') return detail;
        if (detail && typeof detail.message === 'string') return detail.message;
        if (Array.isArray(detail)) return '提交内容不符合要求，请检查输入后重试。';
        return fallback;
    }

    async function request(path, options = {}) {
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 20000);
        state.requests.add(controller);
        const method = options.method || 'GET';
        const headers = { Accept: options.blob ? 'text/csv' : 'application/json' };
        if (options.body !== undefined) headers['Content-Type'] = 'application/json';
        if (method !== 'GET' && state.user && !options.auth) headers['X-CSRF-Token'] = state.csrf;
        try {
            const response = await fetch(`${API}${path}`, {
                method, headers, credentials: 'same-origin', cache: 'no-store',
                body: options.body === undefined ? undefined : JSON.stringify(options.body),
                signal: controller.signal,
            });
            if (response.status === 204) return {};
            if (response.ok && options.blob) return await response.blob();
            let data = null;
            try { data = await response.json(); } catch (_) { /* Some gateways return a non-JSON error. */ }
            if (!response.ok) {
                const error = new Error(errorText(data, response.status === 429
                    ? '请求过于频繁，请稍后重试。' : '服务暂时不可用，请稍后重试。'));
                error.status = response.status;
                error.code = data?.code || data?.detail?.code;
                if (response.status === 401 && state.user && !options.auth) {
                    clearSession('登录已过期，请重新登录。');
                }
                throw error;
            }
            if (!data || typeof data !== 'object') throw new Error('服务返回了无效数据，请稍后重试。');
            return data;
        } catch (error) {
            if (error.name === 'AbortError') throw new Error('请求超时或已中止，请检查连接后重试。');
            if (error instanceof TypeError) throw new Error('无法连接服务，请检查网络后重试。');
            throw error;
        } finally {
            window.clearTimeout(timeout);
            state.requests.delete(controller);
        }
    }

    function applyTheme(preference) {
        state.theme = ['light', 'dark', 'system'].includes(preference) ? preference : 'system';
        const dark = state.theme === 'dark' || (state.theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
        document.documentElement.dataset.theme = dark ? 'dark' : 'light';
        document.querySelector('meta[name="theme-color"]').content = dark ? '#11161f' : '#f5f7fb';
        all('[data-theme-control]').forEach(select => { select.value = state.theme; });
        try { localStorage.setItem('alphapilot-workspace-theme', state.theme); } catch (_) { /* Theme storage is optional. */ }
        if (state.performance && state.currentView === 'performance') renderPerformance();
    }

    function switchAuthMode(mode, preserveMessage = false) {
        state.authMode = mode === 'register' ? 'register' : 'login';
        state.recoveryMode = '';
        byId('standard-auth-content').hidden = false;
        byId('account-recovery-panel').hidden = true;
        const registering = state.authMode === 'register';
        all('[data-auth-mode]').forEach(button => {
            const active = button.dataset.authMode === state.authMode;
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', String(active));
        });
        setText('auth-heading', registering ? '开始你的策略研究' : '回到你的工作台');
        setText('auth-description', registering ? '免费创建账户，用模拟资金探索多个市场。' : '登录后继续你的模拟交易与策略研究。');
        buttonText('auth-submit', registering ? '免费创建账户' : '登录工作台');
        byId('auth-password').autocomplete = registering ? 'new-password' : 'current-password';
        byId('auth-password').placeholder = registering ? '设置至少 12 个字符的密码' : '输入你的密码';
        byId('auth-password').minLength = registering ? 12 : 1;
        byId('auth-confirm').required = registering;
        byId('auth-confirm').value = '';
        byId('auth-confirm').setCustomValidity('');
        byId('paper-agreement').required = registering;
        ['confirm-password-field', 'paper-agreement-field', 'password-help'].forEach(id => { byId(id).hidden = !registering; });
        if (!preserveMessage) notice('auth-status');
        renderCapabilities();
    }

    function showAuth(message = '') {
        byId('boot-view').hidden = true;
        byId('app-view').hidden = true;
        byId('auth-view').hidden = false;
        document.title = '登录 · AlphaPilot';
        switchAuthMode(state.authMode, true);
        if (message) notice('auth-status', message, 'error');
        if (state.mailAction) showRecovery(state.mailAction.kind);
    }

    function clearSession(message = '') {
        state.user = null;
        state.csrf = '';
        state.accounts = [];
        state.account = null;
        state.detail = null;
        state.quote = null;
        state.instrumentId = '';
        state.pendingOrder = null;
        state.strategyDirty = false;
        state.performance = null;
        state.performanceSequence += 1;
        state.performanceLoading = false;
        state.securityLoading = false;
        state.securityRefreshRequested = false;
        state.selectionSequence += 1;
        state.quoteSequence += 1;
        state.requests.forEach(controller => controller.abort());
        state.authMode = 'login';
        byId('auth-password').value = '';
        byId('auth-confirm').value = '';
        byId('paper-agreement').checked = false;
        byId('order-quantity').value = '';
        ['current-password', 'new-password', 'new-password-confirm', 'recovery-password', 'recovery-confirm'].forEach(id => { byId(id).value = ''; });
        byId('session-list').replaceChildren();
        state.charts.forEach(chart => chart.dispose());
        state.charts.clear();
        renderMarketControls();
        renderBilling();
        showAuth(message);
    }

    function profile(data) {
        if (!data.user || !data.csrf_token) throw new Error('登录会话不完整，请重新登录。');
        state.user = data.user;
        state.csrf = data.csrf_token;
        if (data.limits) state.limits = data.limits;
        const email = String(state.user.email || '我的账户');
        setText('user-email', email);
        byId('user-email').title = email;
        setText('user-avatar', email.charAt(0).toUpperCase());
        const plan = state.user.plan === 'free' || !state.user.plan ? '免费版' : String(state.user.plan);
        setText('user-plan', plan);
        setText('billing-plan-name', plan);
        renderSecurityIdentity();
    }

    async function enterWorkspace(data) {
        profile(data);
        byId('auth-password').value = '';
        byId('auth-confirm').value = '';
        byId('auth-view').hidden = true;
        byId('boot-view').hidden = true;
        byId('app-view').hidden = false;
        const desiredView = location.hash.slice(1);
        changeView(Object.hasOwn(VIEWS, desiredView) ? desiredView : 'overview', false);
        await refreshWorkspace();
    }

    async function submitAuth(event) {
        event.preventDefault();
        const registering = state.authMode === 'register';
        if (registering && byId('auth-password').value !== byId('auth-confirm').value) {
            byId('auth-confirm').setCustomValidity('两次输入的密码不一致。');
            byId('auth-confirm').reportValidity();
            return;
        }
        if (!byId('auth-form').reportValidity()) return;
        byId('auth-fields').disabled = true;
        all('[data-auth-mode]').forEach(button => { button.disabled = true; });
        setBusy('auth-submit', true);
        buttonText('auth-submit', registering ? '正在创建账户…' : '正在登录…');
        notice('auth-status');
        try {
            const data = await request(`/auth/${registering ? 'register' : 'login'}`, {
                method: 'POST', auth: true,
                body: { email: byId('auth-email').value.trim(), password: byId('auth-password').value },
            });
            await enterWorkspace(data);
            if (registering && !state.user?.email_verified) {
                notice('workspace-notice', state.capabilities?.email_delivery_available
                    ? '账户已创建。请在账号安全中发送验证邮件并完成邮箱验证。'
                    : '账户已创建。当前邮件服务不可用，暂时无法发送验证邮件。', 'warning');
            }
        } catch (error) {
            if (!state.user) notice('auth-status', error.message, 'error');
            else notice('workspace-notice', error.message, 'error');
        } finally {
            byId('auth-fields').disabled = false;
            all('[data-auth-mode]').forEach(button => { button.disabled = false; });
            setBusy('auth-submit', false);
            buttonText('auth-submit', state.authMode === 'register' ? '免费创建账户' : '登录工作台');
            renderCapabilities();
        }
    }

    function accountLimit() {
        return number(state.limits.accounts) ?? 3;
    }

    function marketInstruments() {
        return state.markets.find(market => market.id === state.market)?.instruments || [];
    }

    function changeView(view, updateHash = true) {
        if (!Object.hasOwn(VIEWS, view)) return;
        state.currentView = view;
        all('.page-view').forEach(section => { section.hidden = section.id !== `view-${view}`; });
        all('[data-view]').forEach(button => {
            const active = button.dataset.view === view;
            button.classList.toggle('active', active);
            if (active) button.setAttribute('aria-current', 'page');
            else button.removeAttribute('aria-current');
        });
        setText('page-title', VIEWS[view][0]);
        setText('page-description', VIEWS[view][1]);
        setText('breadcrumb-current', VIEWS[view][0]);
        byId('market-toolbar').hidden = ['billing', 'security'].includes(view);
        byId('market-empty').hidden = ['billing', 'security'].includes(view) || !!state.account || !state.user;
        byId('strategy-history-section').hidden = view !== 'strategies' || !state.account;
        document.title = `${VIEWS[view][0]} · AlphaPilot`;
        if (updateHash) history.replaceState(null, '', `#${view}`);
        if (view === 'trading' && state.user && state.account && Date.now() - state.quoteFetchedAt > 20000) loadQuote();
        if (view === 'security' && state.user) loadSecurity();
        if (view === 'performance' && state.user && state.account) loadPerformance();
        if (view === 'strategies' && state.user && state.account) loadStrategyVersions();
        if (view === 'billing' && state.user) loadBillingUsage();
        renderSecurityIdentity();
    }

    function renderBilling() {
        const used = state.accounts.length;
        const limit = accountLimit();
        setText('billing-usage', `${used} / ${limit} 个`);
        byId('billing-progress').max = Math.max(1, limit);
        byId('billing-progress').value = used;
        setText('billing-usage-description', `已创建 ${used} 个模拟账户，当前额度 ${limit} 个。各市场资金独立核算。`);
    }

    async function loadBillingUsage() {
        const userId = state.user?.id;
        try {
            const data = await request('/billing');
            if (!state.user || state.user.id !== userId) return;
            setText('billing-account-count', formatQuantity(data.usage?.accounts));
            setText('billing-strategy-count', formatQuantity(data.usage?.active_strategies));
            setText('billing-order-count', formatQuantity(data.usage?.filled_orders_this_month));
            setText('billing-period', data.period?.start && data.period?.end ? `${data.period.start.slice(0, 10)} 至 ${data.period.end.slice(0, 10)} · ${data.period.timezone || 'UTC'}` : '统计区间尚未提供');
            setText('billing-metering-note', data.metering_note || '当前免费版不会根据这些使用记录产生扣费。');
            notice('billing-load-status');
        } catch (error) { if (state.user) notice('billing-load-status', `用量读取失败：${error.message}`, 'error'); }
    }

    function renderMarketControls() {
        all('[data-market]').forEach(button => {
            const active = button.dataset.market === state.market;
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', String(active));
            button.disabled = state.mutation;
        });
        const select = byId('account-select');
        const available = state.accounts.filter(account => account.market === state.market);
        select.replaceChildren();
        if (!available.length) select.add(new Option('尚未创建账户', ''));
        available.forEach(account => select.add(new Option(account.name || `${MARKETS[state.market][0]}模拟账户`, String(account.id))));
        select.value = state.account ? String(state.account.id) : '';
        select.disabled = state.mutation || !available.length;
        const empty = !state.account;
        all('.requires-account').forEach(element => { element.hidden = empty; });
        byId('market-empty').hidden = !empty || ['billing', 'security'].includes(state.currentView);
        byId('strategy-history-section').hidden = empty || state.currentView !== 'strategies';
        setText('empty-account-title', `创建你的${MARKETS[state.market][0]}模拟账户`);
        setText('empty-account-copy', `账户初始获得 100,000 ${MARKETS[state.market][1]} 模拟资金，资金、持仓与委托独立核算。`);
        const atLimit = state.accounts.length >= accountLimit();
        const needsVerification = verificationRequired();
        byId('create-account').disabled = state.mutation || atLimit || needsVerification;
        buttonText('create-account', state.mutation && empty ? '正在创建…' : '创建模拟账户');
        const gateMessage = empty && needsVerification ? '请先前往账号安全完成邮箱验证，再创建模拟账户。'
            : empty && atLimit ? `当前最多可创建 ${accountLimit()} 个模拟账户，已达到额度。` : '';
        if (gateMessage) {
            notice('create-account-status', gateMessage, 'error');
            byId('create-account-status').dataset.gated = 'true';
        } else if (byId('create-account-status').dataset.gated) {
            notice('create-account-status');
            delete byId('create-account-status').dataset.gated;
        }
    }

    function populateInstruments() {
        const instruments = marketInstruments();
        if (!instruments.some(item => item.id === state.instrumentId)) state.instrumentId = instruments[0]?.id || '';
        const oldStrategyInstrument = byId('strategy-instrument').value;
        ['instrument-select', 'strategy-instrument'].forEach(id => {
            const select = byId(id);
            select.replaceChildren();
            if (!instruments.length) select.add(new Option('暂无可用标的', ''));
            instruments.forEach(item => select.add(new Option(`${item.symbol} · ${item.name}`, item.id)));
        });
        byId('instrument-select').value = state.instrumentId;
        byId('instrument-select').disabled = !instruments.length || state.mutation;
        const savedInstrument = state.detail?.strategy?.instrument_id;
        const preferred = state.strategyDirty ? oldStrategyInstrument : savedInstrument;
        if (instruments.some(item => item.id === preferred)) byId('strategy-instrument').value = preferred;
        byId('order-quantity').placeholder = state.market === 'CRYPTO' ? '例如 0.001' : state.market === 'CN' ? '例如 100' : '例如 1';
        setText('order-quantity-help', state.market === 'CN'
            ? 'A 股当前支持 ETF 模拟交易，买入数量须符合整手规则。'
            : state.market === 'CRYPTO' ? '支持小数数量，精度及最小金额由服务端校验。' : '按美股交易时段和数量规则校验，卖出资金结算后可用。');
        renderStrategy();
        renderQuote();
    }

    async function selectAccount(account, refresh = true) {
        state.selectionSequence += 1;
        const sequence = state.selectionSequence;
        state.account = account || null;
        state.detail = null;
        state.quote = null;
        state.quoteError = '';
        state.quoteFetchedAt = 0;
        state.quoteSequence += 1;
        state.quoteLoading = false;
        setBusy('refresh-quote', false);
        state.strategyDirty = false;
        state.pendingOrder = null;
        state.performance = null;
        state.performanceOffset = 0;
        state.performanceSequence += 1;
        state.performanceLoading = false;
        renderPerformance();
        byId('order-quantity').value = '';
        notice('order-status');
        notice('strategy-save-status');
        notice('create-account-status');
        renderMarketControls();
        populateInstruments();
        renderAccount();
        if (!account || !refresh) return;
        await loadAccount(account.id, sequence);
        if (state.currentView === 'trading' && sequence === state.selectionSequence) await loadQuote();
        if (state.currentView === 'performance' && sequence === state.selectionSequence) await loadPerformance();
        if (state.currentView === 'strategies' && sequence === state.selectionSequence) await loadStrategyVersions();
    }

    async function selectMarket(market) {
        if (!Object.hasOwn(MARKETS, market) || state.mutation) return;
        state.market = market;
        state.instrumentId = '';
        notice('workspace-notice');
        await selectAccount(state.accounts.find(account => account.market === market));
    }

    async function loadAccount(accountId, sequence = state.selectionSequence) {
        try {
            const detail = await request(`/accounts/${encodeURIComponent(accountId)}`);
            if (!state.user || sequence !== state.selectionSequence) return;
            if (!detail.account) throw new Error('账户信息暂时不可用，请刷新重试。');
            state.detail = detail;
            state.account = detail.account;
            const index = state.accounts.findIndex(account => account.id === accountId);
            if (index >= 0) state.accounts[index] = detail.account;
            renderMarketControls();
            renderAccount();
            renderStrategy();
            updateOrderForm();
        } catch (error) {
            if (state.user && sequence === state.selectionSequence) {
                notice('workspace-notice', `账户读取失败：${error.message}`, 'error');
                renderEmptyTable('positions-body', 7, '持仓读取失败，请刷新重试。');
                renderEmptyTable('orders-body', 7, '委托读取失败，请刷新重试。');
            }
            throw error;
        }
    }

    async function refreshWorkspace() {
        if (!state.user || state.refreshing || state.mutation) return;
        state.refreshing = true;
        setBusy('refresh-workspace', true);
        setText('sync-status', '正在同步');
        const sequence = state.selectionSequence;
        try {
            const results = await Promise.all([
                request('/overview'),
                state.markets.length ? Promise.resolve({ markets: state.markets }) : request('/markets'),
            ]);
            if (!state.user) return;
            state.accounts = Array.isArray(results[0].accounts) ? results[0].accounts : [];
            state.markets = Array.isArray(results[1].markets) ? results[1].markets : [];
            if (results[0].limits) state.limits = results[0].limits;
            renderBilling();
            if (sequence !== state.selectionSequence) return;
            notice('workspace-notice');
            const selected = state.accounts.find(account => account.id === state.account?.id && account.market === state.market)
                || state.accounts.find(account => account.market === state.market);
            if (!state.account || !selected || selected.id !== state.account.id) {
                await selectAccount(selected);
            } else {
                state.account = selected;
                renderMarketControls();
                populateInstruments();
                await loadAccount(selected.id);
                if (state.currentView === 'trading') await loadQuote();
            }
            if (state.user) setText('sync-status', `更新于 ${new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date())}`);
            if (state.user && state.currentView === 'performance' && state.account) await loadPerformance();
        } catch (error) {
            if (state.user) {
                setText('sync-status', '同步失败');
                notice('workspace-notice', `${error.message} 页面可能保留上次成功读取的数据。`, 'error');
            }
        } finally {
            state.refreshing = false;
            setBusy('refresh-workspace', false);
        }
    }

    function valuationLabel(status) {
        const labels = { fresh: '按当前行情估值', current: '按当前行情估值', live: '按当前行情估值', ready: '按当前行情估值', ok: '按当前行情估值', complete: '按当前行情估值', cash_only: '当前仅持有模拟现金', unavailable: '部分行情不可用，估值未完成', stale: '行情已过期，估值需更新', partial: '部分持仓缺少行情', incomplete: '部分持仓缺少行情' };
        return labels[status] || (state.detail?.positions?.length === 0 ? '当前仅持有模拟现金' : '估值口径以服务端数据为准');
    }

    function renderAccount() {
        const account = state.account;
        if (!account) return;
        const positions = state.detail?.positions;
        all('.account-currency').forEach(element => { element.textContent = currency(); });
        setText('metric-equity', formatNumber(account.equity));
        setText('metric-cash', formatNumber(account.cash));
        signed('metric-realized', account.realized_pnl);
        setText('metric-equity-note', valuationLabel(account.valuation_status));
        setText('metric-positions', formatQuantity(account.positions_count ?? positions?.length));
        const pending = number(account.pending_cash) || 0;
        setText('metric-cash-note', pending > 0 ? `另有 ${money(pending)} 待结算` : '可用于新的模拟委托');
        let positionValue = null;
        if (Array.isArray(positions) && positions.every(position => number(position.market_value) !== null)) {
            positionValue = positions.reduce((sum, position) => sum + number(position.market_value), 0);
        } else if (!positions && number(account.equity) !== null && number(account.cash) !== null) {
            positionValue = Math.max(0, number(account.equity) - number(account.cash) - pending);
        }
        setText('metric-positions-value', positionValue === null ? '行情不完整，持仓估值不可用' : `持仓市值 ${money(positionValue)}`);
        setText('allocation-cash-value', formatNumber(account.cash));
        setText('allocation-holdings-value', formatNumber(positionValue));
        setText('allocation-pending-value', formatNumber(pending));
        byId('allocation-pending-row').hidden = pending <= 0;
        const equity = number(account.equity);
        const cash = number(account.cash);
        const ratio = equity !== null && equity > 0 && cash !== null ? Math.max(0, Math.min(1, cash / equity)) : null;
        const pendingRatio = equity !== null && equity > 0 ? Math.max(0, Math.min(1, pending / equity)) : 0;
        const circumference = Math.PI * 144;
        const arcLength = ratio === null ? 0 : circumference * ratio;
        byId('allocation-cash-arc').setAttribute('stroke-dasharray', `${arcLength} ${circumference}`);
        byId('allocation-pending-arc').setAttribute('stroke-dasharray', `${circumference * pendingRatio} ${circumference}`);
        byId('allocation-pending-arc').setAttribute('stroke-dashoffset', String(-arcLength));
        setText('allocation-percent', ratio === null ? '—' : `${formatNumber(ratio * 100, 1)}%`);
        byId('allocation-chart').setAttribute('aria-label', ratio === null ? '资金分布尚未就绪' : `可用现金占比 ${formatNumber(ratio * 100, 1)}%，待结算资金 ${money(pending)}，持仓市值 ${money(positionValue)}`);
        setText('valuation-note', valuationLabel(account.valuation_status));
        setText('holdings-count', positions?.length ?? account.positions_count ?? '—');
        setText('order-account-name', account.name || `${MARKETS[state.market][0]}模拟账户`);
        if (!state.detail) {
            renderEmptyTable('positions-body', 7, '正在读取持仓…');
            renderEmptyTable('orders-body', 7, '正在读取委托…');
        } else {
            renderPositions(state.detail.positions || []);
            renderOrders(state.detail.orders || []);
        }
        renderStrategy();
        updateOrderForm();
    }

    function renderEmptyTable(id, columns, message) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = columns;
        cell.className = 'table-empty';
        cell.textContent = message;
        row.append(cell);
        byId(id).replaceChildren(row);
    }

    function cell(text, className = '') {
        const element = document.createElement('td');
        element.textContent = text;
        if (className) element.className = className;
        return element;
    }

    function instrumentCell(item) {
        const element = document.createElement('td');
        element.className = 'instrument-cell';
        const name = document.createElement('strong');
        name.textContent = item.symbol || item.instrument_id || '—';
        element.append(name);
        if (item.name) {
            const description = document.createElement('small');
            description.textContent = item.name;
            element.append(description);
        }
        return element;
    }

    function pillCell(text, kind = '') {
        const element = document.createElement('td');
        const pill = document.createElement('span');
        pill.className = `status-pill${kind ? ` ${kind}` : ''}`;
        pill.textContent = text;
        element.append(pill);
        return element;
    }

    function quoteState(status, tradable = false) {
        const states = {
            fresh: ['行情可用', 'good'], current: ['行情可用', 'good'], live: ['行情可用', 'good'], ready: ['行情可用', 'good'], ok: ['行情可用', 'good'],
            stale: ['行情已过期', 'warning'], delayed: ['延迟行情', 'warning'], market_closed: ['市场休市', 'warning'], closed: ['市场休市', 'warning'],
            unavailable: ['行情不可用', 'warning'], unconfigured: ['行情未配置', 'warning'], not_configured: ['行情未配置', 'warning'], error: ['行情读取失败', 'error'], invalid: ['无效行情', 'error'],
        };
        return states[status] || (tradable ? ['可模拟交易', 'good'] : ['行情待核验', 'warning']);
    }

    function renderPositions(positions) {
        if (!positions.length) {
            renderEmptyTable('positions-body', 7, '还没有持仓。可前往模拟交易，开始验证你的想法。');
            return;
        }
        const fragment = document.createDocumentFragment();
        positions.forEach(position => {
            const row = document.createElement('tr');
            row.append(instrumentCell(position));
            row.append(cell(formatQuantity(position.quantity), 'numeric'));
            row.append(cell(formatNumber(position.average_cost), 'numeric'));
            row.append(cell(formatNumber(position.last_price), 'numeric'));
            row.append(cell(formatNumber(position.market_value), 'numeric'));
            const pnl = number(position.unrealized_pnl);
            row.append(cell(pnl === null ? '—' : `${pnl > 0 ? '+' : ''}${formatNumber(pnl)}`, `numeric${pnl > 0 ? ' positive' : pnl < 0 ? ' negative' : ''}`));
            row.append(pillCell(...quoteState(position.quote_status)));
            fragment.append(row);
        });
        byId('positions-body').replaceChildren(fragment);
    }

    function renderOrders(orders) {
        if (!orders.length) {
            renderEmptyTable('orders-body', 7, '尚无委托记录。手动与策略提交的模拟委托会显示在这里。');
            return;
        }
        const labels = { filled: ['已成交', 'good'], rejected: ['已拒绝', 'error'], pending: ['待处理', 'warning'], cancelled: ['已取消', ''], canceled: ['已取消', ''], failed: ['失败', 'error'] };
        const fragment = document.createDocumentFragment();
        orders.slice(0, 30).forEach(order => {
            const row = document.createElement('tr');
            row.append(cell(dateTime(order.created_at)));
            row.append(instrumentCell(order));
            row.append(cell(order.side === 'buy' ? '买入' : order.side === 'sell' ? '卖出' : '—', order.side === 'sell' ? 'negative' : ''));
            row.append(cell(formatQuantity(order.quantity), 'numeric'));
            row.append(cell(formatNumber(order.price), 'numeric'));
            row.append(cell(formatNumber(order.fee), 'numeric'));
            row.append(pillCell(...(labels[order.status] || ['待核验', 'warning'])));
            fragment.append(row);
        });
        byId('orders-body').replaceChildren(fragment);
    }

    function lockMutation(locked) {
        state.mutation = locked;
        all('[data-market]').forEach(button => { button.disabled = locked; });
        byId('account-select').disabled = locked || !state.account;
        byId('instrument-select').disabled = locked || !state.instrumentId;
        byId('order-fields').disabled = locked;
        byId('strategy-fields').disabled = locked || state.market === 'CN' || !marketInstruments().length || !state.detail;
        byId('refresh-workspace').disabled = locked;
        byId('create-account').disabled = locked || state.accounts.length >= accountLimit() || verificationRequired();
        byId('logout-button').disabled = locked;
        updateOrderForm();
        // A user can navigate while a write finishes; resume the quote that was gated by the write.
        if (!locked && state.currentView === 'trading' && state.account && !state.quote && !state.quoteLoading) loadQuote();
        if (!locked && state.currentView === 'performance' && state.account && !state.performance) loadPerformance();
    }

    async function createAccount() {
        if (state.mutation || !state.user) return;
        if (verificationRequired()) { renderMarketControls(); return; }
        lockMutation(true);
        buttonText('create-account', '正在创建…');
        notice('create-account-status');
        try {
            const data = await request('/accounts', { method: 'POST', body: { market: state.market } });
            const account = data.account || data;
            if (!account.id) throw new Error('创建结果尚未确认，请刷新账户列表后重试。');
            if (!state.accounts.some(item => item.id === account.id)) state.accounts.push(account);
            renderBilling();
            await selectAccount(account);
            notice('workspace-notice', `${MARKETS[state.market][0]}模拟账户已创建。可以前往模拟交易查看行情。`, 'success');
        } catch (error) {
            if (state.user) notice(state.account ? 'workspace-notice' : 'create-account-status', error.message, 'error');
        } finally {
            lockMutation(false);
            renderMarketControls();
        }
    }

    async function loadQuote() {
        if (!state.user || !state.account || !state.instrumentId || state.mutation) return;
        const sequence = ++state.quoteSequence;
        const instrumentId = state.instrumentId;
        state.quoteLoading = true;
        state.quoteError = '';
        setBusy('refresh-quote', true);
        renderQuote();
        try {
            const data = await request(`/quote?instrument_id=${encodeURIComponent(instrumentId)}`);
            if (!state.user || sequence !== state.quoteSequence || state.instrumentId !== instrumentId) return;
            state.quote = data.quote || data;
            state.quoteFetchedAt = Date.now();
        } catch (error) {
            if (state.user && sequence === state.quoteSequence) {
                state.quote = null;
                state.quoteError = error.message;
            }
        } finally {
            if (sequence === state.quoteSequence) {
                state.quoteLoading = false;
                setBusy('refresh-quote', false);
                renderQuote();
            }
        }
    }

    function renderQuote() {
        const selected = marketInstruments().find(item => item.id === state.instrumentId);
        const quote = state.quote;
        setText('quote-symbol', quote?.symbol || selected?.symbol);
        setText('quote-name', quote?.name || selected?.name || '暂无可用标的');
        setText('quote-price', formatNumber(quote?.price, state.market === 'CRYPTO' && number(quote?.price) < 1 ? 6 : 2));
        setText('quote-currency', quote?.currency || currency());
        setText('quote-bid', formatNumber(quote?.bid));
        setText('quote-ask', formatNumber(quote?.ask));
        setText('quote-time', quote?.as_of ? `${dateTime(quote.as_of)}（北京时间）` : '—');
        setText('quote-provider', quote?.provider || '—');
        setText('quote-market', MARKETS[state.market][0]);
        if (state.quoteLoading) {
            statusPill('quote-status', '正在更新');
            notice('quote-notice', '正在核验最新行情，请稍候。');
        } else if (state.quoteError) {
            statusPill('quote-status', '行情不可用', 'error');
            notice('quote-notice', state.quoteError, 'error');
        } else if (quote) {
            const tradable = quoteTradable();
            const [label, kind] = quoteState(quote.status, tradable);
            statusPill('quote-status', tradable ? label : label === '行情可用' ? '暂不可交易' : label, tradable ? kind : kind === 'error' ? 'error' : 'warning');
            notice('quote-notice', quote.reason || (tradable ? '当前行情通过核验，可提交模拟委托。成交前将再次检查行情与交易规则。' : '当前不可提交模拟委托，请等待行情或市场状态恢复。'), tradable ? '' : 'warning');
        } else {
            statusPill('quote-status', '等待行情');
            notice('quote-notice', selected ? '选择标的后读取行情。缺失或过期的行情不可用于下单。' : '当前市场暂未提供可用标的。', selected ? '' : 'warning');
        }
        updateOrderForm();
    }

    function orderSide() {
        return document.querySelector('input[name="order-side"]:checked').value;
    }

    function quoteTradable() {
        return state.quote?.tradable === true && number(state.quote.price) > 0
            && ['live', 'fresh', 'current', 'ready', 'ok'].includes(state.quote.status);
    }

    function updateOrderForm() {
        const buying = orderSide() === 'buy';
        const position = state.detail?.positions?.find(item => item.instrument_id === state.instrumentId);
        setText('order-balance-label', buying ? '可用资金' : '当前可卖数量');
        setText('order-balance', buying ? money(state.account?.cash) : formatQuantity(position?.available_quantity ?? position?.quantity ?? 0));
        const price = number(buying ? state.quote?.ask ?? state.quote?.price : state.quote?.bid ?? state.quote?.price);
        const quantity = number(byId('order-quantity').value.trim());
        setText('order-price', money(price, state.quote?.currency || currency()));
        setText('order-total', quantity !== null && quantity > 0 && price !== null ? money(quantity * price) : '—');
        byId('order-submit').disabled = state.mutation || state.quoteLoading || !state.account || !state.instrumentId
            || !quoteTradable() || price === null || !state.detail || verificationRequired();
        buttonText('order-submit', state.mutation ? '正在提交…' : buying ? '模拟买入' : '模拟卖出');
    }

    function requestId() {
        if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        bytes[6] = (bytes[6] & 15) | 64;
        bytes[8] = (bytes[8] & 63) | 128;
        const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
        return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }

    async function submitOrder(event) {
        event.preventDefault();
        if (state.mutation || !state.account || byId('order-submit').disabled) return;
        const quantity = byId('order-quantity').value.trim();
        if (!byId('order-form').reportValidity() || !(number(quantity) > 0)) {
            notice('order-status', '请输入大于 0 的有效数量。', 'error');
            return;
        }
        const accountId = state.account.id;
        const side = orderSide();
        const intent = `${accountId}:${state.instrumentId}:${side}:${quantity}`;
        if (!state.pendingOrder || state.pendingOrder.intent !== intent) state.pendingOrder = { intent, id: requestId() };
        const idempotencyKey = state.pendingOrder.id;
        lockMutation(true);
        notice('order-status', '正在核验行情并提交模拟委托…');
        try {
            const data = await request(`/accounts/${encodeURIComponent(accountId)}/orders`, {
                method: 'POST', body: { instrument_id: state.instrumentId, side, quantity, request_id: idempotencyKey },
            });
            const order = data.order;
            if (!order) throw new Error('委托结果尚未确认，请刷新记录后使用相同内容重试。');
            state.pendingOrder = null;
            byId('order-quantity').value = '';
            const filled = order.status === 'filled';
            notice('order-status', filled
                ? `模拟${side === 'buy' ? '买入' : '卖出'}已成交：${formatQuantity(order.quantity)}，成交价 ${money(order.price)}。`
                : order.status === 'rejected' ? `模拟委托被拒绝。${order.reason || '请检查行情与账户状态。'}` : '模拟委托已提交，请在账户总览中查看处理结果。', filled ? 'success' : order.status === 'rejected' ? 'error' : '');
            await loadAccount(accountId);
        } catch (error) {
            if (state.user) notice('order-status', `${error.message}${state.pendingOrder ? ' 重试相同委托会沿用请求编号，避免重复提交。' : ''}`, 'error');
        } finally {
            lockMutation(false);
            updateOrderForm();
        }
    }

    function strategyFacts() {
        const strategy = state.detail?.strategy;
        if (state.market === 'CN') return { strategy, label: '当前市场暂不支持', kind: '', lastRun: null, reason: 'A 股新账户当前提供 ETF 手动模拟交易。自动趋势策略支持美股与数字资产。' };
        if (!strategy) return { strategy: null, label: '尚未启用', kind: '', lastRun: null, reason: '尚无运行记录。保存并启用策略后，执行结果会显示在这里。' };
        const labels = { running: ['运行中', 'good'], active: ['已启用', 'good'], enabled: ['已启用', 'good'], paused: ['已暂停', 'warning'], stopped: ['已停止', ''], disabled: ['已停用', ''], error: ['执行异常', 'error'], waiting: ['等待评估', ''], observing: ['观察中', ''], executed: ['本轮已执行', 'good'], waiting_market: ['等待开市', ''], waiting_data: ['等待行情', 'warning'], blocked: ['本轮暂缓', 'warning'], risk_paused: ['风控暂停', 'warning'] };
        let [label, kind] = !strategy.enabled ? ['已停用', ''] : (labels[strategy.status] || ['已启用', 'good']);
        if (strategy.enabled && state.detail?.worker?.running === false && !['risk_paused', 'error', 'paused'].includes(strategy.status)) {
            label = '已启用 · 等待执行服务';
            kind = 'warning';
        }
        return { strategy, label, kind, lastRun: strategy.last_run_at || strategy.last_evaluated_at, reason: strategy.last_reason || strategy.reason || (strategy.enabled ? '策略已启用，等待服务端评估记录。' : '策略已停用，不再自动提交新的模拟委托。') };
    }

    function renderStrategy() {
        const facts = strategyFacts();
        statusPill('overview-strategy-status', facts.label, facts.kind);
        statusPill('strategy-runtime-status', facts.label, facts.kind);
        setText('overview-strategy-last-run', dateTime(facts.lastRun));
        setText('strategy-runtime-last-run', dateTime(facts.lastRun));
        setText('strategy-worker-status', state.market === 'CN' ? '当前市场暂不支持' : !state.detail?.worker ? '状态未知' : state.detail.worker.running ? '在线' : '未检测到有效心跳');
        setText('strategy-worker-heartbeat', state.market === 'CN' ? '—' : dateTime(state.detail?.worker?.heartbeat_at));
        setText('strategy-runtime-reason', facts.reason);
        setText('overview-strategy-copy', state.market === 'CN' ? facts.reason : facts.strategy ? facts.reason : '选择策略标的与仓位上限，开始积累可复盘的执行记录。');
        const strategyInstrument = marketInstruments().find(item => item.id === facts.strategy?.instrument_id);
        setText('strategy-runtime-instrument', strategyInstrument ? `${strategyInstrument.symbol} · ${strategyInstrument.name}` : facts.strategy?.instrument_id || '—');
        const allocation = number(facts.strategy?.max_allocation);
        setText('strategy-runtime-allocation', allocation === null ? '—' : `${formatNumber(allocation * 100, 0)}%`);
        byId('strategy-support-notice').hidden = state.market !== 'CN';
        byId('strategy-fields').disabled = state.mutation || state.market === 'CN' || !marketInstruments().length || !state.detail;
        if (!state.strategyDirty && !state.mutation) {
            byId('strategy-enabled').checked = !!facts.strategy?.enabled;
            byId('strategy-allocation').value = allocation === 0.1 ? '0.10' : '0.20';
            if (strategyInstrument) byId('strategy-instrument').value = strategyInstrument.id;
        }
    }

    async function saveStrategy(event) {
        event.preventDefault();
        if (state.mutation || !state.account || state.market === 'CN' || !byId('strategy-form').reportValidity()) return;
        const accountId = state.account.id;
        const body = {
            enabled: byId('strategy-enabled').checked,
            instrument_id: byId('strategy-instrument').value,
            max_allocation: byId('strategy-allocation').value,
        };
        if (body.enabled && verificationRequired()) {
            notice('strategy-save-status', '请先在账号安全中完成邮箱验证，再启用自动策略。', 'error');
            return;
        }
        lockMutation(true);
        buttonText('strategy-submit', '正在保存…');
        notice('strategy-save-status');
        try {
            const data = await request(`/accounts/${encodeURIComponent(accountId)}/strategy`, { method: 'POST', body });
            if (state.detail) state.detail.strategy = data.strategy || data;
            state.strategyDirty = false;
            notice('strategy-save-status', body.enabled ? '策略设置已保存并启用。实际执行结果将在运行状态与委托记录中显示。' : '策略已停用。现有持仓会保留，可在模拟交易中管理。', 'success');
            await loadAccount(accountId);
            await loadStrategyVersions();
        } catch (error) {
            if (state.user) notice('strategy-save-status', error.message, 'error');
        } finally {
            lockMutation(false);
            buttonText('strategy-submit', '保存策略设置');
            renderStrategy();
        }
    }

    function verificationRequired() {
        return !!(state.user && state.capabilities?.require_verified_email && !state.user.email_verified);
    }

    async function loadCapabilities() {
        try { state.capabilities = await request('/auth/capabilities', { auth: true }); }
        catch (_) { state.capabilities = null; }
        renderCapabilities();
        renderSecurityIdentity();
    }

    function renderCapabilities() {
        const caps = state.capabilities;
        const available = caps?.email_delivery_available === true;
        setText('auth-delivery-note', !caps ? '账户服务状态暂时不可用。' : available ? '可以通过已验证邮箱恢复账号。' : '邮件服务暂未启用，当前无法找回密码。');
        all('[data-auth-mode="register"]').forEach(button => { button.disabled = caps?.registration_allowed === false; });
        if (state.authMode === 'register') {
            byId('auth-submit').disabled = caps?.registration_allowed === false;
            if (caps?.registration_allowed === false) notice('auth-status', '当前暂未开放新用户注册。', 'warning');
        }
        if (state.recoveryMode === 'forgot') {
            byId('recovery-submit').disabled = !available;
            setText('recovery-explanation', available
                ? '填写注册邮箱后申请重置。若该邮箱可用于恢复账号，你将收到重置邮件。'
                : '当前邮件服务不可用，暂时无法申请密码重置。请在服务恢复后重试。');
        }
    }

    function showRecovery(mode) {
        state.recoveryMode = mode;
        byId('standard-auth-content').hidden = true;
        byId('account-recovery-panel').hidden = false;
        const copy = {
            forgot: ['找回你的账号', '通过安全邮件设置新密码。', '申请重置密码'],
            reset: ['设置新密码', '新密码生效后，所有设备需要重新登录。', '设置新密码'],
            verify: ['验证邮箱地址', '确认这是你注册 AlphaPilot 使用的邮箱。', '验证邮箱'],
            verified: ['邮箱已验证', '邮箱验证已完成，可以继续使用你的工作台。', '验证完成'],
        }[mode];
        setText('auth-heading', copy[0]);
        setText('auth-description', copy[1]);
        buttonText('recovery-submit', copy[2]);
        buttonText('recovery-back', state.user ? '返回工作台' : '返回登录');
        byId('recovery-email-field').hidden = mode !== 'forgot';
        byId('recovery-email').required = mode === 'forgot';
        ['recovery-password-field', 'recovery-confirm-field'].forEach(id => { byId(id).hidden = mode !== 'reset'; });
        ['recovery-password', 'recovery-confirm'].forEach(id => { byId(id).required = mode === 'reset'; });
        byId('recovery-confirm').setCustomValidity('');
        byId('recovery-submit').hidden = mode === 'verified';
        byId('recovery-submit').disabled = (['verify', 'reset'].includes(mode) && !state.mailAction?.token);
        notice('recovery-status');
        if (mode === 'verify') setText('recovery-explanation', '点击下方按钮确认验证。此操作只验证邮件链接对应的账号，不会更换当前登录账号。');
        if (mode === 'reset') setText('recovery-explanation', '至少使用 12 个字符。此链接仅能使用一次，请确认后设置新密码。');
        if (mode === 'verified') setText('recovery-explanation', '验证链接已使用完成。');
        if (['verify', 'reset'].includes(mode) && !state.mailAction?.token) notice('recovery-status', '此链接无效，请重新申请邮件。', 'error');
        renderCapabilities();
    }

    async function submitRecovery(event) {
        event.preventDefault();
        const mode = state.recoveryMode;
        if (mode === 'reset' && byId('recovery-password').value !== byId('recovery-confirm').value) {
            byId('recovery-confirm').setCustomValidity('两次输入的新密码不一致。');
            byId('recovery-confirm').reportValidity();
            return;
        }
        if (!byId('recovery-form').reportValidity()) return;
        byId('recovery-fields').disabled = true;
        byId('recovery-back').disabled = true;
        notice('recovery-status');
        try {
            if (mode === 'forgot') {
                const data = await request('/auth/password-reset/request', { method: 'POST', auth: true, body: { email: byId('recovery-email').value.trim() } });
                notice('recovery-status', data.detail || '重置申请已受理。若该邮箱可用于恢复账号，请检查收件箱。', 'success');
            } else if (mode === 'verify') {
                const data = await request('/auth/email-verification/confirm', { method: 'POST', auth: true, body: { token: state.mailAction?.token || '' } });
                if (data.email_verified !== true) throw new Error('邮箱验证结果尚未确认，请稍后重试。');
                state.mailAction = null;
                if (state.user) {
                    try { profile(await request('/auth/me')); } catch (_) { /* Verification may target a different signed-in account. */ }
                }
                showRecovery('verified');
                notice('recovery-status', '邮箱验证成功。', 'success');
            } else if (mode === 'reset') {
                await request('/auth/password-reset/confirm', { method: 'POST', auth: true, body: { token: state.mailAction?.token || '', new_password: byId('recovery-password').value } });
                state.mailAction = null;
                clearSession();
                notice('auth-status', '密码已重置，所有旧会话已注销。请使用新密码登录。', 'success');
            }
        } catch (error) { notice('recovery-status', error.message, 'error'); }
        finally {
            byId('recovery-fields').disabled = false;
            byId('recovery-back').disabled = false;
            if (mode === 'reset') ['recovery-password', 'recovery-confirm'].forEach(id => { byId(id).value = ''; });
        }
    }

    function renderSecurityIdentity() {
        const verified = state.user?.email_verified === true;
        setText('security-email', state.user?.email || '—');
        statusPill('security-email-state', verified ? '已验证' : '未验证', verified ? 'good' : 'warning');
        const available = state.capabilities?.email_delivery_available === true;
        setText('security-verification-help', verified ? '邮箱已完成验证。请保持对该邮箱的访问权限，以便接收账号恢复邮件。'
            : available ? '发送验证邮件后，打开邮件中的链接并确认。' : '邮件服务暂时不可用，当前无法发送验证邮件；账号状态会如实保留为未验证。');
        byId('send-verification').disabled = verified || !available || state.sessionBusy || state.securityLoading;
        buttonText('send-verification', verified ? '邮箱已验证' : '发送验证邮件');
        byId('verification-banner').hidden = !state.user || verified || state.currentView === 'security';
        setText('verification-banner-copy', verificationRequired()
            ? '请先验证邮箱，再创建模拟账户、提交委托或启用自动策略。'
            : '邮箱尚未验证。完成验证有助于恢复账号和保护登录安全。');
        updateOrderForm();
        renderMarketControls();
    }

    async function loadSecurity() {
        if (!state.user) return;
        if (state.securityLoading) { state.securityRefreshRequested = true; return; }
        state.securityLoading = true;
        const userId = state.user.id;
        lockSecurityControls();
        try {
            const results = await Promise.allSettled([
                request('/auth/me'), request('/auth/sessions'), request('/auth/email-verification/status'), loadCapabilities(),
            ]);
            if (!state.user || state.user.id !== userId) return;
            if (results[0].status === 'fulfilled') profile(results[0].value);
            if (results[1].status === 'fulfilled') renderSessions(results[1].value.sessions || []);
            else notice('sessions-status', results[1].reason.message, 'error');
            if (results[2].status === 'fulfilled') {
                const value = results[2].value;
                const labels = { not_requested: '尚未请求', pending: '正在处理', sent: '已发送', failed: '投递失败', verified: '邮箱已验证', expired: '链接已过期，请重新发送' };
                setText('verification-delivery-status', labels[value.status] || '状态未知');
                setText('verification-requested-at', dateTime(value.requested_at));
            } else {
                setText('verification-delivery-status', '暂时无法读取');
                setText('verification-requested-at', '—');
            }
            renderSecurityIdentity();
        } finally {
            state.securityLoading = false;
            lockSecurityControls();
            if (state.securityRefreshRequested && state.user) { state.securityRefreshRequested = false; await loadSecurity(); }
        }
    }

    function lockSecurityControls() {
        const busy = state.sessionBusy || state.securityLoading;
        all('#session-list button').forEach(button => { button.disabled = busy; });
        byId('revoke-other-sessions').disabled = busy || byId('session-list').children.length < 2;
        byId('logout-button').disabled = busy || state.mutation;
        renderSecurityIdentity();
    }

    function renderSessions(sessions) {
        notice('sessions-status');
        byId('session-list').replaceChildren();
        byId('revoke-other-sessions').disabled = !sessions.some(session => !session.current) || state.sessionBusy || state.securityLoading;
        if (!sessions.length) {
            const empty = document.createElement('p');
            empty.className = 'table-empty';
            empty.textContent = '没有可显示的登录会话。';
            byId('session-list').append(empty);
            return;
        }
        sessions.forEach(session => {
            const row = document.createElement('div');
            row.className = 'session-row';
            row.append(icon('monitor-smartphone'));
            const info = document.createElement('div');
            info.className = 'session-details';
            const label = document.createElement('strong');
            label.textContent = `登录于 ${dateTime(session.created_at)}`;
            info.append(label);
            if (session.current) {
                const badge = document.createElement('span');
                badge.className = 'status-pill good';
                badge.textContent = '当前会话';
                info.append(badge);
            }
            const expiry = document.createElement('p');
            expiry.textContent = `有效期至 ${dateTime(session.expires_at)}`;
            info.append(expiry);
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'button secondary small';
            button.textContent = session.current ? '退出当前' : '注销';
            button.disabled = state.sessionBusy || state.securityLoading;
            button.addEventListener('click', () => revokeSession(session.id, session.current));
            row.append(info, button);
            byId('session-list').append(row);
        });
        renderIcons();
    }

    async function revokeSession(id, current = false) {
        if (state.sessionBusy) return;
        state.sessionBusy = true;
        lockSecurityControls();
        try {
            await request(id ? `/auth/sessions/${encodeURIComponent(id)}` : '/auth/sessions/revoke-others', { method: id ? 'DELETE' : 'POST' });
            if (current) {
                clearSession();
                notice('auth-status', '当前会话已注销，请重新登录。', 'success');
            } else {
                await loadSecurity();
                notice('sessions-status', id ? '该登录会话已注销。' : '其他登录会话已注销，当前会话已保留。', 'success');
            }
        } catch (error) { if (state.user) notice('sessions-status', error.message, 'error'); }
        finally {
            state.sessionBusy = false;
            lockSecurityControls();
        }
    }

    async function sendVerification() {
        if (!state.user || state.sessionBusy) return;
        state.sessionBusy = true;
        lockSecurityControls();
        setBusy('send-verification', true);
        notice('verification-status');
        try {
            const data = await request('/auth/email-verification/request', { method: 'POST', body: {} });
            notice('verification-status', data.detail || '验证邮件已发送，请检查收件箱。', 'success');
            await loadSecurity();
        } catch (error) { if (state.user) notice('verification-status', error.message, 'error'); }
        finally { state.sessionBusy = false; setBusy('send-verification', false); lockSecurityControls(); }
    }

    async function changePassword(event) {
        event.preventDefault();
        if (byId('new-password').value !== byId('new-password-confirm').value) {
            byId('new-password-confirm').setCustomValidity('两次输入的新密码不一致。');
            byId('new-password-confirm').reportValidity();
            return;
        }
        if (!byId('password-change-form').reportValidity()) return;
        byId('password-change-fields').disabled = true;
        state.sessionBusy = true;
        lockSecurityControls();
        notice('password-change-status');
        try {
            await request('/auth/password/change', { method: 'POST', body: { current_password: byId('current-password').value, new_password: byId('new-password').value } });
            clearSession();
            notice('auth-status', '密码已修改，所有旧会话已注销。请重新登录。', 'success');
        } catch (error) { if (state.user) notice('password-change-status', error.message, 'error'); }
        finally {
            byId('password-change-fields').disabled = false;
            state.sessionBusy = false;
            lockSecurityControls();
            ['current-password', 'new-password', 'new-password-confirm'].forEach(id => { byId(id).value = ''; });
        }
    }

    function percent(value) {
        const parsed = number(value);
        return parsed === null ? '—' : `${parsed > 0 ? '+' : ''}${formatNumber(parsed)}%`;
    }

    async function loadPerformance(offset = state.performanceOffset) {
        if (!state.user || !state.account || state.performanceLoading) return;
        state.performanceLoading = true;
        const sequence = ++state.performanceSequence;
        const accountId = state.account.id;
        state.performanceOffset = Math.max(0, offset);
        notice('performance-status', '正在读取真实净值快照…');
        try {
            const data = await request(`/accounts/${encodeURIComponent(accountId)}/performance?limit=2000&offset=${state.performanceOffset}`);
            if (!state.user || state.account?.id !== accountId || sequence !== state.performanceSequence) return;
            state.performance = data;
            renderPerformance();
        } catch (error) {
            if (state.user && sequence === state.performanceSequence) {
                state.performance = null;
                renderPerformance();
                notice('performance-status', `收益数据读取失败：${error.message}`, 'error');
            }
        } finally { if (sequence === state.performanceSequence) state.performanceLoading = false; }
    }

    function renderPerformance() {
        const data = state.performance;
        const points = Array.isArray(data?.points) ? data.points : [];
        const summary = data?.summary || {};
        const benchmark = data?.benchmark || {};
        const coverage = data?.coverage || {};
        const observer = data?.observer;
        const pagination = data?.pagination || {};
        const total = number(pagination.total) ?? points.length;
        const valid = points.filter(point => number(point.equity) !== null);
        all('.account-currency').forEach(element => { element.textContent = currency(); });
        setText('performance-equity', formatNumber(summary.last_equity));
        setText('performance-equity-time', summary.last_at ? `${dateTime(summary.last_at)}（北京时间）` : '尚无快照');
        setText('performance-return', percent(summary.total_return_pct));
        setText('performance-drawdown', total >= 2 ? percent(summary.max_drawdown_pct) : '—');
        setText('performance-benchmark', benchmark.paired_samples >= 2 ? percent(summary.benchmark_return_pct) : '—');
        setText('performance-return-note', `按初始资金 ${money(summary.initial_cash)} 计算`);
        setText('performance-benchmark-name', benchmark.name ? `${benchmark.name} · 价格收益` : '基准数据尚未就绪');
        setText('performance-period', points.length ? `${dateTime(points[0].as_of)} 至 ${dateTime(points.at(-1).as_of)} · 北京时间` : '尚无可显示的净值记录');
        setText('performance-coverage', `已记录 ${total} 个快照；${coverage.gap_count || 0} 段采样缺口；${coverage.missing_benchmark_samples || 0} 个快照缺少基准配对。缺测不补值、不连线。`);
        setText('snapshot-record-count', `${points.length} / ${total} 条`);
        setText('comparison-chart-description', benchmark.baseline_as_of ? `以 ${dateTime(benchmark.baseline_as_of)} 首次配对归一 · 价格收益，不含分红再投资` : '等待首次基准配对，不使用历史回填');
        setText('equity-chart-description', '实际净值快照 · 人民币或美元按账户分别核算');
        statusPill('observer-state', !observer ? '采样状态未知' : observer.running ? '净值采样在线' : '等待净值采样服务', observer?.running ? 'good' : 'warning');
        setText('observer-detail', !observer ? '尚无采样进程状态' : `最近心跳 ${dateTime(observer.heartbeat_at)} · 最近循环 ${dateTime(observer.last_cycle_at)}`);
        if (!data) notice('performance-status');
        else if (!valid.length) notice('performance-status', '尚无有效净值快照。采样服务产生记录后才会展示收益轨迹。');
        else if (valid.length === 1) notice('performance-status', '当前页仅有一个有效快照，仅展示该点；尚不足以判断区间走势或回撤。', 'warning');
        else if ((coverage.gap_count || 0) > 0 || (coverage.missing_benchmark_samples || 0) > 0) notice('performance-status', '部分采样或基准数据缺失，图表保留间断；收益摘要以服务端已记录的有效快照为准。', 'warning');
        else notice('performance-status');
        byId('performance-pagination').hidden = total <= (number(pagination.limit) || 2000);
        byId('performance-previous').disabled = (number(pagination.offset) || 0) <= 0;
        byId('performance-next').disabled = pagination.has_more !== true;
        setText('performance-page-label', `本页 ${points.length} 条 · 共 ${total} 条`);
        const fragment = document.createDocumentFragment();
        points.slice(-100).reverse().forEach(point => {
            const row = document.createElement('tr');
            row.append(cell(dateTime(point.as_of)), cell(formatNumber(point.equity), 'numeric'), cell(formatNumber(point.cash), 'numeric'), cell(formatNumber(point.pending_cash), 'numeric'), cell(percent(point.return_pct), 'numeric'), pillCell(point.valuation_status === 'initial' ? '初始快照' : number(point.equity) !== null ? '已估值' : '缺少估值', number(point.equity) !== null ? '' : 'warning'));
            fragment.append(row);
        });
        if (points.length) {
            byId('snapshots-body').replaceChildren(fragment);
            if (points.length > 100) setText('snapshot-record-count', `表格显示本页最近 100 条 · 图表 ${points.length} 条 · 共 ${total} 条`);
        } else renderEmptyTable('snapshots-body', 6, '尚无净值快照。');
        const paired = points.filter(point => number(point.benchmark_return_pct) !== null && number(point.comparison_return_pct) !== null);
        drawPerformanceChart('equity-chart', points, [{ name: `账户净值 (${currency()})`, key: 'equity' }], valid.length > 0, false);
        drawPerformanceChart('comparison-chart', points, [{ name: '账户区间收益', key: 'comparison_return_pct' }, { name: benchmark.name || '市场基准', key: 'benchmark_return_pct' }], paired.length >= 2, true);
        drawPerformanceChart('drawdown-chart', points, [{ name: '账户回撤', key: 'drawdown_pct' }], valid.length >= 2, true);
    }

    function drawPerformanceChart(id, points, lines, show, percentage) {
        const container = byId(id);
        container.hidden = !show;
        byId(`${id}-empty`).hidden = show;
        if (!show) { state.charts.get(id)?.clear(); return; }
        if (state.currentView !== 'performance') return;
        if (!window.echarts) {
            container.hidden = true;
            byId(`${id}-empty`).hidden = false;
            setText(`${id}-empty`, '图表组件不可用，仍可查看快照明细或下载 CSV。');
            return;
        }
        const style = getComputedStyle(document.documentElement);
        const muted = style.getPropertyValue('--muted').trim();
        const border = style.getPropertyValue('--border').trim();
        const accent = style.getPropertyValue('--accent').trim();
        const gaps = state.performance?.coverage?.gaps || [];
        const valuationGaps = state.performance?.coverage?.valuation_gaps || [];
        const chart = state.charts.get(id) || window.echarts.init(container, null, { renderer: 'canvas' });
        state.charts.set(id, chart);
        const series = lines.map((line, index) => {
            const values = [];
            points.forEach((point, position) => {
                const time = new Date(point.as_of).getTime();
                const previous = position > 0 ? new Date(points[position - 1].as_of).getTime() : null;
                if (previous !== null && (point.gap_before === true || gaps.some(gap => new Date(gap.from).getTime() < time && new Date(gap.to).getTime() > previous))) values.push([previous + (time - previous) / 2, null]);
                values.push([time, number(point[line.key])]);
            });
            if (values.length) {
                const from = values[0][0];
                const to = values.at(-1)[0];
                valuationGaps.forEach(gap => { const time = new Date(gap.as_of).getTime(); if (time > from && time < to) values.push([time, null]); });
                values.sort((a, b) => a[0] - b[0]);
            }
            return { name: line.name, type: 'line', data: values, smooth: false, connectNulls: false, showSymbol: points.length <= 20, symbolSize: 6, lineStyle: { width: 2, type: index ? 'dashed' : 'solid' }, itemStyle: { color: index ? '#8b9ebc' : accent }, animation: false };
        });
        chart.setOption({
            animation: false, color: [accent, '#8b9ebc'],
            textStyle: { color: muted, fontFamily: 'system-ui, Microsoft YaHei, sans-serif' },
            grid: { left: 20, right: 25, top: 45, bottom: 32, containLabel: true },
            legend: { top: 3, textStyle: { color: muted, fontSize: 10 }, itemWidth: 15, itemHeight: 7 },
            tooltip: { trigger: 'axis', renderMode: 'richText', confine: true, backgroundColor: style.getPropertyValue('--surface').trim(), borderColor: border, textStyle: { color: style.getPropertyValue('--ink').trim(), fontSize: 11 }, formatter: items => {
                const entries = Array.isArray(items) ? items : [items];
                if (!entries.length) return '';
                return [`${dateTime(entries[0].value?.[0])}（北京时间）`, ...entries.map(item => `${item.seriesName}：${item.value?.[1] === null ? '缺测' : `${formatNumber(item.value?.[1])}${percentage ? '%' : ` ${currency()}`}`}`)].join('\n');
            } },
            xAxis: { type: 'time', axisLine: { lineStyle: { color: border } }, splitLine: { show: false }, axisTick: { show: false }, axisLabel: { color: muted, fontSize: 9, hideOverlap: true, formatter: value => new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) } },
            yAxis: { type: 'value', scale: true, axisLabel: { color: muted, fontSize: 9, formatter: value => `${formatQuantity(value)}${percentage ? '%' : ''}` }, splitLine: { lineStyle: { color: border, type: 'dashed' } } }, series,
        }, true);
        chart.resize();
    }

    async function downloadCsv(kind) {
        if (!state.user || !state.account || state.exportBusy || !['orders', 'snapshots'].includes(kind)) return;
        state.exportBusy = true;
        const accountId = state.account.id;
        const market = state.market;
        all('[data-export]').forEach(button => { button.disabled = true; });
        notice('export-status', '正在准备 CSV 下载…');
        try {
            const blob = await request(`/accounts/${encodeURIComponent(accountId)}/${kind}.csv`, { blob: true });
            if (!state.user || state.account?.id !== accountId) return;
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = `AlphaPilot-${market}-${kind}.csv`;
            document.body.append(link);
            link.click();
            link.remove();
            window.setTimeout(() => URL.revokeObjectURL(url), 1000);
            notice('export-status', `${kind === 'orders' ? '成交' : '净值'} CSV 已准备下载。`, 'success');
        } catch (error) { if (state.user) notice('export-status', `导出失败：${error.message}`, 'error'); }
        finally { state.exportBusy = false; all('[data-export]').forEach(button => { button.disabled = false; }); }
    }

    async function loadStrategyVersions() {
        if (!state.user || !state.account) return;
        const accountId = state.account.id;
        try {
            const data = await request(`/accounts/${encodeURIComponent(accountId)}/strategy/versions`);
            if (!state.user || state.account?.id !== accountId) return;
            const versions = data.versions || [];
            if (!versions.length) { renderEmptyTable('strategy-versions-body', 5, '尚无策略修改记录。'); return; }
            const fragment = document.createDocumentFragment();
            versions.slice(0, 20).forEach(version => {
                const row = document.createElement('tr');
                row.append(cell(`v${version.version}`), cell(dateTime(version.created_at)), cell(version.instrument_id || '—'), cell(`${formatNumber(number(version.max_allocation) * 100, 0)}%`, 'numeric'), pillCell(version.enabled ? '启用' : '停用', version.enabled ? 'good' : ''));
                fragment.append(row);
            });
            byId('strategy-versions-body').replaceChildren(fragment);
        } catch (_) { if (state.user && state.account?.id === accountId) renderEmptyTable('strategy-versions-body', 5, '修改记录暂时不可用，请刷新重试。'); }
    }

    function handleAsync(action) {
        return (...args) => {
            Promise.resolve(action(...args)).catch(error => {
                if (state.user) notice('workspace-notice', error.message, 'error');
            });
        };
    }

    function bindEvents() {
        all('[data-theme-control]').forEach(select => select.addEventListener('change', () => applyTheme(select.value)));
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (state.theme === 'system') applyTheme('system'); });
        all('[data-auth-mode]').forEach(button => button.addEventListener('click', () => switchAuthMode(button.dataset.authMode)));
        byId('auth-form').addEventListener('submit', submitAuth);
        byId('auth-confirm').addEventListener('input', () => byId('auth-confirm').setCustomValidity(''));
        byId('auth-password').addEventListener('input', () => byId('auth-confirm').setCustomValidity(''));
        byId('forgot-password').addEventListener('click', () => { byId('recovery-email').value = byId('auth-email').value; showRecovery('forgot'); });
        byId('recovery-form').addEventListener('submit', submitRecovery);
        ['recovery-password', 'recovery-confirm'].forEach(id => byId(id).addEventListener('input', () => byId('recovery-confirm').setCustomValidity('')));
        byId('recovery-back').addEventListener('click', () => {
            state.mailAction = null;
            state.recoveryMode = '';
            if (state.user) {
                byId('auth-view').hidden = true;
                byId('app-view').hidden = false;
                changeView('security');
            } else switchAuthMode('login');
        });
        byId('send-verification').addEventListener('click', sendVerification);
        byId('password-change-form').addEventListener('submit', changePassword);
        ['new-password', 'new-password-confirm'].forEach(id => byId(id).addEventListener('input', () => byId('new-password-confirm').setCustomValidity('')));
        byId('revoke-other-sessions').addEventListener('click', () => revokeSession(null));
        all('[data-export]').forEach(button => button.addEventListener('click', () => downloadCsv(button.dataset.export)));
        byId('performance-previous').addEventListener('click', () => loadPerformance(Math.max(0, state.performanceOffset - 2000)));
        byId('performance-next').addEventListener('click', () => loadPerformance(state.performance?.pagination?.next_offset ?? state.performanceOffset + 2000));
        window.addEventListener('resize', () => state.charts.forEach(chart => chart.resize()));
        byId('toggle-password').addEventListener('click', () => {
            const visible = byId('auth-password').type === 'password';
            byId('auth-password').type = visible ? 'text' : 'password';
            byId('toggle-password').setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
            byId('toggle-password').setAttribute('aria-pressed', String(visible));
            byId('toggle-password').replaceChildren(icon(visible ? 'eye-off' : 'eye'));
            renderIcons();
        });
        all('[data-view]').forEach(button => button.addEventListener('click', () => changeView(button.dataset.view)));
        all('[data-navigate]').forEach(button => button.addEventListener('click', () => {
            changeView(button.dataset.navigate);
            byId('main-content').focus({ preventScroll: true });
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }));
        window.addEventListener('hashchange', () => {
            if (captureMailAction()) {
                byId('app-view').hidden = true;
                byId('auth-view').hidden = false;
                showRecovery(state.mailAction.kind);
            } else if (state.user) changeView(location.hash.slice(1), false);
        });
        all('[data-market]').forEach(button => button.addEventListener('click', handleAsync(() => selectMarket(button.dataset.market))));
        byId('account-select').addEventListener('change', handleAsync(() => selectAccount(state.accounts.find(account => String(account.id) === byId('account-select').value))));
        byId('refresh-workspace').addEventListener('click', () => state.currentView === 'security' ? loadSecurity() : refreshWorkspace());
        byId('create-account').addEventListener('click', createAccount);
        byId('refresh-quote').addEventListener('click', loadQuote);
        byId('instrument-select').addEventListener('change', () => {
            state.instrumentId = byId('instrument-select').value;
            state.quote = null;
            state.quoteFetchedAt = 0;
            notice('order-status');
            loadQuote();
        });
        byId('order-quantity').addEventListener('input', updateOrderForm);
        all('input[name="order-side"]').forEach(input => input.addEventListener('change', () => { notice('order-status'); updateOrderForm(); }));
        byId('order-form').addEventListener('submit', submitOrder);
        byId('strategy-form').addEventListener('submit', saveStrategy);
        byId('strategy-form').addEventListener('change', () => { state.strategyDirty = true; notice('strategy-save-status'); });
        byId('logout-button').addEventListener('click', async () => {
            if (state.mutation) return;
            setBusy('logout-button', true);
            try {
                await request('/auth/logout', { method: 'POST', body: {} });
                clearSession();
                notice('auth-status', '已安全退出登录。', 'success');
            } catch (error) {
                if (state.user) notice('workspace-notice', `退出登录失败：${error.message}`, 'error');
            } finally { setBusy('logout-button', false); }
        });
        document.addEventListener('visibilitychange', () => { if (!document.hidden && state.user) refreshWorkspace(); });
        window.addEventListener('pagehide', () => {
            window.clearInterval(state.timer);
            state.requests.forEach(controller => controller.abort());
        });
        window.addEventListener('pageshow', event => { if (event.persisted && state.user) refreshWorkspace(); });
    }

    async function init() {
        let preference = 'system';
        try { preference = localStorage.getItem('alphapilot-workspace-theme') || 'system'; } catch (_) { /* Optional preference. */ }
        applyTheme(preference);
        bindEvents();
        renderIcons();
        await loadCapabilities();
        try {
            const data = await request('/auth/me', { auth: true });
            if (state.mailAction) { profile(data); showAuth(); }
            else await enterWorkspace(data);
        } catch (error) {
            showAuth(error.status === 401 ? '' : error.message);
        }
        state.timer = window.setInterval(() => {
            if (!document.hidden && state.user && !state.mutation) refreshWorkspace();
        }, 30000);
    }

    init().catch(() => showAuth('工作台暂时无法打开，请刷新页面重试。'));
})();
