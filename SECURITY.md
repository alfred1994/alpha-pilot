# Security Policy

AlphaPilot is a research and paper-trading project. Treat every deployment as a
financial automation system and keep the public web surface read-only by default.

## Supported Versions

Security fixes are currently handled on the default branch after the repository is
public. Until formal releases exist, use the latest `main` commit.

## Reporting a Vulnerability

Please open a private security advisory on GitHub or contact the maintainer through
the repository owner profile. Do not disclose exploitable issues publicly before a
fix is available.

Useful reports include:

- affected commit or version
- reproduction steps
- whether credentials, trading control, or account data can be exposed
- logs with secrets redacted
- suggested fix, if available

## Secret Handling

Never commit real credentials or runtime state. In particular, keep these out of
Git:

- `.env` and `.env.*`
- LongBridge credentials
- LLM API keys
- Telegram bot tokens and chat IDs
- GitHub tokens
- SSH keys
- broker credentials
- `data/*.db`
- `data/*.json`
- `logs/`

The repository includes `env.example` only as a blank template.

Before making a private fork public, scan history with tools such as `gitleaks`,
`trufflehog`, or GitHub secret scanning.

## Web Dashboard Safety

Production web mode is expected to use:

```bash
ALPHAPILOT_ENV=production
ALPHAPILOT_CORS_ORIGINS=https://your-dashboard-domain.example
BROKER_MODE=paper
```

In production mode, AlphaPilot:

- disables `/docs` and `/openapi.json`
- exposes public read-only status through `/api/public/status`
- returns redacted status from `/api/status` unless explicitly configured otherwise
- does not mount `/api/control/*` unless `ALPHAPILOT_ENABLE_CONTROL_API=true`
- hides control panel UI from the public static dashboard
- adds basic browser security headers
- redacts token-like strings, internal paths, shell commands, and tracebacks

Do not expose control endpoints to the public internet. If you intentionally enable
them for a private network, use a strong `ALPHAPILOT_CONTROL_TOKEN`, HTTPS, and
network-level access controls.

## Trading Safety

AlphaPilot defaults to paper trading. The project does not ship a production-ready
real-money broker adapter. Any real-money extension is outside the default safety
boundary and must be reviewed independently.

Use `BROKER_MODE=paper` for demos, public dashboards, and unattended experiments.
