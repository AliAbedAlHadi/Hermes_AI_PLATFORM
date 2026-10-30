# M0 — Discovery Report (laptop node)

**Date:** 2026-10-20 · **Scope:** this laptop only, read-only (R15.02). No `.env`, keys or secret files were read.
**Not covered:** Nous cloud hosting, Microsoft Cloud VPS, the "50-90" GPU computer, the LAN server (R02.02).

## 1. Hermes identity (R02.01)
| Item | Value |
|---|---|
| Product | Nous Research Hermes Agent |
| Version | v0.22.2 (2026.9.24), stable |
| Repository | https://github.com/NousResearch/hermes-agent.git (official, S01) |
| Commit | d923a5eb487a17c0a68801bba9ef0f56c81040a8 (2026-10-15) |
| Install | git, `%LOCALAPPDATA%\hermes\hermes-agent` |
| Default model | `upstage/solar-mini4:free` via Nous Portal |
| Providers configured | Nous Portal, Local Ollama (localhost:11434) |
| Gateway | stopped; platform `api_server` |
| MCP servers | `hermes-cloud` (Nous portal, 3 tools, enabled) |

**"7.3" extension:** **not found.** No installed plugin, MCP server or version matches "7.3". All 56 plugins are `bundled` with Hermes. Status stays OPEN: the owner must say what "7.3" refers to (R02.01, R09.05).

## 2. Hardware (R08.01)
| Item | Value |
|---|---|
| GPU | NVIDIA GeForce RTX 4060 Laptop, **8 GB VRAM**, driver 591.66 |
| CPU | AMD Ryzen 7 7435HS, 8 cores / 16 threads |
| RAM | 15.7 GB |
| OS | Windows 11 Home 10.0.26300 |
| Free disk (C:) | ~135 GB |

## 3. Runtimes
| Runtime | State |
|---|---|
| Ollama 0.34.4 | installed, **service not running** |
| Docker 28.5.1 | running |
| PostgreSQL 17 and 18 | both running (ports 5432, 5433) |
| WSL | Ubuntu and docker-desktop, stopped |
| Python 3.11.9 / Node 22.17.1 / Git 2.50.1 | installed |
| Claude Code | installed (R09.06) |

## 4. Ollama models
| Model | Where inference runs | Fits 8 GB VRAM? |
|---|---|---|
| `qwen2.5-coder:7b` | local | yes (quantized) |
| `qwen3.5:9b` | local | yes (quantized, tight with long context) |
| `llama3:latest` (8B) | local | yes (quantized) |
| `gpt-oss:20b-cloud` | **cloud** (Ollama Cloud), not local | n/a |

Per R08.05, `gpt-oss:20b-cloud` must be registered as a cloud route even though it is listed in local Ollama. No local benchmarks have been run yet (R08.03).

Note: the default `ollama_num_ctx: 65536` is large for 8 GB VRAM; the KV cache at that context may not fit next to a 7–9B model. Benchmark before relying on it (R08.03).

## 5. Network exposure (R05.05)
| Port | Process | Bound to | Note |
|---|---|---|---|
| 1433 | SQL Server | **0.0.0.0 (all interfaces)** | Reachable from the network. Not Hermes, but R05.05 forbids publicly exposed database ports on enrolled assets |
| 5432 | PostgreSQL | **0.0.0.0 (all interfaces)** | Same concern |
| 5433 | PostgreSQL | all IPv6 interfaces | Same concern |
| 1434 | SQL Server Browser | 127.0.0.1 | local only, fine |

No Hermes, model or Docker API port is listening right now.

## 6. Supply-chain audit (R09.05) — `hermes security audit`
44 known findings across 115 components, all in the Hermes Python venv:

| Package | Installed | Fixed in | Worst severity |
|---|---|---|---|
| PyJWT | 2.13.0 | 2.14.0 / 2.15.0 | **CRITICAL** (1) + HIGH (5) |
| aiohttp | 3.14.3 | 3.14.4 | HIGH (2) |
| urllib3 | 2.7.0 | 2.8.0 | HIGH (2) |
| oauthlib | 3.3.1 | — | unknown |
| multidict | 6.7.1 | — | unknown |

No plugin or MCP findings.

## 7. Backup (R15.02 snapshot)
Full Hermes backup taken before any change: `%USERPROFILE%\hermes-backups\hermes-backup-2026-10-20-before-federation.zip` (outside OneDrive, because it contains `.env` and auth files).

## 8. Open items from M0
1. What "7.3" is.
2. Inventory of the other 4 machines (needs access to them).
3. Fix the database ports exposed to the network (needs approval).
4. Update the vulnerable venv packages (needs approval).
5. Start Ollama and run local benchmarks before assigning model tiers.
