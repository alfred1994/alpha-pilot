export class HealthTab {
    constructor(app) { this.app = app; }

    escape(value) {
        return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    async load() {
        const data = this.app.globalData || {};
        this.renderCapabilities(data.capabilities || []);
        this.renderWarnings(data.risk_warnings || []);
        this.renderEvents(data.recent_logs || []);
    }

    renderCapabilities(capabilities) {
        const container = document.getElementById('health-capability-grid');
        if (!container) return;
        if (!capabilities.length) {
            container.innerHTML = '<div class="empty-state">暂无能力状态</div>';
            return;
        }
        const labels = { healthy: '正常', degraded: '降级', idle: '待命', pending: '待运行' };
        container.innerHTML = capabilities.map(item => `
            <article class="health-card ${this.escape(item.status)}">
                <header><small>${this.escape(labels[item.status] || item.status)}</small><span></span></header>
                <h2>${this.escape(item.label)}</h2><p>${this.escape(item.summary)}</p>
            </article>
        `).join('');
    }

    renderWarnings(warnings) {
        const container = document.getElementById('health-warning-list');
        if (!container) return;
        if (!warnings.length) {
            container.innerHTML = '<div class="empty-state">当前没有需要关注的运行提醒</div>';
            return;
        }
        container.innerHTML = warnings.map(item => `<div class="fact-item"><span class="fact-badge failed">关注</span><div><strong>${this.escape(item)}</strong></div></div>`).join('');
    }

    renderEvents(events) {
        const container = document.getElementById('health-event-list');
        if (!container) return;
        if (!events.length) {
            container.innerHTML = '<div class="empty-state">暂无自动循环动作记录</div>';
            return;
        }
        container.innerHTML = events.map(item => `
            <div class="timeline-item"><time>${this.escape(item.time || '-')}</time><p><strong>${this.escape(item.status || item.type || '')}</strong><br>${this.escape(item.action || '-')}</p></div>
        `).join('');
    }
}
