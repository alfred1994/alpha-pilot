export class ControlTab {
    constructor(app) {
        this.app = app;
        this.init();
    }

    init() {
        document.getElementById('btn-pause').addEventListener('click', () => this.setTradingState(true));
        document.getElementById('btn-resume').addEventListener('click', () => this.setTradingState(false));
    }

    async load() {
        const data = this.app.globalData;
        if (!data) return;

        const isPaused = data.control?.paused;
        const stateLabel = document.getElementById('control-state-label');
        
        if (isPaused) {
            stateLabel.textContent = `自动交易动作已暂停 (${data.control?.reason || '人工暂停'})`;
            stateLabel.className = 'value red-text';
            document.getElementById('btn-pause').style.display = 'none';
            document.getElementById('btn-resume').style.display = 'flex';
            document.getElementById('reason-group').style.display = 'none';
        } else {
            stateLabel.textContent = '自动交易动作运行中';
            stateLabel.className = 'value green-text';
            document.getElementById('btn-pause').style.display = 'flex';
            document.getElementById('btn-resume').style.display = 'none';
            document.getElementById('reason-group').style.display = 'flex';
        }
    }

    async setTradingState(pause) {
        const url = `${this.app.apiBase}/control/${pause ? 'pause' : 'resume'}`;
        const reason = document.getElementById('pause-reason').value || 'Web端手动暂停';
        
        const body = pause ? JSON.stringify({ reason }) : JSON.stringify({});
        
        try {
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body
            });
            const result = await res.json();
            if (result.success) {
                await this.app.loadGlobalStatus();
                this.load();
                if (!pause) {
                    document.getElementById('pause-reason').value = '';
                }
            } else {
                alert(`控制操作失败: ${result.error}`);
            }
        } catch (e) {
            alert(`请求网络异常: ${e}`);
        }
    }
}
