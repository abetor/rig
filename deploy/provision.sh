#!/usr/bin/env bash
# Provision rig on a clean Ubuntu/Debian VM. Install its deterministic dependencies (Python, Git,
# and Node.js), then check whether the environment is ready. Agent CLI installation and login remain
# manual because they require credentials inside the VM.
#
# Run from the repository root on the VM: bash deploy/provision.sh
set -euo pipefail

say() { printf '\n=== %s ===\n' "$*"; }

# --- 0. Repository ---
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
say "rig provision · $REPO_ROOT"

# --- 1. OS packages (rig uses only the Python standard library; agent CLIs need Node.js) ---
if command -v apt-get >/dev/null 2>&1; then
  say "installing python3/git/curl/build + node (apt)"
  sudo apt-get update -y
  sudo apt-get install -y python3 git curl ca-certificates build-essential
  if ! command -v node >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo apt-get install -y nodejs
  fi
else
  echo "!! apt is unavailable - install python3, git, curl, node, and npm manually" >&2
fi

# --- 2. Version checks ---
say "versions"
python3 --version
git --version
node --version 2>/dev/null || echo "node: not found (required by the agent CLI)"

# --- 3. rig readiness (reports missing agent CLIs and credentials) ---
say "rig --init (environment readiness)"
python3 -m rig --init || true

# --- 4. Manual steps (interactive credentials cannot be automated) ---
cat <<'NEXT'

=== NEXT (manual, inside the VM) ===
1) Install at least ONE agent CLI and log in:
   • claude - install the official Anthropic CLI, then run: claude -> /login
              (or set a key: export ANTHROPIC_API_KEY=... )
   • codex  - npm i -g <codex-cli>, then:  codex login
              (or: export CODEX_API_KEY=... )
2) (optional) config and secrets in one file:
   mkdir -p ~/.rig && cp "$PWD/.env.sample" ~/.rig/.env && ${EDITOR:-nano} ~/.rig/.env
   (notification routes such as TELEGRAM_*/WEBHOOK_URL and defaults such as RIG_WORKER_MODEL)
3) Recheck:  python3 -m rig --init
4) Run:      python3 -m rig "<task>" --path /path/to/project --flow lead/dev-feature
             python3 -m rig --approve <id>
5) Destroy the VM after the run. Disposability is the isolation boundary.
NEXT
say "provision OK"
