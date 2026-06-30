# Contributing

Thanks for considering a contribution to Quant Pilot.

This project sits at the intersection of AI, market data, paper trading, and
operations automation. Please keep changes small, reviewable, and safe by default.

## Ground Rules

- Keep the default execution mode as paper trading.
- Do not add real-money execution paths without a separate design discussion.
- Do not commit secrets, account data, runtime databases, logs, or private server
  details.
- Prefer standard libraries and well-maintained ecosystem packages over custom
  infrastructure.
- Keep public web endpoints read-only unless a feature is explicitly designed as a
  private control-plane tool.
- Include tests for changes that affect trading decisions, order execution, risk,
  web security, scheduler behavior, or persistence.

## Local Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
```

Keep `BROKER_MODE=paper`.

## Useful Test Commands

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

Some tests depend on local credentials or market-data availability. If a test cannot
run in your environment, document the reason in the pull request.

## Pull Request Checklist

- [ ] The change keeps `BROKER_MODE=paper` as the documented default.
- [ ] No secrets or runtime data are included.
- [ ] Public dashboard responses remain redacted in production mode.
- [ ] Relevant tests or contract checks pass locally.
- [ ] Documentation is updated for user-facing behavior.
- [ ] Risky operational changes include rollback notes.

## Good First Contribution Areas

- Demo/sample data mode
- Dashboard polish
- Documentation improvements
- More deterministic replay fixtures
- Data-source adapter cleanup
- Test coverage for failure and recovery paths

## Reporting Issues

When opening an issue, include:

- operating system
- Python version
- command that failed
- redacted logs or traceback
- whether the issue affects paper trading, web dashboard, scheduler, or data source

Do not paste credentials, tokens, private server addresses, or account identifiers.
