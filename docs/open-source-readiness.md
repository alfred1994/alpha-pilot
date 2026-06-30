# Open Source Readiness

Date: 2026-07-01

This checklist tracks the repository changes needed before switching the GitHub
repository from private to public.

## Completed in the open-source-prep branch

- Added Apache-2.0 license.
- Added security policy.
- Added financial disclaimer.
- Added contribution guide.
- Rewrote the README around the public value proposition.
- Added a demo and safe exploration guide.
- Made the Hermes deploy workflow skip automatically in forks.
- Removed internal Codex review output from version control.
- Added `.codex/` to `.gitignore`.
- Replaced agent instruction files with public-safe project guidance.

## Must Check Before Making Public

- Run a git history secret scan with `gitleaks`, `trufflehog`, or an equivalent tool.
- Confirm no real `.env`, database, logs, account snapshots, or API tokens are tracked.
- Confirm deployment secrets remain in GitHub Actions secrets only.
- Confirm the public dashboard stays read-only in production.
- Confirm `BROKER_MODE=paper` remains the documented default.

## Recommended Follow-Ups

- Add screenshot assets for the README.
- Add zero-key sample data mode.
- Add a Docker Compose demo.
- Publish a short architecture article explaining the "trading race car" model.
- Add release tags once the demo path is stable.
