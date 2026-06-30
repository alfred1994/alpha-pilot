export class ControlTab {
    constructor(app) {
        this.app = app;
        this.init();
    }

    init() {
        const pauseBtn = document.getElementById('btn-pause');
        const resumeBtn = document.getElementById('btn-resume');
        if (!pauseBtn || !resumeBtn) {
            return;
        }

        pauseBtn.addEventListener('click', () => this.setTradingState(true));
        resumeBtn.addEventListener('click', () => this.setTradingState(false));

        const tokenInput = document.getElementById('control-token');
        if (tokenInput) {
            tokenInput.value = localStorage.getItem('control_token') || '';
            tokenInput.addEventListener('input', (e) => {
                localStorage.setItem('control_token', e.target.value.trim());
            });
        }
    }

    async load() {
        const data = this.app.globalData;
        if (!data) return;

        const isPaused = data.control?.paused;
        const stateLabel = document.getElementById('control-state-label');
        const pauseBtn = document.getElementById('btn-pause');
        const resumeBtn = document.getElementById('btn-resume');
        const reasonGroup = document.getElementById('reason-group');
        if (!stateLabel || !pauseBtn || !resumeBtn || !reasonGroup) {
            return;
        }
        
        if (isPaused) {
            stateLabel.textContent = `自动交易动作已暂停 (${data.control?.reason || '人工暂停'})`;
            stateLabel.className = 'value red-text';
            pauseBtn.style.display = 'none';
            resumeBtn.style.display = 'flex';
            reasonGroup.style.display = 'none';
        } else {
            stateLabel.textContent = '自动交易动作运行中';
            stateLabel.className = 'value green-text';
            pauseBtn.style.display = 'flex';
            resumeBtn.style.display = 'none';
            reasonGroup.style.display = 'flex';
        }
    }

    async setTradingState(pause) {
        const actionText = pause ? '暂停' : '恢复';
        const confirmed = confirm(`⚠️ 请确认：您确定要执行 [${actionText}] 自动交易系统吗？\n\n该操作会立即向系统发送指令，变更盯盘自动循环。`);
        if (!confirmed) {
            return;
        }

        const url = `${this.app.apiBase}/control/${pause ? 'pause' : 'resume'}`;
        const reason = document.getElementById('pause-reason').value || 'Web端手动暂停';
        
        const body = pause ? JSON.stringify({ reason }) : JSON.stringify({});
        
        const headers = { 'Content-Type': 'application/json' };
        const token = (localStorage.getItem('control_token') || '').trim();
        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }

        try {
            const res = await fetch(url, {
                method: 'POST',
                headers,
                body
            });
            
            if (res.status === 401) {
                alert("控制 Token 校验失败，请检查控制面板的 Token 设置！");
                return;
            }

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
