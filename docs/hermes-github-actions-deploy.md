# Hermes GitHub Actions 自动部署

日期：2026-06-30
执行者：Codex

本文档说明 `main` 分支推送后自动部署 Quant Pilot 到 Hermes 所在 Linux 服务器的契约。

## 触发方式

`.github/workflows/deploy-hermes.yml` 会在以下场景触发：

- 推送到 `main`
- GitHub Actions 页面手动执行 `Deploy Hermes Server`

## GitHub Secrets

仓库 Settings -> Secrets and variables -> Actions -> Repository secrets 至少配置：

| 名称 | 用途 |
| --- | --- |
| `HERMES_HOST` | Hermes 服务器 SSH 地址，例如 `alphapilot.pp.ua` 或服务器公网 IP |
| `HERMES_USER` | 运行 Quant Pilot 的 Linux 用户，例如 `ubuntu` |
| `HERMES_SSH_PRIVATE_KEY` | GitHub Actions 连接服务器使用的私钥内容，推荐 |
| `HERMES_PASSWORD` | SSH 密码，未配置私钥时使用 |
| `HERMES_REPO_TOKEN` | 可选，服务器首次从私有仓库 clone 时使用；默认使用 workflow 的 `GITHUB_TOKEN` |

推荐使用 `HERMES_SSH_PRIVATE_KEY`。如果暂时沿用密码登录，可配置 `HERMES_PASSWORD`，workflow 会在 runner 中临时安装 `sshpass` 后部署。
使用私钥时，服务器的 `~/.ssh/authorized_keys` 需要包含上述私钥对应的公钥。

## GitHub Variables

仓库 Settings -> Secrets and variables -> Actions -> Variables 可选配置：

| 名称 | 默认值 | 用途 |
| --- | --- | --- |
| `HERMES_SSH_PORT` | `22` | SSH 端口 |
| `HERMES_PROJECT_DIR` | `/home/ubuntu/projects/quant-pilot` | 服务器项目目录 |
| `HERMES_PYTHON_CMD` | `python3` | 创建虚拟环境时使用的 Python 命令 |
| `HERMES_RUN_INSTALL_SYSTEMD` | `true` | 是否部署时重新生成并安装 systemd user 任务 |
| `HERMES_USER_UNITS` | `quant-pilot-auto.service quant-pilot-auto-restart.timer quant-pilot-doctor.timer quant-pilot-report.timer quant-pilot-status.timer` | 部署后重启的 systemd user 单元 |
| `HERMES_SYSTEM_UNITS` | 空 | 需要 `sudo -n systemctl restart` 的系统级服务，例如仪表盘服务 |
| `HERMES_HEALTH_URL` | `https://alphapilot.pp.ua/api/status` | 部署后从 GitHub runner 侧检查的公网状态接口 |
| `HERMES_RESTART_WEB` | `true` | 是否在部署后重启 `main.py --web` 仪表盘进程 |
| `HERMES_WEB_HOST` | `0.0.0.0` | Web 仪表盘监听地址 |
| `HERMES_WEB_PORT` | `8000` | Web 仪表盘监听端口 |

如果仪表盘是系统级服务，建议把实际服务名加入 `HERMES_SYSTEM_UNITS`，例如：

```text
quant-pilot-dashboard.service quant-pilot-fastapi-server.service
```

此时 Hermes 用户需要具备这些服务的免密 `sudo systemctl restart` 权限。

## 服务器前置条件

服务器项目目录推荐是 Git 仓库，并且服务器本机可以拉取私有 GitHub 仓库：

```bash
cd /home/ubuntu/projects/quant-pilot
git fetch origin main
```

如果服务器目录是旧迁移包、还没有 `.git`，部署脚本会先把旧目录移动到
`/home/ubuntu/projects/quant-pilot.pre-git.<timestamp>`，再从 GitHub clone 新目录，并保留以下运行态文件：

- `.venv` / `venv`
- `logs/`
- `data/quant.db*`
- `data/reviews/`
- `data/paper_account.json`
- `data/signal_cache.json`
- `data/adaptive_state.json`
- `data/latest_crash.json`
- `data/auto_control.json`
- `data/auto_state.json`

部署脚本会在服务器上执行：

```bash
git fetch --prune origin main
git checkout -B main origin/main
git reset --hard origin/main
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m compileall -q main.py config.py data execution review risk scheduler signals strategy web
.venv/bin/python main.py --linux-tasks --python-cmd /home/ubuntu/projects/quant-pilot/.venv/bin/python
bash data/linux_tasks/install_systemd_user.sh
systemctl --user restart ...
systemd-run --user --unit=quant-pilot-web ... python main.py --web --host 0.0.0.0 --port 8000
python main.py --agent-status > logs/deploy_agent_status.json
python main.py --health
```

## 手动验证

一次部署完成后，在服务器执行：

```bash
cd /home/ubuntu/projects/quant-pilot
git log --oneline -n 3
systemctl --user status quant-pilot-auto.service
python3 main.py --agent-status
curl -fsS https://alphapilot.pp.ua/api/status
```

## 失败排查

- `HERMES_HOST is required`：GitHub secret 没配置。
- `Permission denied (publickey)`：服务器 `authorized_keys` 未加入部署公钥，或 secret 私钥不匹配。
- `git fetch` 失败：服务器本机没有私有仓库读取权限，需要配置 GitHub deploy key 或服务器侧 GitHub token。
- `sudo: a password is required`：`HERMES_SYSTEM_UNITS` 配了系统级服务，但 Hermes 用户没有免密重启权限。
- 公网状态接口还是旧结构：Web 仪表盘服务没有被纳入 `HERMES_USER_UNITS` 或 `HERMES_SYSTEM_UNITS` 重启列表。
