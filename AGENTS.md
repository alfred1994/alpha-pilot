# AGENTS.md

Guidance for AI coding agents working on Quant Pilot.

Quant Pilot is a research and paper-trading system for A-share market automation.
Keep changes conservative, observable, and safe by default.

## Core Principles

- Preserve `BROKER_MODE=paper` as the default documented mode.
- Do not introduce real-money execution paths without explicit design review.
- Do not commit secrets, runtime databases, logs, private server details, or account
  snapshots.
- Keep the public dashboard read-only in production.
- Prefer standard SDKs and maintained ecosystem packages over custom infrastructure.
- Keep implementation style consistent with the existing Python modules and tests.
- Add or update tests when changing trading decisions, scheduler behavior, risk,
  persistence, web security, or deployment contracts.

## Useful Local Commands

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

Some checks require market-data or LLM credentials. If a test cannot run locally,
record the reason and the residual risk.

## Web and Security Notes

Production web mode should use:

```bash
ALPHAPILOT_ENV=production
BROKER_MODE=paper
```

The production dashboard must not expose:

- control buttons
- control tokens
- raw environment variables
- internal file paths
- raw tracebacks
- shell repair commands
- broker credentials

Control APIs should stay unmounted in production unless explicitly enabled for a
private network.

## Documentation Expectations

User-facing behavior changes should update README or `docs/`. Risky operational
changes should mention rollback and verification steps.

## Git Hygiene

- Keep commits focused.
- Do not rewrite unrelated user changes.
- Do not remove safety checks or redaction layers to make a test pass.
- Avoid committing generated runtime files under `data/`, `logs/`, or `.codex/`.
