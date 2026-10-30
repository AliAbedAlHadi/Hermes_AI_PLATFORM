# Hermes Federation — Agent Roster and Role Cards

**Version:** 0.1 (proposal for owner corrections)
**Date:** 2026-10-20
**Basis:** Hermes Federation Implementation Specification v1.1 (Rxx.yy references below)
**Status:** DRAFT. Every role name is taken word-for-word from the spec. Where the spec gives no detail, the card says **PROPOSED** and the owner corrects it.

---

## 0. How to read this

- **Agent ID** is immutable and machine-safe (`company.role`). **Display name** is what the owner sees. IDs never change when a name, model or host changes (R04.01, R03.03).
- **Persistence:** `persistent` = keeps identity and memory, woken by events; `on-demand` = spawned for a task, permissions expire (R04.06). No role needs a permanently running LLM process.
- **Default tier:** T0 deterministic, T1 local/LAN, T2 free cloud, T3 economical, T4 premium (R06.02). This is the *starting* tier; escalation rules in R06.04–R06.05 apply.
- **Approval:** management is accountable but is **not** an approval station for every file write (R04.05). Routine work inside the delegation runs autonomously (R01.02, R10.06).

---

## 1. Layers

```
Owner (human, authenticated)
 └── Federation management            (8 roles, one set for the whole federation)
      └── Companies                   (5: ISC, AUVIA, TabbeQ AI, Spectra, Spectra Solutions)
           ├── Receptionist           (public front door, per company)
           ├── Company management     (CEO, directors)
           ├── Delivery pipeline      (Analyst → Architect → Executor → Reviewer → Verifier → Release Controller)
           └── Support functions      (QA, Security, DevOps, Customer Success, Finance, Marketing, Document Operations)
 └── Deterministic bots               (T0, no LLM: gateway, quota ledger, anomaly monitor, emergency stop, ...)
```

---

## 2. Federation management (R04.02)

One instance each, scope = whole federation.

### 2.1 Hermes Admin — `federation.hermes-admin`
- **Mission:** Senior administrative interface for the owner (R01.01). Turns owner conversation into companies, departments, agents and policies.
- **Owns:** conversational setup (R03.01–R03.02), company/agent registry changes, the owner questionnaire, decision log.
- **Inputs:** authenticated owner messages (Web, WhatsApp, Telegram, voice).
- **Outputs:** versioned config changes, short question batches, status summaries.
- **Must not:** accept admin commands from a non-authenticated channel or from voice alone (R09.02); re-ask a question that already has a current answer (R03.02).
- **Escalates to:** owner, only for the cases in R10.06.
- **Persistence / tier:** persistent / T4 for decisions, T1 for routine replies.

### 2.2 Federation COO — `federation.coo`
- **Mission:** Keeps work flowing across all companies.
- **Owns:** cross-company work orders (R11.05), capacity balancing, agent redistribution (R14.04), blocked-task triage.
- **Outputs:** daily operations digest, reassignment events.
- **Must not:** change company scope or spending authority.
- **Escalates to:** Hermes Admin.
- **Persistence / tier:** persistent / T3.

### 2.3 ModelOps — `federation.modelops`
- **Mission:** Knows which model can do what, where, and how well.
- **Owns:** model registry (R08.02), endpoint capability probes (R06.03), local benchmarks (R08.03), routing policy, version pinning (R13.04).
- **Must not:** call a model "local" without verified local inference (R08.05–R08.06); pick models by marketing name or size.
- **Escalates to:** Hermes Admin (policy), Infrastructure and Recovery (GPU problems).
- **Persistence / tier:** persistent / T0 probes + T3 analysis.

### 2.4 Quota/FinOps — `federation.finops`
- **Mission:** Protects paid allowance and money.
- **Owns:** account registry (R07.01), shared quota ledger (R07.02), 20% supervision reserve (R07.06), cost-per-accepted-output reports (R07.04).
- **Must not:** buy subscriptions, raise caps or auto-top-up (R07.05); rotate accounts to evade limits.
- **Works with bot:** Quota Ledger, Spend Anomaly Monitor (section 5).
- **Persistence / tier:** persistent / T0 + T3 reports.

### 2.5 Infrastructure and Recovery — `federation.infra-recovery`
- **Mission:** Machines, networks, backups and restore.
- **Owns:** node enrollment and identity (R05.02), private networking (R05.05), snapshots, backup/restore tests (R16.04), runbooks.
- **Must not:** scan networks beyond enrolled assets, overwrite existing installs without snapshot (R15.02).
- **Persistence / tier:** persistent / T1.

### 2.6 Knowledge Management — `federation.knowledge`
- **Mission:** Memory that is correct, scoped and traceable.
- **Owns:** memory scopes (R12.01), provenance and supersession (R12.02), promotion of sanitized lessons to global memory (R12.04), deletion/invalidation.
- **Must not:** promote customer data or secrets; treat vector search as source of truth.
- **Persistence / tier:** persistent / T1.

### 2.7 Security/Policy — `federation.security-policy`
- **Mission:** Defines and reviews policy. Enforcement itself lives in software services, not in this agent's opinion (R04.02).
- **Owns:** permission model, egress rules (R12.03), plugin/extension vetting (R09.05), prompt-injection tests, secret redaction checks.
- **Must not:** disable audit or emergency stop.
- **Persistence / tier:** persistent / T3, T4 for security changes.

### 2.8 Independent Audit — `federation.audit`
- **Mission:** Checks that what was claimed actually happened.
- **Owns:** evidence ledger (R15.06), audit trail review (R12.05), random sampling of SUCCEEDED tasks against R11.06.
- **Independence:** reports to the owner directly; cannot be reassigned or reconfigured by the optimizer or COO.
- **Persistence / tier:** persistent / T3.

---

## 3. Company role templates (R04.03, R04.05)

Every company gets these roles from one template. ID pattern: `<company-id>.<role>`. Roles marked *on-demand* are instantiated only when work needs them.

| # | Display name (exact) | Role ID | Persistence | Default tier |
|---|---|---|---|---|
| 1 | Receptionist | `receptionist` | persistent | T1 |
| 2 | CEO | `ceo` | persistent | T4 (decisions) |
| 3 | Delivery Director | `delivery-director` | persistent | T3 |
| 4 | Engineering Director | `engineering-director` | persistent | T3 |
| 5 | Business Analyst/Sales | `business-analyst` | persistent | T3 |
| 6 | Project Manager | `project-manager` | persistent | T1 |
| 7 | Solution Architect | `solution-architect` | on-demand | T4 |
| 8 | Team Lead | `team-lead` | on-demand | T3 |
| 9 | Senior Developer | `senior-developer` | on-demand | T3 |
| 10 | Junior Developer | `junior-developer` | on-demand | T1 |
| 11 | Executor | `executor` | on-demand | T1 |
| 12 | Optimizer | `optimizer` | on-demand | T3 |
| 13 | Reviewer | `reviewer` | on-demand | T1 → T4 |
| 14 | Verifier | `verifier` | on-demand | T0 + T1 |
| 15 | Release Controller | `release-controller` | on-demand | T0 + T4 |
| 16 | QA | `qa` | on-demand | T1 |
| 17 | Security | `security` | on-demand | T3 |
| 18 | DevOps | `devops` | on-demand | T1 |
| 19 | Customer Success | `customer-success` | persistent | T1 |
| 20 | Finance | `finance` | when required | T3 |
| 21 | Marketing | `marketing` | when required | T2 |
| 22 | Document Operations | `document-operations` | when required | T1 |

### Role cards

**Receptionist** — Greets customers, shows only the published service directory, routes by capability fit, customer preference and capacity (R03.04). Shows company identity, role and reason on every handoff (R03.05). *Must not:* route by keyword alone; accept admin claims from customers; expose internal tools.

**CEO** — Accountable for the company's goals, services and priorities. Approves new services and cross-company subcontracting as lead company. *Must not:* sit in the approval path of routine tasks.

**Delivery Director** — Owns customer projects end to end; accountable to the customer as lead (R11.05). Watches deadlines, WAITING_* states and blockers.

**Engineering Director** — Owns technical standards, architecture decisions records (ADRs), and escalation of hard technical problems.

**Business Analyst/Sales** — Turns customer needs into requirements, acceptance criteria and a requirements revision. Asks short question batches; defers non-blocking unknowns (R03.02).

**Project Manager** — Decomposes work into tasks with full contracts (R11.01), dependencies (no cycles), and keeps the Kanban truthful (RUNNING needs a heartbeat, R11.02).

**Solution Architect** — Called when design, data model, security boundary or integration choice is needed. Output: ADR + interface contract.

**Team Lead** — Assigns tasks to developers/executors, holds resource leases (R10.02), unblocks.

**Senior Developer** — Solves the hard sub-problem after escalation evidence, then hands routine execution back to local workers (R06.04).

**Junior Developer** — First-attempt implementation on local models; always attaches diff, tests and evidence.

**Executor** — Runs commands, builds, installs, operates GUI/browser inside its workspace and lease. Prefers API → CLI → GUI (R10.02).

**Optimizer** — Measures efficiency (cost, latency, rework) outside the critical path unless performance is the active problem (R04.05). Proposes changes through Candidate → Sandbox → Measure → Canary → Promote/Rollback (R13.03).

**Reviewer** — Gives specific findings, not opinions. Local reviewer first; premium review only at milestones, architecture/security changes, or after cheaper review fails (R06.06).

**Verifier** — Confirms evidence is bound to the exact commit/artifact hash and mandatory tests passed (R11.06). Rejects screenshot-only evidence.

**Release Controller** — Deploys when policy and acceptance checks pass (R10.06); verifies health and URL; refuses to deploy a different hash than the one reviewed.

**QA** — Writes and runs functional, regression and Arabic/RTL tests.

**Security** — Company-level security review; secret redaction, dependency risk, injection tests. Escalates policy questions to Federation Security/Policy.

**DevOps** — Environments, CI, deployment pipelines, rollback notes and snapshots.

**Customer Success** — Delivery handover, customer updates, feedback, WAITING_CUSTOMER follow-up.

**Finance** — Cost allocation per project and work order; customer billing records.

**Marketing** — Service catalog text and customer-facing identity.

**Document Operations** — Word/Excel/PowerPoint/LibreOffice outputs via licensed integrations (R09.04).

### Normal delivery pipeline (R04.05)

```
Business Analyst ─► Solution Architect* ─► Executor/Junior Developer ─► Reviewer ─► Verifier ─► Release Controller
                                              ▲            │
                                              └─ rework ◄──┘   (2 local repairs → economical senior → expert, R06.05)
* only when necessary.  Optimizer runs alongside, not in the path.
```

---

## 4. Companies (R03.01)

| Company ID | Official name | Notes |
|---|---|---|
| `isc` | ISC | PROPOSED: owner to give purpose and services |
| `auvia` | AUVIA | Seed staff below; delivers AI services, may subcontract to TabbeQ AI |
| `tabbeq-ai` | TabbeQ AI | Specialist AI supplier to other companies |
| `spectra` | Spectra | Distinct from Spectra Solutions |
| `spectra-solutions` | Spectra Solutions | Distinct from Spectra |

### AUVIA seed (R04.04)

Labels are agent identifiers, not human identities. Department placement is from the spec; the role inside each department is **PROPOSED** for owner correction.

| Agent label | Department (spec) | Proposed role |
|---|---|---|
| MAK | Management | CEO |
| HAK | Management | Delivery Director |
| HAY | Management | Engineering Director |
| MYA | Software | Team Lead (Software) |
| ZMA | AI | Team Lead (AI) |
| FIS | People & Office Operations | Document Operations |
| AKA | People & Office Operations | Customer Success |

---

## 5. Deterministic bots (T0, no LLM)

Required by the spec to enforce rules in software, not by agent opinion.

| Bot | ID | Job | Spec |
|---|---|---|---|
| Channel Gateway | `bot.gateway` | Authenticate, rate-limit, deduplicate, persist messages | R09.01 |
| Quota Ledger | `bot.quota-ledger` | Atomic reserve/settle across windows | R07.02 |
| Spend Anomaly Monitor | `bot.spend-monitor` | Interrupt abnormal spending loops, open incident | R07.05 |
| Emergency Stop | `bot.emergency-stop` | Outside Hermes process; stop dispatch, revoke credentials | R10.04 |
| Lease Manager | `bot.lease-manager` | Locks for files, windows, deployments, fencing tokens | R10.02 |
| Heartbeat Watchdog | `bot.watchdog` | Detect stuck/dead workers, recover in ≤60s | R16.01 |
| Recovery Bot | `bot.recovery` | Restore previous config without an LLM | R13.06 |
| Audit Sink | `bot.audit-sink` | Append-only external audit log | R10.04, R12.05 |

---

## 6. Group chat design

**Recommendation:** yes to group chats, but **only for people-facing coordination**. Agents hand off work through task contracts and Kanban, not chat — the spec says unstructured conversation is not a handoff contract (R11.04).

**Grouping in the desktop app (Bot Mode):**
- **Sections** (sidebar folders, no member limit) group bots by company: Hermes Federation, ISC, AUVIA, TabbeQ AI, Spectra, Spectra Solutions. Already applied to all 118 profiles.
- **Group chats** hold 2–6 bots (`GROUP_CHAT_MAX_MEMBERS = 6`). Every room below fits that limit.

Federation rooms:

| Room | Bots (≤6) |
|---|---|
| **Federation Ops** | Hermes Admin, Federation COO, ModelOps, Quota/FinOps, Infrastructure and Recovery, Security/Policy |
| **Federation Governance** | Hermes Admin, Knowledge Management, Security/Policy, Independent Audit |
| **CEO Council** | Hermes Admin, ISC CEO, AUVIA CEO, TabbeQ AI CEO, Spectra CEO, Spectra Solutions CEO |

Per-company rooms (4 rooms × 5 companies; together they cover all 22 roles):

| Room | Bots (≤6) |
|---|---|
| **<Company> Leadership** | CEO, Delivery Director, Engineering Director, Business Analyst/Sales, Project Manager |
| **<Company> Delivery** | Team Lead, Junior Developer, Executor, Reviewer, Verifier, Release Controller |
| **<Company> Engineering** | Solution Architect, Senior Developer, Optimizer, QA, Security, DevOps |
| **<Company> Customer & Business** | Delivery Director, Receptionist, Customer Success, Finance, Marketing, Document Operations |

The owner and the developer join rooms as users, not as bots, so they do not count toward the limit.

Rules:
- Groups are private; none is created public and no invitations are sent implicitly (guide §8).
- Workers (developers, executors, reviewers) do **not** sit in chats; they report through task events, which the chats summarize.
- A customer never sees another company's internal chat, secrets or full conversation history (R03.05).

---

## 6a. Operating rules (applied 2026-10-20)

- **Workspaces:** each board has a git repo at `%USERPROFILE%\hermes-federation\workspaces\<board>` (outside OneDrive), bound as a Hermes project. Create real tasks with `--workspace worktree`: each task gets a preserved worktree and its own `wt/<task-id>` branch, so evidence stays bound to a commit (R11.06). Without the flag the CLI uses `scratch`, which is deleted when the task completes.
- **Tool restrictions** (`agent.disabled_toolsets`, applied by `apply_settings.py`):
  - Reviewer, Verifier, Independent Audit: no `file`, `computer_use`, `browser`, `image_gen`, `cronjob`, `delegation`. Terminal stays so they can run tests; it can still write, so strict read-only needs OS sandboxing (R10.03).
  - Receptionist: additionally no `terminal` or `code_execution` (customer-facing, R05.05).
- **Local models:** Ollama is configured as provider `local-ollama` (`http://localhost:11434/v1`).
- **Models per tier:** pending. Owner direction is DeepSeek for every tier; the exact model ID is taken from the account after the key is added (R08.06), then set in `apply_settings.py`.

## 7. Open points for the owner

1. Purpose and services for ISC, Spectra and Spectra Solutions.
2. Confirm or correct the AUVIA proposed roles (section 4).
3. Which channel hosts **Hermes Implementation**: Telegram or WhatsApp.
4. Whether the owner wants to sit in each company's Leadership group.
