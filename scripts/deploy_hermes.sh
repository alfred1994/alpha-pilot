#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${1:-/home/ubuntu/projects/quant-pilot}"
BRANCH="${2:-main}"
USER_UNITS="${3:-quant-pilot-auto.service quant-pilot-auto-restart.timer quant-pilot-doctor.timer quant-pilot-report.timer quant-pilot-status.timer}"
SYSTEM_UNITS="${4:-}"
PYTHON_CMD="${5:-python3}"
RUN_INSTALL_SYSTEMD="${6:-true}"
REPO_URL="${7:-}"
REPO_TOKEN="${8:-}"
RESTART_WEB="${9:-true}"
WEB_HOST="${10:-0.0.0.0}"
WEB_PORT="${11:-8000}"
HERMES_ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"

log() {
  printf '[deploy-hermes] %s\n' "$*"
}

git_auth() {
  if [ -n "$REPO_TOKEN" ] && [ -n "$REPO_URL" ]; then
    auth="$(printf 'x-access-token:%s' "$REPO_TOKEN" | base64 | tr -d '\n')"
    git -c "http.https://github.com/.extraheader=AUTHORIZATION: basic $auth" "$@"
  else
    git "$@"
  fi
}

copy_if_exists() {
  src="$1"
  dst="$2"
  if [ -e "$src" ]; then
    mkdir -p "$(dirname "$dst")"
    rm -rf "$dst"
    cp -a "$src" "$dst"
    log "preserved runtime path: ${dst#$PROJECT_DIR/}"
  fi
}

restore_runtime_state() {
  backup_dir="$1"
  [ -d "$backup_dir" ] || return 0

  copy_if_exists "$backup_dir/.venv" "$PROJECT_DIR/.venv"
  copy_if_exists "$backup_dir/venv" "$PROJECT_DIR/venv"
  copy_if_exists "$backup_dir/logs" "$PROJECT_DIR/logs"
  copy_if_exists "$backup_dir/data/reviews" "$PROJECT_DIR/data/reviews"

  for runtime_file in \
    data/quant.db \
    data/quant.db-shm \
    data/quant.db-wal \
    data/paper_account.json \
    data/signal_cache.json \
    data/adaptive_state.json \
    data/latest_crash.json \
    data/auto_control.json \
    data/auto_state.json; do
    copy_if_exists "$backup_dir/$runtime_file" "$PROJECT_DIR/$runtime_file"
  done
}

ensure_hermes_web_env() {
  mkdir -p "$(dirname "$HERMES_ENV_FILE")"
  touch "$HERMES_ENV_FILE"
  chmod 600 "$HERMES_ENV_FILE"

  if ! grep -q '^ALPHAPILOT_CONTROL_TOKEN=' "$HERMES_ENV_FILE"; then
    token="$("$VENV_PY" - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
    {
      printf '\n'
      printf 'ALPHAPILOT_CONTROL_TOKEN=%s\n' "$token"
    } >> "$HERMES_ENV_FILE"
    log "generated ALPHAPILOT_CONTROL_TOKEN in $HERMES_ENV_FILE"
  fi

  if ! grep -q '^ALPHAPILOT_ENV=' "$HERMES_ENV_FILE"; then
    printf 'ALPHAPILOT_ENV=production\n' >> "$HERMES_ENV_FILE"
  fi
  if ! grep -q '^PRODUCTION=' "$HERMES_ENV_FILE"; then
    printf 'PRODUCTION=true\n' >> "$HERMES_ENV_FILE"
  fi
  if ! grep -q '^ALPHAPILOT_CORS_ORIGINS=' "$HERMES_ENV_FILE"; then
    printf 'ALPHAPILOT_CORS_ORIGINS=https://alphapilot.pp.ua\n' >> "$HERMES_ENV_FILE"
  fi
}

prepare_git_repository() {
  if [ -d "$PROJECT_DIR/.git" ]; then
    return 0
  fi

  if [ -z "$REPO_URL" ]; then
    log "project is not a git repository and repo url is empty: $PROJECT_DIR"
    exit 20
  fi

  parent_dir="$(dirname "$PROJECT_DIR")"
  mkdir -p "$parent_dir"
  backup_dir=""

  if [ -e "$PROJECT_DIR" ]; then
    backup_dir="${PROJECT_DIR}.pre-git.$(date '+%Y%m%d%H%M%S')"
    log "move existing non-git project to $backup_dir"
    mv "$PROJECT_DIR" "$backup_dir"
  fi

  log "clone $REPO_URL to $PROJECT_DIR"
  git_auth clone --branch "$BRANCH" --single-branch "$REPO_URL" "$PROJECT_DIR"
  restore_runtime_state "$backup_dir"
}

restart_user_units() {
  systemctl --user daemon-reload
  for unit in $USER_UNITS; do
    if systemctl --user list-unit-files "$unit" --no-pager 2>/dev/null | grep -q "$unit"; then
      log "restart user unit: $unit"
      systemctl --user restart "$unit"
    else
      log "skip missing user unit: $unit"
    fi
  done
}

restart_system_units() {
  if [ -z "$SYSTEM_UNITS" ]; then
    return 0
  fi

  for unit in $SYSTEM_UNITS; do
    if systemctl list-unit-files "$unit" --no-pager 2>/dev/null | grep -q "$unit"; then
      log "restart system unit: $unit"
      sudo -n systemctl restart "$unit"
    else
      log "skip missing system unit: $unit"
    fi
  done
}

stop_stale_auto_processes() {
  log "stop stale auto driver processes before systemd restart"
  pkill -u "$(id -u)" -f "main.py --auto" 2>/dev/null || true
}

restart_web_process() {
  if [ "$RESTART_WEB" != "true" ]; then
    log "skip web dashboard restart"
    return 0
  fi

  log "restart web dashboard on ${WEB_HOST}:${WEB_PORT}"
  systemctl --user stop quant-pilot-web.service 2>/dev/null || true
  pkill -u "$(id -u)" -f "main.py --web" 2>/dev/null || true
  sleep 1

  if command -v systemd-run >/dev/null 2>&1; then
    web_cmd="cd '$PROJECT_DIR' && set -a && [ -f '$HERMES_ENV_FILE' ] && . '$HERMES_ENV_FILE'; set +a; export BROKER_MODE=paper PYTHONUNBUFFERED=1 ALPHAPILOT_ENV=\"\${ALPHAPILOT_ENV:-production}\" ENV=\"\${ENV:-production}\" PRODUCTION=\"\${PRODUCTION:-true}\"; exec '$VENV_PY' main.py --web --host '$WEB_HOST' --port '$WEB_PORT'"
    systemd-run --user \
      --unit=quant-pilot-web \
      --collect \
      --working-directory="$PROJECT_DIR" \
      --setenv=BROKER_MODE=paper \
      --setenv=PYTHONUNBUFFERED=1 \
      /usr/bin/env bash -lc "$web_cmd"
  else
    (
      set -a
      [ -f "$HERMES_ENV_FILE" ] && . "$HERMES_ENV_FILE"
      set +a
      export BROKER_MODE=paper PYTHONUNBUFFERED=1
      export ALPHAPILOT_ENV="${ALPHAPILOT_ENV:-production}"
      export ENV="${ENV:-production}"
      export PRODUCTION="${PRODUCTION:-true}"
      nohup "$VENV_PY" main.py --web --host "$WEB_HOST" --port "$WEB_PORT" >> logs/web.log 2>&1 &
    )
  fi

  for attempt in $(seq 1 20); do
    if curl -fsS --max-time 3 "http://127.0.0.1:${WEB_PORT}/" >/dev/null; then
      log "web dashboard is accepting requests"
      return 0
    fi
    log "waiting for web dashboard (${attempt}/20)"
    sleep 1
  done

  log "web dashboard did not become ready"
  return 30
}

prepare_git_repository

cd "$PROJECT_DIR"
mkdir -p logs

log "deploy branch $BRANCH in $PROJECT_DIR"
git_auth fetch --prune origin "$BRANCH"
git checkout -B "$BRANCH" "origin/$BRANCH"
git reset --hard "origin/$BRANCH"

if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  VENV_PY="$PROJECT_DIR/.venv/bin/python"
elif [ -x "$PROJECT_DIR/venv/bin/python" ]; then
  VENV_PY="$PROJECT_DIR/venv/bin/python"
else
  log "create .venv"
  "$PYTHON_CMD" -m venv "$PROJECT_DIR/.venv"
  VENV_PY="$PROJECT_DIR/.venv/bin/python"
fi

log "install python dependencies"
"$VENV_PY" -m pip install -r requirements.txt

log "compile python modules"
"$VENV_PY" -m compileall -q main.py config.py data execution review risk scheduler signals strategy web

if [ "$RUN_INSTALL_SYSTEMD" = "true" ]; then
  log "regenerate linux tasks"
  "$VENV_PY" main.py --linux-tasks --python-cmd "$VENV_PY"
  log "install systemd user units"
  bash data/linux_tasks/install_systemd_user.sh
else
  log "skip linux task regeneration and installer"
fi

ensure_hermes_web_env
stop_stale_auto_processes
restart_user_units
restart_system_units
restart_web_process

log "write agent status snapshot"
"$VENV_PY" main.py --agent-status > logs/deploy_agent_status.json

log "health check"
"$VENV_PY" main.py --health

log "deployment complete"
