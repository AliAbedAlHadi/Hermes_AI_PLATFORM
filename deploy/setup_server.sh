#!/usr/bin/env bash
# One-time setup of Hermes Federation on an Ubuntu server (e.g. Oracle Cloud Always Free, Ampere A1).
# Run as the normal user (not root) AFTER copying these to ~/hermes-deploy/:
#   hermes-federation.zip   (from `hermes backup` on the PC)
#   workspaces.zip          (the PC folder %USERPROFILE%\hermes-federation\workspaces, zipped)
#   console/                (server.py, index.html)
#   roster/set_local_model.py
# Usage:  bash setup_server.sh
set -euo pipefail
DEPLOY="$HOME/hermes-deploy"
WS="$HOME/hermes-federation/workspaces"
BOARDS="federation isc auvia tabbeqai spectra spectrasolutions"

echo "== 1. system packages"
sudo apt-get update -y
sudo apt-get install -y git python3 unzip curl

echo "== 2. Hermes Agent (official installer)"
if ! command -v hermes >/dev/null; then
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
fi
hermes --version

echo "== 3. restore the federation (profiles, boards, settings)"
hermes import --force "$DEPLOY/hermes-federation.zip"

echo "== 4. workspaces and Windows -> Linux paths"
mkdir -p "$WS"
unzip -oq "$DEPLOY/workspaces.zip" -d "$WS/.."
for b in $BOARDS; do
  [ -d "$WS/$b/.git" ] || { mkdir -p "$WS/$b"; git -C "$WS/$b" init -q -b main; }
  hermes kanban boards set-default-workdir "$b" "$WS/$b"
  hermes project add-folder "$b-work" "$WS/$b" --primary || true
done

echo "== 5. agents -> Groq (no GPU on this server)"
python3 -I "$DEPLOY/roster/set_local_model.py" custom:groq qwen/qwen3.8-27b

echo "== 6. services"
hermes gateway install --start-now
sudo cp "$DEPLOY/hermes-console.service" /etc/systemd/system/
sudo systemctl daemon-reload
echo "Now put the secrets in /etc/hermes-console.env (see DEPLOY_ORACLE.md), then:"
echo "  sudo systemctl enable --now hermes-console"
