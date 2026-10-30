# Deploying Hermes Federation online — checklist

Verified locally on 2026-10-25: 118 agents, 6 boards with git workspaces, console API and UI, end-to-end agent chat.

## 1. Before you copy anything
- [ ] Rotate the Groq key (the old one was pasted in chat). Set a spending limit in the Groq console.
- [ ] Patch the vulnerable Hermes venv packages: PyJWT ≥ 2.15, aiohttp ≥ 3.14.4, urllib3 ≥ 2.8 (`hermes security audit` to confirm).

## 2. Move Hermes to the server
```bash
# on this PC
hermes backup -o hermes-federation.zip -k 0      # contains .env and auth — copy it privately, never via OneDrive/email
# on the server (after installing Hermes Agent v0.22.2)
hermes import hermes-federation.zip
```
- Windows paths inside the imported data must be fixed on a Linux server:
  - each board's `default_workdir` in `~/.hermes/kanban/boards/<board>/board.json`
  - the workspace repos: copy `%USERPROFILE%\hermes-federation\workspaces\*` to the server and point the boards at them
    (`hermes kanban boards set-default-workdir <board> /srv/hermes-federation/workspaces/<board>`)
- The server has no Ollama/GPU: switch every agent to Groq before starting work
  (`python set_local_model.py custom:groq qwen/qwen3.8-27b`, edit HOME in the script to `~/.hermes` on Linux).
- Set the key as an environment variable for the Hermes service user: `GROQ_API_KEY=...`

## 3. Run the services
- Gateway (runs the Kanban dispatcher, detects dead workers — R16.01): `hermes gateway install && hermes gateway start`
- Console:
```bash
export CONSOLE_HOST=127.0.0.1          # keep it local; publish through the proxy/tunnel below
export CONSOLE_PORT=8765
python3 -I console/server.py
```
Owner sign-in: on first start the console prints a one-time setup code; open /login, enter it and choose the owner password. Only a salted hash is stored (HERMES_HOME/federation/owner.json). The console listens on 127.0.0.1 unless `CONSOLE_ALLOW_REMOTE=1` (only behind HTTPS).

## 4. Expose it safely (R05.05)
- Preferred: private access only (Tailscale, or Cloudflare Tunnel with Access) — nothing public.
- If public: a reverse proxy with HTTPS (Caddy/Nginx) in front of `127.0.0.1:8765`. Basic auth is only safe over HTTPS.
- Never expose Ollama (11434), PostgreSQL, Docker or Hermes admin ports.

## 5. Smoke test online
- [ ] Console asks for the password; wrong password → 401.
- [ ] 118 agents listed; each board loads.
- [ ] Chat with `isc-receptionist` answers.
- [ ] Create a task with `--workspace worktree` → done → file and commit exist in the board repo.

## 6. Restart behaviour (validated 2026-10-28 on a fresh clone)
- State lives in `HERMES_HOME/federation/` (`memory.db`, `workflow.db`, `owner.json`) and the Kanban boards; a
  restart keeps tasks, events, reviews, usage, memory, customers and work orders.
- Starting a newer version migrates `workflow.db` in place: new columns and tables are added, no row is changed.
  Back up `HERMES_HOME/federation/` before upgrading anyway.
- Owner sessions live in memory: after a restart the owner signs in again. The setup code is printed only while
  no owner exists.
- The console's sync loop (every 15 s) resumes on start: workers that finished while it was down are picked up,
  and gateway-blocked tasks are retried when their backoff time has passed.
- Isolated validation: `FED_OWNER_FILE`, `FED_MEMORY_DB`, `FED_WORKFLOW_DB`, `FED_HERMES_CMD`, `FED_WORKSPACES` and
  `HERMES_HOME` point a second console at copies, on another `CONSOLE_PORT`.

## Optional: local cost rate
Local inference has no API bill. To see an operational estimate, set `FED_LOCAL_COST_PER_HOUR` (your machine's
cost per hour, e.g. electricity); without it the console reports tokens and wall time only.

## Known gaps (not deployment blockers, but do not promise them)
- Company records for ISC, Spectra, Spectra Solutions are missing → agents may invent company details.
- Spec package (contracts, acceptance tests, backlog) not received → no acceptance evidence yet.
