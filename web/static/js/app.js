import { DashboardTab } from './modules/dashboard.js?v=2026080201';
import { DecisionsTab } from './modules/decisions.js?v=2026080201';
import { EvolutionTab } from './modules/evolution.js?v=2026080201';
import { HealthTab } from './modules/health.js?v=2026080201';

class App {
    constructor() {
        this.apiBase = '/api';
        this.currentTab = 'dashboard';
        this.globalData = null;
        this.timer = null;
        this.tabs = {
            dashboard: new DashboardTab(this),
            decisions: new DecisionsTab(this),
            evolution: new EvolutionTab(this),
            health: new HealthTab(this),
        };
        this.init();
    }

    async init() {
        document.querySelectorAll('.nav-btn').forEach(button => button.addEventListener('click', () => this.switchTab(button.dataset.tab)));
        await this.refresh();
        this.timer = window.setInterval(() => this.refresh(), 15000);
        if (window.lucide) window.lucide.createIcons();
    }

    switchTab(name) {
        if (!this.tabs[name]) return;
        document.querySelectorAll('.nav-btn').forEach(button => button.classList.toggle('active', button.dataset.tab === name));
        document.querySelectorAll('.tab-content').forEach(section => section.classList.toggle('active', section.id === `tab-${name}`));
        this.currentTab = name;
        this.tabs[name].load();
    }

    async refresh() {
        try {
            const response = await fetch(`${this.apiBase}/public/status`);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            this.globalData = await response.json();
            this.renderHeader(this.globalData);
            await this.tabs[this.currentTab].load();
        } catch (error) {
            console.error('Failed to load global status:', error);
            this.renderUnavailable();
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
        this.setText('header-regime', this.regimeLabel(data.adaptive?.regime));
        this.setText('header-assets', `净值 ￥${Number(data.account?.total_assets || 0).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`);
        this.setText('footer-update-time', `最后更新 ${this.formatTime(data.timestamp)}`);

        const alert = document.getElementById('autopilot-alert-bar');
        const warnings = data.risk_warnings || [];
        if (alert) alert.hidden = warnings.length === 0;
        this.setText('alert-message', warnings.join('；'));
    }

    renderUnavailable() {
        const dot = document.getElementById('status-pulse');
        if (dot) dot.className = 'state-dot danger';
        this.setText('header-trader-state', '状态暂不可用');
        this.setText('trader-headline', '暂时无法读取 AI 交易员状态');
        this.setText('trader-explanation', '公开状态接口没有返回有效数据，请稍后刷新。');
    }

    regimeLabel(regime) {
        return { bull: '牛市环境', bear: '熊市环境', sideways: '震荡环境', rebound: '反弹环境' }[regime] || '市场环境待识别';
    }

    formatTime(value) {
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? '-' : date.toLocaleString('zh-CN', { hour12: false });
    }

    setText(id, value) {
        const element = document.getElementById(id);
        if (element) element.textContent = value;
    }
}

window.addEventListener('DOMContentLoaded', () => { window.app = new App(); });
