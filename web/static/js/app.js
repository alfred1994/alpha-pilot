import { DashboardTab } from './modules/dashboard.js';
import { DecisionsTab } from './modules/decisions.js';
import { EvolutionTab } from './modules/evolution.js';
import { ControlTab } from './modules/control.js';

class App {
    constructor() {
        this.tabs = {};
        this.currentTab = 'dashboard';
        this.updateTimer = null;
        this.apiBase = '/api';

        this.init();
    }

    async init() {
        this.tabs['dashboard'] = new DashboardTab(this);
        this.tabs['decisions'] = new DecisionsTab(this);
        this.tabs['evolution'] = new EvolutionTab(this);
        this.tabs['control'] = new ControlTab(this);

        document.querySelectorAll('.nav-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const targetTab = e.currentTarget.getAttribute('data-tab');
                this.switchTab(targetTab);
            });
        });

        await this.loadGlobalStatus();
        await this.tabs[this.currentTab].load();

        this.startPoll();
    }

    switchTab(tabName) {
        if (this.currentTab === tabName) return;

        document.querySelectorAll('.nav-btn').forEach(btn => {
            btn.classList.toggle('active', btn.getAttribute('data-tab') === tabName);
        });

        document.querySelectorAll('.tab-content').forEach(content => {
            content.classList.toggle('active', content.getAttribute('id') === `tab-${tabName}`);
        });

        this.currentTab = tabName;
        this.tabs[this.currentTab].load();
    }

    async loadGlobalStatus() {
        try {
            const res = await fetch(`${this.apiBase}/status`);
            const data = await res.json();
            
            const regimeStr = data.adaptive?.regime || (data.globalData?.regime) || 'sideways';
            document.getElementById('header-regime').textContent = this.formatRegime(regimeStr);
            document.getElementById('header-assets').textContent = `￥${data.account?.total_assets?.toLocaleString('zh-CN', {minimumFractionDigits:0, maximumFractionDigits:0}) || '-'}`;
            
            const pulse = document.getElementById('status-pulse');
            if (data.health?.ok && data.watchdog?.ok) {
                pulse.className = 'pulse-dot green';
            } else {
                pulse.className = 'pulse-dot red';
            }

            const timeStr = new Date(data.timestamp).toLocaleTimeString();
            document.getElementById('footer-update-time').textContent = `最后更新: ${timeStr}`;

            this.globalData = data;
        } catch (e) {
            console.error('Failed to load global status:', e);
        }
    }

    formatRegime(regime) {
        return {
            'bull': '🟢 牛市环境',
            'bear': '🔴 熊市环境',
            'sideways': '⚪ 震荡环境',
            'rebound': '🔵 超跌反弹'
        }[regime] || `⚪ ${regime}环境`;
    }

    startPoll() {
        this.stopPoll();
        this.updateTimer = setInterval(async () => {
            await this.loadGlobalStatus();
            this.tabs[this.currentTab].load();
        }, 10000);
    }

    stopPoll() {
        if (this.updateTimer) {
            clearInterval(this.updateTimer);
            this.updateTimer = null;
        }
    }
}

window.addEventListener('DOMContentLoaded', () => {
    window.app = new App();
});
