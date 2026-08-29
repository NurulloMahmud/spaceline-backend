#!/usr/bin/env bash
#
# Deploy Spaceline onto this server.
#
# Four supervisor programs make up Spaceline, and three of them — box,
# email_agent, agent_bot — are directories inside this one git repo, so the
# pull happens once here rather than once per service. The fourth, agent, is
# a separate repo (makhammatovb/atrek) checked out inside this one, and it
# already ships its own deploy.sh that pulls, builds the Go binary and
# restarts itself; this script calls that rather than reimplementing it.
#
# cosmos and celery also run under supervisor on this box. They belong to a
# different project (/home/api/cosmos) and are deliberately never touched.
#
# Git auth comes from the credential store configured for root
# (credential.helper=store, /root/.git-credentials), so no token appears here.
#
# Usage:
#   ./deploy.sh                       everything
#   ./deploy.sh box email_agent       only these
#   ./deploy.sh --dry-run             show what would happen, change nothing
#   ./deploy.sh --collectstatic       also push static files to S3 (slow)
#
set -euo pipefail

REPO_DIR=/home/api/spaceline
AGENT_DIR="$REPO_DIR/agent"
ALL_COMPONENTS=(box email_agent agent_bot agent)

DRY_RUN=false
COLLECTSTATIC=false
COMPONENTS=()

for arg in "$@"; do
    case "$arg" in
        --dry-run)       DRY_RUN=true ;;
        --collectstatic) COLLECTSTATIC=true ;;
        -h|--help)
            sed -n '3,25p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        -*)
            echo "Unknown option: $arg" >&2
            exit 2
            ;;
        *)
            if [[ " ${ALL_COMPONENTS[*]} " != *" $arg "* ]]; then
                echo "Unknown component: $arg (valid: ${ALL_COMPONENTS[*]})" >&2
                exit 2
            fi
            COMPONENTS+=("$arg")
            ;;
    esac
done

if [ ${#COMPONENTS[@]} -eq 0 ]; then
    COMPONENTS=("${ALL_COMPONENTS[@]}")
fi

selected() { [[ " ${COMPONENTS[*]} " == *" $1 "* ]]; }

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
fail() { printf '\n\033[31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

run() {
    if $DRY_RUN; then
        info "[dry-run] $*"
    else
        "$@"
    fi
}

# Poll a health endpoint after a restart. A service that comes back up and
# then dies on its first request looks identical to a healthy one if you only
# check supervisor's status, so every HTTP service is actually spoken to.
wait_healthy() {
    local name=$1 log_file=$2
    shift 2
    if $DRY_RUN; then
        info "[dry-run] health check $name"
        return 0
    fi
    for _ in $(seq 1 20); do
        sleep 1
        if "$@" >/dev/null 2>&1; then
            info "$name is healthy"
            return 0
        fi
    done
    echo >&2
    supervisorctl status "$name" >&2 || true
    [ -f "$log_file" ] && tail -30 "$log_file" >&2
    fail "$name did not come back up"
}

# agent_bot talks to Telegram, not to us, so there is nothing to curl. A crash
# loop still shows up here: supervisor reports BACKOFF/FATAL rather than
# RUNNING once the process has failed to stay alive.
wait_running() {
    local name=$1 log_file=$2
    if $DRY_RUN; then
        info "[dry-run] status check $name"
        return 0
    fi
    sleep 5
    local status
    status=$(supervisorctl status "$name" || true)
    if [[ "$status" != *RUNNING* ]]; then
        echo >&2
        echo "$status" >&2
        [ -f "$log_file" ] && tail -30 "$log_file" >&2
        fail "$name is not running"
    fi
    info "$status"
}

# --- Pull -----------------------------------------------------------------
# agent pulls its own repo from inside its own deploy.sh, so a run that only
# touches agent has nothing to pull here.
if [ "${COMPONENTS[*]}" != "agent" ]; then
    log "Pulling $REPO_DIR"
    cd "$REPO_DIR"
    before=$(git rev-parse HEAD)
    run git pull --ff-only origin main
    after=$(git rev-parse HEAD)

    if [ "$before" = "$after" ]; then
        info "already up to date at $(git log --oneline -1)"
    else
        git --no-pager log --oneline "$before..$after" | sed 's/^/    /'
    fi
fi

# --- box (Django API) -----------------------------------------------------
if selected box; then
    log "Deploying box (Django API)"
    cd "$REPO_DIR/boxTruck"
    run ./venv/bin/pip install -q -r requirements.txt
    run ./venv/bin/python manage.py migrate --noinput

    # Static files live on S3, so collectstatic is a slow upload rather than a
    # local copy. It is opt-in: most deploys do not change a static asset.
    if $COLLECTSTATIC; then
        run ./venv/bin/python manage.py collectstatic --noinput
    fi

    run supervisorctl restart box
    wait_healthy box /var/log/boxTruck.err.log \
        curl -sf --unix-socket "$REPO_DIR/boxTruck/boxTruck.sock" http://localhost/
fi

# --- email_agent (FastAPI) ------------------------------------------------
if selected email_agent; then
    log "Deploying email_agent"
    cd "$REPO_DIR/email-agent"
    run ./venv/bin/pip install -q -r requirements.txt
    run supervisorctl restart email_agent
    wait_healthy email_agent /var/log/email_agent.err.log \
        curl -sf http://127.0.0.1:8100/api/v1/health
fi

# --- agent_bot (Telegram bot) ---------------------------------------------
if selected agent_bot; then
    log "Deploying agent_bot"
    cd "$REPO_DIR/agent_bot"
    run ./venv/bin/pip install -q -r requirements.txt
    run supervisorctl restart agent_bot
    wait_running agent_bot /var/log/agent_bot.err.log
fi

# --- agent (Go) -----------------------------------------------------------
if selected agent; then
    log "Deploying agent (delegating to its own deploy.sh)"
    if [ ! -x "$AGENT_DIR/deploy.sh" ]; then
        fail "$AGENT_DIR/deploy.sh is missing or not executable"
    fi
    run "$AGENT_DIR/deploy.sh"
    wait_running agent /var/log/agent/err.log
fi

# --- Summary --------------------------------------------------------------
log "Done"
for component in "${COMPONENTS[@]}"; do
    supervisorctl status "$component" || true
done
