import { DashboardTab } from './modules/dashboard.js?v=2026070102';
import { DecisionsTab } from './modules/decisions.js?v=2026070102';
import { EvolutionTab } from './modules/evolution.js?v=2026070102';

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
        if (this.tabs[this.currentTab]) {
            this.tabs[this.currentTab].load();
        }
    }

    async loadGlobalStatus() {
        try {
            const res = await fetch(`${this.apiBase}/public/status`);
            const data = await res.json();
            
            const regimeStr = data.adaptive?.regime || (data.globalData?.regime) || 'sideways';
            document.getElementById('header-regime').textContent = this.formatRegime(regimeStr);
            document.getElementById('header-assets').textContent = `￥${data.account?.total_assets?.toLocaleString('zh-CN', {minimumFractionDigits:0, maximumFractionDigits:0}) || '-'}`;
            
            const pulse = document.getElementById('status-pulse');
            if (data.health?.ok && data.watchdog?.ok && !data.crash_open && !data.control?.paused) {
                pulse.className = 'pulse-dot green';
            } else {
                pulse.className = 'pulse-dot red';
            }

            // 更新头部 4 个核心状态指示灯
            const hDot = document.getElementById('indicator-health');
            if (hDot) {
                hDot.className = data.health?.ok ? 'status-indicator-dot green-dot' : 'status-indicator-dot red-dot pulse-dot';
            }
            const wdDot = document.getElementById('indicator-watchdog');
            if (wdDot) {
                wdDot.className = data.watchdog?.ok ? 'status-indicator-dot green-dot' : 'status-indicator-dot red-dot pulse-dot';
            }
            const cDot = document.getElementById('indicator-crash');
            if (cDot) {
                cDot.className = !data.crash_open ? 'status-indicator-dot green-dot' : 'status-indicator-dot red-dot pulse-dot';
            }
            const ctrlDot = document.getElementById('indicator-control');
            if (ctrlDot) {
                ctrlDot.className = !data.control?.paused ? 'status-indicator-dot green-dot' : 'status-indicator-dot orange-dot pulse-dot';
            }

            // 更新顶部警告条
            const alertBar = document.getElementById('autopilot-alert-bar');
            const alertMsg = document.getElementById('alert-message');
            if (alertBar && alertMsg) {
                if (data.risk_warnings && data.risk_warnings.length > 0) {
                    alertMsg.textContent = `自动驾驶异常报警: ${data.risk_warnings.join(' | ')}`;
                    alertBar.style.display = 'flex';
                } else {
                    alertBar.style.display = 'none';
                }
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
            if (this.tabs[this.currentTab]) {
                this.tabs[this.currentTab].load();
            }
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
