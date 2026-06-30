# Demo and Safe Exploration

Quant Pilot is easiest to understand when viewed as a running cockpit. The current
project still expects real market-data and LLM credentials for the full loop, but
you can inspect the main surfaces safely in paper mode.

## Public Demo

The maintainer-operated public read-only dashboard is available at:

<https://alphapilot.pp.ua>

It shows a live paper-trading cockpit and hides all control actions.

## Local Web Server

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
python3 main.py --web --host 127.0.0.1 --port 8000
```

Then open:

<http://127.0.0.1:8000>

If you do not have market-data credentials, some sections may show empty states.
This is expected until a sample-data mode is added.

## Paper-Only Checks

```bash
python3 main.py --health
python3 main.py --paper-ready --unattended-platform linux
python3 tests/test_web_public_dashboard.py
python3 tests/test_auto_control.py
python3 tests/test_auto_watchdog.py
```

## Planned Zero-Key Demo Mode

The roadmap includes a zero-key demo mode that will seed sample account snapshots,
positions, trades, LLM decisions, and lessons so contributors can evaluate the
dashboard and scheduler contracts without LongBridge, Telegram, or LLM credentials.

Until that lands, keep experiments in paper mode and avoid real broker adapters.
