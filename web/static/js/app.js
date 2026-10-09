import { registerChartTheme } from './chart-theme.js?v=2026100901';
import { ReturnsTab } from './modules/returns.js?v=2026100901';
import { ResearchTab } from './modules/research.js?v=2026091702';
import { DashboardTab } from './modules/dashboard.js?v=2026100901';
import { DecisionsTab } from './modules/decisions.js?v=2026100901';
import { EvolutionTab } from './modules/evolution.js?v=2026100901';
import { HealthTab } from './modules/health.js?v=2026080201';

class App {
    constructor() {
        this.apiBase = '/api';
        this.currentTab = 'dashboard';
        this.globalData = null;
        this.timer = null;
        this.refreshInFlight = false;
        this.refreshSequence = 0;
        this.statusUnavailable = false;
        this.refreshController = null;
        this.tabs = {
            dashboard: new DashboardTab(this),
            research: new ResearchTab(this),
            returns: new ReturnsTab(this),
            decisions: new DecisionsTab(this),
            evolution: new EvolutionTab(this),
            health: new HealthTab(this),
        };
        this.init().catch(error => { console.error('Dashboard initialization failed:', error); this.renderUnavailable(); });
    }

    async init() {
        registerChartTheme();
        document.querySelectorAll('.nav-btn').forEach(button => {
            const label = button.textContent.trim();
            button.setAttribute('aria-label', label);
            button.title = label;
            button.addEventListener('click', () => this.switchTab(button.dataset.tab));
        });
        document.querySelectorAll('[data-navigate]').forEach(button => button.addEventListener('click', () => this.switchTab(button.dataset.navigate)));
        document.getElementById('refresh-dashboard')?.addEventListener('click', () => this.refresh());
        window.addEventListener('hashchange', () => this.switchTab(location.hash.slice(1) || 'dashboard', false));
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) this.refreshController?.abort();
            else this.refresh();
        });
        window.addEventListener('pagehide', () => {
            window.clearInterval(this.timer);
            this.refreshController?.abort();
        });
        this.switchTab(location.hash.slice(1) || 'dashboard', false);
        await this.refresh();
        this.timer = window.setInterval(() => { if (!document.hidden) this.refresh(); }, 15000);
        if (window.lucide) window.lucide.createIcons();
    }

    switchTab(name, updateHash = true) {
        if (!this.tabs[name]) return;
        document.querySelectorAll('.nav-btn').forEach(button => {
            button.classList.toggle('active', button.dataset.tab === name);
            if (button.dataset.tab === name) button.setAttribute('aria-current', 'page');
            else button.removeAttribute('aria-current');
        });
        this.setText('workspace-section', document.querySelector(`.nav-btn[data-tab="${name}"]`)?.textContent.trim() || '账户总览');
        document.querySelectorAll('.tab-content').forEach(section => section.classList.toggle('active', section.id === `tab-${name}`));
        this.currentTab = name;
        if (updateHash) history.replaceState(null, '', `#${name}`);
        this.loadCurrentTab();
        if (name === 'dashboard') this.tabs.research.loadMarket().catch(error => console.error('Market evidence failed:', error));
    }

    async loadCurrentTab() {
        try { await this.tabs[this.currentTab].load(); }
        catch (error) { console.error('Tab loading failed:', error); this.showDataNotice('当前页签读取失败，请刷新重试。'); }
    }

    showDataNotice(message) {
        const notice = document.getElementById('data-notice');
        if (notice) { notice.hidden = !message; notice.textContent = message || ''; }
    }

    async refresh() {
        if (this.refreshInFlight) return;
        this.refreshInFlight = true;
        const refreshButton = document.getElementById('refresh-dashboard');
        if (refreshButton) { refreshButton.disabled = true; refreshButton.setAttribute('aria-busy', 'true'); }
        const requestId = ++this.refreshSequence;
        const controller = new AbortController();
        this.refreshController = controller;
        const timeoutId = window.setTimeout(() => controller.abort(), 12000);
        try {
            const response = await fetch(`${this.apiBase}/public/status`, { signal: controller.signal });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const next = await response.json();
            if (requestId !== this.refreshSequence) return;
            this.globalData = next;
            this.statusUnavailable = false;
            this.showDataNotice('');
            this.renderHeader(this.globalData);
            this.loadCurrentTab();
        } catch (error) {
            console.error('Failed to load global status:', error);
            this.statusUnavailable = true;
            this.renderUnavailable();
            if (['research', 'returns'].includes(this.currentTab)) this.loadCurrentTab();
        } finally {
            window.clearTimeout(timeoutId);
            if (this.refreshController === controller) this.refreshController = null;
            this.refreshInFlight = false;
            if (refreshButton) { refreshButton.disabled = false; refreshButton.setAttribute('aria-busy', 'false'); }
            if (this.currentTab === 'dashboard') this.tabs.research.loadMarket().catch(error => console.error('Market evidence failed:', error));
        }
    }

    renderHeader(data) {
        const brief = data.daily_trader || {};
        const capabilities = data.capabilities || [];
        const degraded = capabilities.some(item => item.status === 'degraded');
        const critical = !data.health?.ok || !data.watchdog?.ok || data.crash_open || data.control?.paused;
        const dot = document.getElementById('status-pulse');
        if (dot) dot.className = `state-dot ${critical ? 'danger' : degraded ? 'degraded' : ''}`;
        this.setText('header-trader-state', brief.headline || '状态读取中');
        const regimeInfo = data.regime_current || null;
        const regimeText = this.regimeLabel(regimeInfo?.regime);
        const regimeDate = regimeInfo?.date ? String(regimeInfo.date).slice(5) : '';
        this.setText('header-regime', regimeText + (regimeDate ? ` · ${regimeDate}` : '') + (regimeInfo?.fresh === false ? ' · 已过期' : ''));
        this.setText('overview-regime', regimeText + (regimeInfo?.fresh === false ? ' · 已过期' : ''));
        const overviewDay = brief.date || String(data.data_as_of || data.timestamp || '').slice(0, 10);
        const overviewDate = new Date(`${overviewDay}T12:00:00+08:00`);
        this.setText('overview-date', Number.isNaN(overviewDate.getTime()) ? '交易日期待确认' : overviewDate.toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' }));
        const assets = data.account?.total_assets;
        this.setText('header-assets', data.account?.available === false || !Number.isFinite(Number(assets)) || assets == null
            ? '净值不可用' : `净值 ￥${Number(assets).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`);
        const fetchedAt = data.fetched_at || data.timestamp;
        this.setText('workspace-sync', `快照 ${this.formatTime(fetchedAt).split(' ').pop()}`);
        this.setText('footer-update-time', `快照抓取 ${this.formatTime(fetchedAt)} · 底层数据 ${this.formatTime(data.data_as_of)}`);
        const snapshotAt = data.snapshot_at || fetchedAt;
        if (snapshotAt && Date.now() - new Date(snapshotAt).getTime() > 60000) this.showDataNotice('当前状态快照已过期，数据可能不是最新。');
        if (snapshotAt && Date.now() - new Date(snapshotAt).getTime() > 60000) this.setText('workspace-sync', '快照已过期');

        const alert = document.getElementById('autopilot-alert-bar');
        const warnings = data.risk_warnings || [];
        if (alert) alert.hidden = warnings.length === 0;
        this.setText('alert-message', warnings.join('；'));
    }

    renderUnavailable() {
        this.setText('workspace-sync', '同步失败 · 数据陈旧');
        this.showDataNotice(this.globalData ? '公开状态接口暂不可达；当前页仍显示上次成功读取的数据，数据已标记陈旧。' : '公开状态接口暂不可达，当前没有可用快照。');
        if (!this.globalData) {
            this.setText('header-assets', '净值不可用');
            this.setText('header-regime', '市场环境未知');
        }
        const dot = document.getElementById('status-pulse');
        if (dot) dot.className = 'state-dot unavailable';
        this.setText('header-trader-state', '接口暂不可达 · 数据陈旧');
        if (!this.globalData) {
            this.setText('trader-headline', '暂时无法读取 AI 交易员状态');
            this.setText('trader-explanation', '公开状态接口没有返回有效数据，请稍后刷新。');
        }
    }

    regimeLabel(regime) {
        return { bull: '牛市环境', bear: '熊市环境', sideways: '震荡环境', rebound: '反弹环境' }[regime] || '市场环境待识别';
    }

    formatTime(value) {
        if (!value) return '未知';
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? '-' : date.toLocaleString('zh-CN', { hour12: false });
    }

    setText(id, value) {
        const element = document.getElementById(id);
        if (element) element.textContent = value;
    }
}

window.addEventListener('DOMContentLoaded', () => { window.app = new App(); });
