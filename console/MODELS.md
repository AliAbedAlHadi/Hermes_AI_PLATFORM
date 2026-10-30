# Model management (Console → Models tab)

Choose which model each group of agents uses, across providers, without editing 118 files by hand.

## Providers

| Provider | Base URL | Key | Allowed destinations |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` | required | that host only, HTTPS |
| Groq | `https://api.groq.com/openai/v1` | required | that host only, HTTPS |
| DeepSeek | `https://api.deepseek.com/v1` | required | that host only, HTTPS |
| Ollama (local) | `http://localhost:11434` | none | localhost only (never exposed) |
| OpenAI-compatible (custom) | your endpoint | required | HTTPS; never loopback, private (10/8, 172.16/12, 192.168/16), link-local, CGNAT, metadata (169.254.169.254) or `.local`/`.internal` names, also after DNS resolution |

Base URLs with credentials, a query or a fragment are refused. Redirects are never followed (a redirect could
carry the key to another host); they are reported as `redirect`.

## Testing a connection (free, read-only)

A test is **one** request: `GET <base>/models` with the key as a Bearer header (Ollama: `/api/tags` for installed
models and `/api/ps` for loaded ones, with their current context length). No chat or completion request is ever
sent, so nothing is billed and no company data leaves the machine. Results:

`ok`, `model_missing`, `auth` (key refused or missing), `rate_limited` (429), `not_found` (no /models at that URL),
`redirect`, `timeout` (8 s), `unreachable`, `malformed` (not JSON / empty list), `invalid` (URL refused before any request).

## Saving

"Test and save" tests first and saves only on `ok`. The configuration (provider, base URL, model, last test) goes to
`federation/models.json`, which never holds a key. The key goes to `federation/model-keys.env` as
`FED_MODEL_KEY_<NAME>=…`; other lines in that file are kept as they are. The Console settings file and its
`GROQ_API_KEY` are never written by model management. The key field is a password field, cleared after every
use, never stored in the browser and never returned by any API (`has_key: true` only).

## Assigning to agents (two steps)

1. **Preview**: pick a saved, successfully tested configuration, a purpose and optionally one company. The preview
   lists every agent that would change, from → to. Nothing is written.
2. **Apply**: type `APPLY`. Only the previewed agents change; the preview expires after 5 minutes, is single use,
   and is void if the configuration changed after the preview.

Purposes: `worker` (developers, executor, devops, QA, team lead, optimizer, release controller, security,
architect, document operations), `reviewer`, `verifier`, `business` (receptionist, CEO, directors, BA, PM,
customer success, finance, marketing), `federation` (the 8 fed-* agents).

For each agent: `config.yaml` is backed up once to `config.yaml.bak-models`; a provider `fed-<name>` is added
under `providers:` and selected in `model:` (`ollama_num_ctx: 65536` only for Ollama); the guard hook, memory
tool, prompts and permissions are untouched; for a keyed provider the key is written to that agent's own
`.env` only. Every applied agent gets its own audit row (`admin/models/apply.agent`).

A start-up `AGENT_PROVIDER` / `AGENT_MODEL` switch in the settings file skips agents the owner assigned here.

## Undo

Copy `profiles/<agent>/config.yaml.bak-models` back over `config.yaml` (and remove the `FED_MODEL_KEY_…` line from
that agent's `.env`). The Phase 5 checkpoint also holds every profile file.

## Not changed by this feature

Ollama's context length, network binding and installed models; the existing `groq` and `local-ollama` provider
entries and `GROQ_API_KEY` in any file.

## Example (no real key)

Provider *OpenAI-compatible (custom)*, base `https://llm.example.com/v1`, model `example-model`, key
`sk-EXAMPLE-NOT-REAL`, name `reviewer-example` → Test and save → Assign: `reviewer-example`, purpose `reviewer`,
company `isc` → Preview (1 agent: isc-reviewer) → Apply.
