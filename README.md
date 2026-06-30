# Quant Pilot

Self-healing AI paper-trading cockpit for A-shares.

Quant Pilot turns an A-share paper trading system into an autonomous trading race car:
an LLM makes trade decisions, a Linux/Hermes agent can drive the system unattended,
Watchdog and Doctor keep the loop observable and repairable, and a public read-only
dashboard shows the cockpit state without exposing control actions.

> Research and paper-trading only. Quant Pilot is not investment advice and does not
> guarantee returns. Keep `BROKER_MODE=paper` unless you fully understand the risks.

## Why This Project Exists

Most AI trading repositories focus on one of three things: notebooks, backtests, or
LLM prompts. Quant Pilot focuses on the operational loop around an AI trader:

- pre-market warmup
- intraday scan and paper execution
- stop-loss inspection
- post-market review
- LLM lesson extraction
- adaptive parameter evolution
- Watchdog monitoring
- Doctor self-healing
- Linux `systemd --user` unattended operation
- public read-only dashboard for observability

The project is intentionally opinionated around A-share paper trading, but the
architecture is useful for anyone exploring agent-operated financial automation.

## Live Cockpit

Public read-only dashboard:

- <https://alphapilot.pp.ua>

The public dashboard is designed for observability only. It does not expose pause,
resume, repair, token input, raw environment variables, or trading control endpoints.

## Feature Highlights

- **LLM trading decisions**: MiMo/Xiaomi-compatible OpenAI-style API for decision
  reasoning and trade plan generation.
- **Five-signal decision context**: technical, capital, sentiment, emotion, and
  fundamental dimensions.
- **Paper account engine**: simulated cash, positions, trades, fees, PnL, and
  account snapshots.
- **Trading memory**: post-trade reviews and lessons can feed later decisions.
- **Adaptive evolution**: low win-rate or poor recent performance tightens the next
  round of thresholds and position sizing.
- **Watchdog and Doctor**: detect broken loops, stale cycles, unresolved crashes,
  and missing closure steps.
- **Hermes/Linux mode**: generates `systemd --user` units for unattended operation.
- **Public safety layer**: production web mode redacts internal paths, commands,
  tokens, raw tracebacks, and long prompt-like reasoning.
- **Dashboard-first observability**: account, positions, risk state, LLM decisions,
  strategy parameters, lessons, and loop heartbeats.

## Architecture

```text
Market/Data Sources
  |-- Tencent realtime quotes
  |-- Eastmoney market data
  |-- LongBridge OpenAPI
  |-- Baostock / fallback historical data
          |
          v
Signal and Context Layer
  |-- technical / capital / emotion / sentiment / fundamental
  |-- market regime
  |-- memory and lessons
          |
          v
LLM Trader
  |-- decision reasoning
  |-- confidence
  |-- trade plan
          |
          v
Paper Execution and Risk
  |-- simulated orders
  |-- stop-loss / take-profit
  |-- drawdown controls
          |
          v
Operations Loop
  |-- auto trader
  |-- watchdog
  |-- doctor
  |-- daily review
          |
          v
Web Cockpit / Reports / Notifications
```

## Repository Layout

```text
config.py                 Global configuration
main.py                   CLI entrypoint
data/                     Market data, database, snapshots
signals/                  Signal calculation
strategy/                 LLM trader, memory, adaptive parameters
execution/                Paper account and order management
risk/                     Position, stop-loss, drawdown controls
review/                   Daily and LLM review
scheduler/                Auto loop, watchdog, doctor, notifications
web/                      FastAPI server and static dashboard
data/linux_tasks/         Generated Linux/Hermes systemd user task templates
docs/                     Architecture and operations docs
test_*.py                 Local contract and regression tests
```

## Quick Start

```bash
git clone https://github.com/alfred1994/quant-pilot.git
cd quant-pilot

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cp env.example .env
# Edit .env. Keep BROKER_MODE=paper.

python3 main.py --health
python3 main.py --paper-ready --unattended-platform linux
python3 main.py --auto-once
python3 main.py --web --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000> after starting the web server.

## Important Environment Variables

```bash
# LongBridge market data
LONGPORT_APP_KEY=
LONGPORT_APP_SECRET=
LONGPORT_ACCESS_TOKEN=

# LLM endpoint
XIAOMI_API_KEY=
XIAOMI_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1

# Optional notification channel
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Keep this as paper unless you are intentionally developing a broker adapter.
BROKER_MODE=paper
```

See [env.example](env.example) for the full template.

## Common Commands

```bash
python3 main.py --scan
python3 main.py --execute
python3 main.py --review
python3 main.py --stop-check
python3 main.py --health
python3 main.py --watchdog
python3 main.py --doctor
python3 main.py --ops-status
python3 main.py --agent-status
python3 main.py --ai-report
python3 main.py --auto-once
python3 main.py --auto
python3 main.py --auto-rehearse --rehearsal-days 5
python3 main.py --paper-ready --unattended-platform linux
python3 main.py --paper-bootstrap --unattended-platform linux --python-cmd python3
python3 main.py --linux-tasks
python3 main.py --linux-unattended-status
```

## Linux / Hermes Unattended Mode

Quant Pilot can generate Linux `systemd --user` tasks:

```bash
python3 main.py --linux-tasks
bash data/linux_tasks/install_systemd_user.sh
systemctl --user status quant-pilot-auto.service
```

For a full deployment overview, see
[docs/hermes-github-actions-deploy.md](docs/hermes-github-actions-deploy.md).

## Public Web Safety Model

In production mode, the FastAPI web server:

- disables `/docs` and `/openapi.json`
- returns a public redacted status snapshot by default
- exposes `/api/public/status` for the dashboard
- does not mount `/api/control/*` unless `ALPHAPILOT_ENABLE_CONTROL_API=true`
- adds browser security headers
- hides internal paths, shell commands, tracebacks, token-like strings, and long
  prompt-like reasoning from public responses

Recommended production variables:

```bash
ALPHAPILOT_ENV=production
ALPHAPILOT_CORS_ORIGINS=https://your-dashboard-domain.example
BROKER_MODE=paper
```

## Tests

The project uses local Python scripts as contract tests:

```bash
python3 -m compileall -q web scheduler strategy execution data main.py config.py
python3 test_web_public_dashboard.py
python3 test_agent_driver_contract.py
python3 test_auto_control.py
python3 test_auto_watchdog.py
python3 test_auto_doctor.py
python3 test_auto_trader.py
python3 test_paper_readiness.py
python3 test_paper_bootstrap.py
```

Run broader tests as your data-source credentials and local environment allow.

## Roadmap

- Sample/demo data mode for zero-key dashboard exploration
- Cleaner broker adapter interface
- More deterministic replay reports
- Better public benchmark story for paper-trading runs
- Dashboard screenshots and release artifacts
- Docker Compose for local cockpit demos

## How This Differs From Popular Projects

- It is not a general-purpose production trading engine like Lean or NautilusTrader.
- It is not primarily an academic RL research platform like FinRL or TradeMaster.
- It is not only a multi-agent prompt framework like TradingAgents.
- It is a paper-trading operations cockpit designed to be driven and monitored by an
  autonomous coding/ops agent.

## Contributing

Contributions are welcome after the project is made public. Please read
[CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[DISCLAIMER.md](DISCLAIMER.md) before opening issues or pull requests.

## License

Apache-2.0. See [LICENSE](LICENSE).
