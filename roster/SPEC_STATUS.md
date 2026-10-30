# Hermes Federation — Status Against Specification v1.1

**Date:** 2026-10-27 (updated after memory, customers and work orders)
**Legend:** ✅ done · 🟡 partial · ❌ not started · ⏳ needs owner input

## Summary

| Spec section | ✅ | 🟡 | ❌ | ⏳ |
|---|---|---|---|---|
| 1. Charter | 0 | 2 | 2 | 0 |
| 2. Baseline, infra, accounts | 0 | 1 | 2 | 2 |
| 3. Companies and reception | 0 | 4 | 1 | 0 |
| 4. Workforce and hierarchy | 1 | 4 | 1 | 0 |
| 5. Architecture | 0 | 2 | 4 | 0 |
| 6. Model routing | 0 | 1 | 5 | 0 |
| 7. Quotas and billing | 0 | 0 | 6 | 0 |
| 8. Local models and hardware | 0 | 1 | 5 | 0 |
| 9. Channels and tools | 0 | 1 | 4 | 1 |
| 10. Autonomy and safety | 0 | 2 | 4 | 0 |
| 11. Task lifecycle | 0 | 1 | 5 | 0 |
| 12. Memory and audit | 2 | 4 | 0 | 0 |
| 13. Self-improvement | 0 | 1 | 5 | 0 |
| 14. 2D/3D and observability | 0 | 2 | 4 | 0 |
| 15. Server preparation | 0 | 4 | 2 | 0 |
| 16. Nonfunctional | 0 | 1 | 5 | 0 |
| **Total (92 counted clauses)** | **3** | **31** | **55** | **3** |

Progress (✅ = 1, 🟡 = ½): **≈20%** (was ≈10% on 2026-10-20).

**Blocking finding:** the spec cites package files that are **not** in `Documents\Hermes`: `contracts/openapi.yaml`, `contracts/data_model.json`, `contracts/task.transitions.json`, `tests/acceptance_EN.feature`, `tests/acceptance_AR.feature`, `backlog/requirements.json`, `reference/router.py`, `tools/preflight.py`, `docs/SOURCES.md`, `AGENTS.md`. Only the EN and AR `.docx` are present. Ask Dr. Hassan Ayoub for the full package.

---

## 1. Product charter
| Clause | Status | Notes |
|---|---|---|
| R01.01 Conversational companies, full customer pipeline | 🟡 | Agents exist; no conversational creation, intake, delivery |
| R01.02 Autonomous operation, auto model switching | ❌ | |
| R01.03 Economic policy (tiers) | ❌ | All profiles on one free model |
| R01.04 Product boundaries | 🟡 | "Not a person" rule written into every SOUL.md |
| R01.05 Terminology | — | Informative |

## 2. Baseline, infrastructure and accounts
| Clause | Status | Notes |
|---|---|---|
| R02.01 Verify Hermes; resolve "7.3" | 🟡 | Hermes Agent v0.22.2, git install, commit d923a5eb. "7.3" unresolved |
| R02.02 Infrastructure inventory | ❌ | Only laptop seen: RTX 4060 Laptop, 16 GB RAM. Nous cloud, Microsoft VPS, "50-90" GPU PC, LAN server not inventoried |
| R02.03 Account list | ⏳ | Owner-reported; not verified |
| R02.04 Separate subscriptions vs API vs automation | ❌ | |
| R02.05 OpenRouter, free services, OpenCode Go | ⏳ | "OpenGo" to resolve |

## 3. Companies, reception, conversational setup
| Clause | Status | Notes |
|---|---|---|
| R03.01 Five companies via Web/WhatsApp/Telegram | 🟡 | Created by script (sections + boards), not by conversation |
| R03.02 "Create a company" questionnaire | ❌ | |
| R03.03 Company record (ID, aliases, capabilities, policies…) | 🟡 | ID, name, roles only. Services, capabilities, sharing policy, success measures missing |
| R03.04 Receptionist routing + service directory | 🟡 | Receptionist agents exist; no directory or routing logic |
| R03.05 Handoff identity, lead company, no secret sharing | 🟡 | Lead company + work orders + isolation built; handoff display of identity/reason not yet |

## 4. Digital workforce
| Clause | Status | Notes |
|---|---|---|
| R04.01 Identity independent of model; activate on events | 🟡 | Profiles give identity; event activation not configured |
| R04.02 Federation management roles | 🟡 | 8 profiles created; software enforcement not built |
| R04.03 Company organization roles | ✅ | 22 roles × 5 companies |
| R04.04 AUVIA seed | 🟡 | Placed; roles inside departments PROPOSED |
| R04.05 Delivery pipeline | 🟡 | Roles exist; pipeline not wired in Kanban |
| R04.06 Persistent/temporary/bots, auto-spawn experts | ❌ | |

## 5. Architecture and state
| Clause | Status | Notes |
|---|---|---|
| R05.01 FastAPI + PostgreSQL + artifact store + React | ❌ | PostgreSQL 18 installed locally, unused |
| R05.02 Node agent + Hermes adapter | ❌ | |
| R05.03 Single source of truth, ID mapping | 🟡 | Kanban boards per company exist; no federation store |
| R05.04 Bridge host boards by API/events | ❌ | |
| R05.05 Gateway/admin/execution separation, private network | 🟡 | Console on 127.0.0.1, Tailscale plan, Docker host networking; customer portal separate from admin only logically |
| R05.06 No mandatory Kafka/K8s/Temporal | — | Constraint, nothing to build |

## 6. Model routing
R06.05 🟡: max 2 retries, stop rule in SOUL, blocked state verified live 2026-10-27. Others ❌:
R06.01 two tracks · R06.02 tiers T0–T4 · R06.03 capability probes · R06.04 junior→review→escalate · R06.05 retry/escalation settings · R06.06 premium review policy.

## 7. Quotas and billing — all ❌
R07.01 account registry · R07.02 shared quota ledger · R07.03 usage readings · R07.04 cost per accepted output · R07.05 no invented caps, anomaly monitor · R07.06 20% supervision reserve.

## 8. Local models and hardware
| Clause | Status | Notes |
|---|---|---|
| R08.01 Inventory CPU/RAM/VRAM/runtimes | 🟡 | Laptop partly seen; Ollama 0.34.4 installed but not running |
| R08.02–R08.06 Registry, benchmarks, local vs cloud | ❌ | |

## 9. Channels, services, tools
| Clause | Status | Notes |
|---|---|---|
| R09.01 Web, WhatsApp, Telegram with voice/files | ❌ | Hermes has `whatsapp-cloud` and gateway commands available |
| R09.02 Arabic/Lebanese voice transcription | ❌ | |
| R09.03 WhatsApp Business Cloud API, Telegram Bot API | ⏳ | Needs owner's accounts |
| R09.04 Hermes Office vs MS Office | ❌ | |
| R09.05 Plugin/MCP registry with checksums | ❌ | |
| R09.06 Usable by coding agents | 🟡 | Claude Code available on this PC |

## 10. Autonomy and safety
R10.03 🟡 risk stated (files/boards not isolated on one host) · R10.05 🟡 memory permissions enforced outside the model, secret guard, identity from config. Others ❌:
R10.01 autonomous PoC on enrolled assets · R10.02 API→CLI→GUI, leases · R10.03 declared root-isolation risk · R10.04 emergency stop **outside** Hermes (built-in `hermes pause` is inside, so not sufficient alone) · R10.05 untrusted data, short-lived credentials · R10.06 automatic deploy when checks pass.

## 11. Task lifecycle
R11.05 🟡 work orders built and proven live (AUVIA → TabbeQ AI, 13/13); align with contracts/data_model.json when received. Others ❌:
R11.01 task contract fields · R11.02 13 logical states · R11.03 events, idempotency, Unknown Outcome · R11.04 structured handoffs · R11.05 cross-company work orders · R11.06 SUCCEEDED evidence rules.

## 12. Memory and audit
| Clause | Status | Notes |
|---|---|---|
| R12.01 Scoped memory | 🟡 | federation, company, department, project, customer, agent, admin, work order. Session scope, artifact store and semantic index missing |
| R12.02 Provenance | 🟡 | author, source, scope, confidentiality, claim/verified, revision, supersession. Retention not enforced |
| R12.03 Permission-scoped retrieval | ✅ | filtered before and after retrieval, permission version on every audit row, customer binding, no cache |
| R12.04 Global promotion | 🟡 | steward-approved lessons; deletion/index invalidation missing |
| R12.05 Audit trail | 🟡 | every memory and work-order action; model route, cost, tool use not yet |
| R12.06 No self-training claims | ✅ | memory is records, not weights |

## 13. Self-improvement
R13.04 🟡 Hermes v0.22.2 and Ollama 0.34.4 pinned in Docker. Others ❌:
R13.01 propose/test/adopt · R13.02 versioned changes + evaluations · R13.03 canary stages · R13.04 version pinning · R13.05 specific rollback · R13.06 deterministic recovery.

## 14. 2D/3D and observability
| Clause | Status | Notes |
|---|---|---|
| R14.01 Org/task views, 2D graph, 3D office | ❌ | |
| R14.02 Agent cards | 🟡 | Hermes desktop Bot Mode shows roster by section; not the full card (node, quota, task) |
| R14.03–R14.06 Inspect, controls, RTL, metrics | 🟡 | R14.05: Arabic RTL customer portal and dir=auto everywhere; console is a test UI, not the spec frontend |

## 15. Agent-led server preparation
| Clause | Status | Notes |
|---|---|---|
| R15.01 AGENTS.md, schemas, backlog, tests, ADRs | ❌ | Package files missing (see top) |
| R15.02 Read-only preflight | 🟡 | Partial, laptop only |
| R15.03 Auditable change plan and seed | 🟡 | Company/role seed done; networking, storage, services, channels not |
| R15.04 Idempotent, resumable provisioning | 🟡 | `create_profiles.py` is idempotent; rest not built |
| R15.05 First operational proof (AUVIA → TabbeQ AI) | 🟡 | Work order path proven live with real agents; integration, deployment and URL verification not yet |
| R15.06 Evidence ledger | ❌ | |

## 16. Nonfunctional
R16.05 🟡 nonleakage and secret tests (107 unit + 35 live). Others ❌:
R16.01 latency targets · R16.02 latency breakdown · R16.03 concurrency tests · R16.04 restore/outage tests · R16.05 release gates · R16.06 cost comparison.

## 17. Acceptance scenarios — none run
Setup and organization · Economics and quality · Federation · Actual execution · Failures and security · Self-improvement and visualization.

## 18. Milestones
| Milestone | Status |
|---|---|
| M0 Discovery and baseline | 🟡 Hermes identified; inventory, "7.3", version pinning, risk report pending |
| M1 Vertical slice | ❌ |
| M2 Federation | 🟡 Companies, hierarchy, roles, scoped memory, customers, work orders done; questionnaire and customer-visible service identity pending |
| M3 Multi-node and ModelOps | ❌ |
| M4 Channels and experience | ❌ |
| M5 Self-improvement and hardening | ❌ |
