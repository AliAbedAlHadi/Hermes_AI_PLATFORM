# Hermes Federation — OS-level isolation: assessment and Proposal A

**Status: assessment complete; proposal awaiting supervisor decision. Nothing here is implemented.**
OPEN-1, OPEN-2 and OPEN-3 remain **OPEN**. They close only when an approved sandbox passes the execution tests in
section 9 — passing application-level tests does not close them.

| Version | Date | Change |
|---|---|---|
| v1 | 2026-10-28 | Phase 5.4: first proposal (kept unchanged in Appendix A) |
| v2 | 2026-10-29 | Security review: code-path trace of Hermes v0.22.2, synthetic-canary probe (`security/isolation_probe.py`), host facts, architecture comparison, Git worktree analysis, verification plan. **Corrects v1:** under a container backend the Hermes file tools also run inside the container (v1 said they stay on the host), which breaks the current guard's path checks (section 7). **Adds two critical findings:** the guard script and Hermes source are writable by every agent. |

Prepared by Spectra Group engineering for the Hermes Federation owner and supervisor.

---

## 1. Executive summary

All 118 agents run as **one Windows user** (`aliab`). That user is a member of local Administrators (running at
Medium integrity under UAC) and of `docker-users`. It has full control of:
* every company workspace;
* the Hermes home (profiles, `.env`, Kanban boards, federation databases);
* the backups;
* the guard's own source.

The only isolation today is **application-level**: `fed_guard.py`, a fail-closed `pre_tool_call` hook.
* For the **structured file tools** it is a real boundary. Paths are resolved with `realpath`; traversal,
  absolute paths and junctions are refused.
* For the **terminal, process and code-execution tools** it only reads the command text. Whatever the command
  does at run time, including every child process, is limited only by the OS, and the OS allows everything.

The probe ran 28 cases on synthetic canaries only. In **14** of them the guard refuses the call but the OS would
allow it, so the boundary there is application-level only.

In **10** cases the guard **allows** the call **and** the canary is read or written:
* a run-time-built path, a wildcard, or a PowerShell `-EncodedCommand`;
* literal paths to another company's Kanban DB and to `auth.json`;
* a wildcard overwrite of the guard script;
* a patch to Hermes source;
* a junction swapped in after the check (time-of-check/time-of-use).

**Two critical findings go beyond v1:**

1. The guard script is writable by every agent. It lives at `OneDrive\Documents\Hermes\security\fed_guard.py`
   and is shared by all 118 hooks. A wildcard command name passes the guard.
2. Hermes source is writable. It lives in `%LOCALAPPDATA%\hermes\hermes-agent` and dispatches the hooks. Its
   path is not in the guard's command ban list.

Hermes approves a hook by its command string only. The script's modification time is recorded as `null` and is
not enforced. So one changed file silently removes the guard for every agent.

**Recommendation (section 6): a hybrid plan.**
* **Stage 1, on this laptop:** move all agent execution into Docker containers using the Hermes Docker backend.
  * One long-lived container per company.
  * Network `none`, and no Docker socket.
  * Only that company's workspace is mounted.
  * The guard becomes container-aware.
* **Stage 2, for production:** a Linux host with one OS user per company plus rootless containers.

Stage 1 needs supervisor approval for:
* starting Docker Desktop;
* a ~1 GB image download;
* a WSL2 memory cap;
* a guard code change;
* a backend change on the pilot profiles.

## 2. Baseline (2026-10-29, before investigation)

| Item | Value |
|---|---|
| Repository | `C:\Users\aliab\hermes-federation-repo`, branch `main`, HEAD `76a9c2a`, 20 commits ahead of origin (never pushed) |
| Working tree | 10 modified + 9 untracked (hardening + Phase 5); identical to `OneDrive\Documents\Hermes` (no drift) |
| Checkpoint | `hermes-backups\phase5-checkpoint-20261028-1808` present (4 DBs, owner.json, profile-files.tar, patch, fingerprint) |
| Agents / permissions | 118 profiles, permission fingerprint `2b698b4c9bfe6ba5` (unchanged) |
| Existing suite | `security/test_security.py`: 85 passed, 3 OPEN (re-run 2026-10-29, not assumed from history) |
| Hermes | v0.22.2 (`%LOCALAPPDATA%\hermes\hermes-agent`); terminal backend `local` in every profile |
| Host | Windows 11 Home, 16 logical CPUs, 15.7 GB RAM, 49 GB free on C:, RTX 4060 Laptop; Docker Desktop 28.5.1 and WSL2 (Ubuntu, docker-desktop) installed, both stopped; Git 2.50.1 |

## 3. How agents reach the OS (code paths, Hermes v0.22.2)

| Path | Code | Runs where (backend `local`) | Guard |
|---|---|---|---|
| `terminal`, `process_manage` | `tools/terminal_tool.py` → `tools/environments/local.py` | host shell, user `aliab` | text match (defence in depth) |
| `execute_code` | `tools/code_execution_tool.py`, child env from `code_execution_env.py` | host Python kernel, user `aliab` | text match on the code (see below) |
| `read_file`, `write_file`, `patch`, `search_files` | `tools/file_tools.py` → `ShellFileOperations(terminal_env)`: **executed as shell commands through the terminal environment** | host | `realpath` boundary check before the call |
| Kanban tools | `tools/kanban_tools.py` | host process | board/assignee/link rules |
| `delegate_task` | `tools/delegate_tool*.py`: child agent in the same process and profile | host | same hooks (shared dispatcher), from static analysis |
| Kanban dispatch | `hermes kanban dispatch` spawns `hermes -p <assignee>` | host process per worker | the assignee's own hooks |
| Not matched by the guard hook | `browser_*` (incl. `browser_exec`, `browser_cdp`), `vision_analyze`, `computer_use`, `cronjob_manage`, `skill_manage`, `web_*`, `memory` | host | **none** |

The guard hook's matcher is `kanban_.*|read_file|write_file|patch|search_files|terminal|process_manage|execute_code`.
Any tool not named there runs without the guard.

**Tool calls made from inside `execute_code`** go through `model_tools.handle_function_call`, so the hooks fire
again for them. Only web, read/write/search/patch and terminal are available to a script this way.

**Environment.** Hermes scrubs secret-named variables from child processes. In a test set that included
`GROQ_API_KEY`, `FED_MODEL_KEY_*` and `TELEGRAM_BOT_TOKEN`, an `execute_code` child kept only `PATH`,
`LOCALAPPDATA` and `HERMES_HOME`.

Name-based scrubbing does not protect files, though. The child still knows `HERMES_HOME`, so it can read `.env`
and `auth.json` from disk as the same user.

## 4. Threat model and assumptions

**Adversary.** A misbehaving or prompt-injected agent with its configured tools. Terminal and code execution
stay enabled (Proposal B was cancelled). It can write arbitrary code, start background and child processes, and
wait.

**Assets to protect:**
* other companies' workspaces and Kanban boards;
* the federation databases (`memory.db`, `workflow.db`, `sessions.db`, `conversations.db`);
* `owner.json`, `.env` files and `auth.json`;
* the backups;
* the profile control files;
* the guard and Hermes source;
* the local services (Console on 127.0.0.1:8765, Ollama on 11434);
* the Docker engine.

**Trusted:** the owner, the Hermes host process as shipped, the Console, the Windows kernel and the Docker VM.

**Out of scope:**
* malicious model providers;
* physical access;
* Windows kernel exploits;
* supply-chain attacks on images (mitigated only by digest pinning).

**Assumption.** UAC stays at its default (ConsentPromptBehaviorAdmin = 5, secure desktop). An agent at Medium
integrity therefore cannot elevate without a visible consent prompt.

## 5. Findings

Legend:
* **guard** = what `fed_guard.py` decides.
* **OS** = what an ordinary child or grandchild process of the same user achieved on a synthetic canary.

All probe evidence comes from `python -I security/isolation_probe.py`, executed 2026-10-29. It works in a fresh
temporary folder that is deleted afterwards. No real file content, database or service was touched.

### OPEN-1 — filesystem isolation: **High**

| Case | guard | OS |
|---|---|---|
| file tool, own workspace (relative) | allowed | read (authorised path works) |
| file tool, absolute path to another company | denied | read |
| file tool, `../` traversal | denied | read |
| file tool, federation DB / backups | denied | read |
| file tool, junction `workspace\vendor` → other company (`mklink /J`, no admin needed) | denied | read |
| **TOCTOU: plain folder at check time, junction at use time** | **allowed** | **read** |
| shared `%TEMP%` (same user for all agents) | not covered | read |

Further evidence:
* Junctions **and** symbolic links can be created without elevation on this host.
* The file tools are themselves shell commands that run after the hook. A background process can swap a checked
  folder for a junction in between. This is a race, so it is not reliable, but it is real.
* Git worktree `.git` files carry absolute Windows paths, e.g.
  `gitdir: C:/Users/aliab/hermes-federation/workspaces/auvia/.git/worktrees/t_a41140f8`.

The file-tool path check is enforced, but only at application level. Nothing is enforced by the OS.

### OPEN-2 — subprocess and code-execution isolation: **Critical**

| Case | guard | OS |
|---|---|---|
| terminal, literal path to another company / federation DB / `.env` / `owner.json` | denied | read |
| **terminal, run-time-built path (string join)** | **allowed** | **read** |
| **terminal, wildcard (`fed*/mem*.d?`)** | **allowed** | **read** |
| **terminal, PowerShell `-EncodedCommand`** | **allowed** | **read** |
| **execute_code, run-time-built path** | **allowed** | **read** |
| **grandchild process reading another company** | no hook exists | **read** |
| **terminal, literal path to another company's `kanban/boards/<co>/kanban.db`** | **allowed** | **read** |
| **terminal, literal path to Hermes `auth.json`** | **allowed** | **read** |

Further evidence:
* A grandchild process could reach a loopback service. The probe used a throwaway listener standing in for the
  Console or Ollama. At probe time the real Console and Ollama were not listening, so neither was contacted.
* The user is in `docker-users`. Whenever Docker Desktop runs, any agent command could start a container that
  mounts `C:\`, which equals full access to the host's files.
* Child processes inherit the user's full token and NTFS rights.
* Environment scrubbing removes secret **variables**, not secret **files**.

Nothing here is enforced by the OS. The text match is defence in depth only.

### OPEN-3 — cross-company and cross-agent isolation, control-file and guard integrity: **Critical**

| Case | guard | OS |
|---|---|---|
| file tool, write own `config.yaml` (remove own guard) | denied | written |
| terminal, literal write to own `config.yaml` | denied | written |
| **terminal, built path to own `config.yaml`** | **allowed** | **written** |
| file tool, another agent's config | denied | written |
| terminal, overwrite guard script (literal name) | denied | written |
| **terminal, overwrite guard script (wildcard `fed_g*.py`)** | **allowed** | **written** |
| **terminal, patch Hermes source (hook dispatcher)** | **allowed** | **written** |
| terminal, modify backups | denied | written |

Further evidence:
* A read-only `icacls` check shows NTFS gives the shared user full control of the Hermes home, the workspaces,
  the guard source, the backups and the Console settings folder.
* `shell-hooks-allowlist.json` pins only the hook's command string (`script_mtime_at_approval: null`), so a
  changed `fed_guard.py` is not detected.
* Company separation, federation-admin vs. company separation, and per-agent profile separation all exist only
  inside the guard and the memory server, i.e. at application level.

**Not verified live (pending; each needs a real agent run).** All three tools are outside the hook matcher:
* whether the local **browser** tools can open `file://` or `127.0.0.1` URLs (the URL-safety check appears to
  apply to cloud browser sessions only);
* what `computer_use` can reach on the shared desktop;
* whether `vision_analyze` reads arbitrary local paths.

### Controls today

| Control | Type | Enforced by the OS? |
|---|---|---|
| File-tool path boundary (realpath, secrets, control files) | application hook, fail-closed | no |
| Kanban board/assignee/link rules | application hook | no |
| Command text bans | application hook, defence in depth | no |
| Memory scopes, customer binding, work orders | memory MCP server | no (the DB file itself is readable by commands) |
| Console owner auth, sessions, audit | Console process | no (port reachable locally) |
| Secret-variable scrubbing for children | Hermes | no (files remain readable) |
| UAC elevation prompt | Windows | **yes** (prevents silent admin) |
| Anything separating company A from company B | — | **none** |

## 6. Architecture alternatives

The five options:
* **A1:** per-agent (per-session) containers.
* **A2:** per-company containers, with the guard kept for agent-level rules.
* **A3:** one Windows user per company, with NTFS ACLs.
* **A4:** one VM per company.
* **A5 (recommended):** a hybrid — A2 now on the laptop, then a Linux host with per-company users and rootless
  containers.

| | A1 | A2 | A3 | A4 | A5 |
|---|---|---|---|---|---|
| **Boundary** | container (Linux namespaces and cgroups in the Docker VM), one per session | container, one per company | Windows user + ACL | hypervisor | A2 now; per-company Linux users + rootless containers later |
| **Stops OPEN-1/2/3 for commands** | yes | yes between companies; agents of one company share | yes between companies, if every process runs as that user | yes | yes |
| **Mounts** | the task worktree (rw), or a clean execution copy | `workspaces/<co>` (rw) at a fixed path | ACL: own workspace rw, the rest denied | own disk | as A2 |
| **Federation DBs, backups, profiles, `.env`** | not mounted | not mounted | ACL deny (but the Hermes process needs its profile `.env`) | not present | not mounted |
| **Network** | `--network none` by default (Hermes `docker_network: false`) | same; optional egress-only network | no per-user network control on Windows Home without firewall rules | per VM | `none`; `fed-web` later |
| **Limits** | Hermes `container_cpu/memory/disk`, pids limit, cap-drop ALL, no-new-privileges, tmpfs | same, shared per company | Job objects only; not configurable in Hermes | VM size | as A2 |
| **Terminal, code and file tools** | all run in the container (Hermes routes file tools through the backend) | same | unchanged, but Hermes must run as that user | all inside the VM | as A2 |
| **Hermes compatibility** | native (`terminal.backend: docker`) | native (reuses a labelled long-lived container) | **not supported**: needs a separate Hermes install or launch per user, and the Kanban dispatcher spawns workers as the owner today | **large redesign**: Hermes per VM, and the Kanban and federation DBs must then be shared over the network | native |
| **Guard** | must become container-aware (section 7) | same | unchanged | n/a | same |
| **Ops complexity** | high churn; start latency per session | medium | high: Windows Home has no local-users GUI, and secrets must be split per user | very high | medium, then medium-high |
| **Resources** | many short-lived containers | 5 containers (+1 federation) | none extra | 5+ VMs; not feasible on 16 GB, and Windows Home has no Hyper-V manager | ~5 × 1–2 GB RAM on the laptop |
| **Cost** | laptop: 0 | laptop: 0 | 0 | cloud VMs ≈ USD 150–400/month (estimate) | 0 now; server later ≈ USD 40–80/month (estimate) |
| **Failure modes** | container start failures block tasks | one company's container down = that company blocked | ACL drift silently re-opens access | network or DB coupling | as A2 |

Two rows that do not fit the table:
* **Browser vs. light image.** A1 and A2 use the light image by default and the browser image only per role.
  A3 keeps the host browser as it is. A4 has one per VM. A5 follows A1/A2, and keeps the Hermes browser tool on
  the host but disabled for containerised roles until it is tested.
* **Rollback.** A1, A2 and A5: restore `config.yaml` (backend `local`). A3: remove the users and reset the ACLs.
  A4: delete the VMs.

**Why Docker alone is not enough.** Docker is a boundary only if all four conditions hold:
1. the agent's commands actually run inside it;
2. the Docker engine is unreachable from inside;
3. nothing secret is mounted;
4. the host-side tools outside the container (browser, computer use, vision, MCP servers) are restricted
   separately.

WSL2 itself is **not** a boundary on this host: WSL interop can run Windows `.exe` files, and `/mnt/c` is
automounted. Only containers that Hermes starts with explicit mounts and no interop are a boundary.

**Why A5:**
* A2 gives a real OS boundary between companies on the laptop, at no cost, and is native to Hermes.
* A1's per-session churn is not needed when the company is the trust unit.
* A3 does not fit how Hermes spawns workers.
* A4 is too heavy.
* Separation between agents of the same company stays application-level (the guard). That is acceptable because
  agents of one company may already share that company's workspace.
* The Linux stage adds separate OS users per company and rootless Docker for production.

## 7. Blockers and constraints

**1. The guard runs on the host, but under a container backend the file tools run in the container.**
The guard's `realpath` check would resolve container paths such as `/workspace/x` on the Windows host. It would
find them outside the allowed roots and deny them, so all file tools would break.
Required change (needs approval): a container-aware mode in `fed_guard.py`. It maps the container path prefix to
the company mount and checks lexically, without a host `realpath`. The container mount itself then becomes the
boundary.

**2. Guard and Hermes integrity.** Before the pilot, the guard script, the hook allowlist and the Hermes source
must be out of reach of any containerised agent. Under A2 they are never mounted, which solves this for
containerised agents. Agents still on the `local` backend during a staged rollout keep today's risk.

**3. Git worktrees.** This was tested on a synthetic repo; the real repositories were only read.
* The problem: worktree `.git` files hold absolute Windows paths, which do not resolve inside Linux.
* Git 2.50 supports `worktree.useRelativePaths=true`. With it the pointers become `../main/.git/worktrees/…`.
  They work as long as the whole `workspaces/<co>` folder is mounted with the same layout, which is the A2 mount
  anyway.
* Existing worktrees keep their absolute paths. They are not repaired; new tasks simply get new worktrees.
* Alternatives:
  * (b) A dedicated execution checkout: `git clone` the task branch into a sandbox folder, then exchange changes
    back with `git format-patch` / `git am`. Verified to work in a temp repo. The cost is an extra patch step in
    the workflow.
  * (c) On the Linux host, identical absolute paths.
* `core.autocrlf=true` is set on the host, so Linux Git in the container would show line-ending noise. Fix it with
  `.gitattributes` per repository, or keep running Git on the host side.
* Recommendation: relative paths for new worktrees plus the per-company mount, with (b) as the fallback. Enabling
  relative paths is a Git configuration change and needs approval.

**4. Docker engine access.** The shared user is in `docker-users`, so agents on the `local` backend could drive
Docker once it runs. Mitigation: migrate all agents in the same stage, or keep Docker Desktop stopped until all
of them are migrated. Never mount the Docker pipe or socket.

**5. Host-side tools outside the hook.** The browser, `computer_use` and `vision_analyze` must be tested. If they
reach local files or the desktop, they must be disabled or restricted for the containerised roles. That is a
toolset change and needs approval.

**6. Capacity.**
* RAM: 15.7 GB, shared with Ollama (`qwen3.5:9b`). Docker Desktop's VM defaults to about half of RAM, so
  `.wslconfig` must cap it (proposed: 6 GB). That is a host configuration change.
* Disk: 49 GB free. The light image is ~1 GB and the browser image ~2.5 GB.

**7. Licensing.** Docker Desktop is free for personal use and for small businesses (fewer than 250 employees and
less than USD 10 M revenue). Otherwise use Docker Engine on Linux.

## 8. Staged implementation plan (each stage needs approval)

| Stage | Work | Approval |
|---|---|---|
| 0 | Supervisor decisions (section 11) | — |
| 1 | `.wslconfig` memory cap; start Docker Desktop; pull the light image, pinned by digest (~1 GB) | host config + download |
| 2 | Container-aware guard mode with unit tests; no profile changes yet | code change |
| 3 | Pilot on a **copy**: a harness Hermes home with the ISC profiles on backend `docker`, one long-lived ISC container, network `none`, `workspaces/isc` mounted | none beyond stages 1–2 (copies only) |
| 4 | Run tests T1–T12 (section 9) on the pilot; report | — |
| 5 | Switch the real ISC worker profiles, using a script with a per-file backup; the fingerprint diff must be limited to the backend lines | profile change |
| 6 | All companies' workers, then the business roles, then the federation roles. Docker access from host-side agents ends when the last profile moves | profile change |
| 7 | `fed-web` egress network, browser image, Linux host | each separately |

## 9. Verification plan

Status labels:
* **Executed** — run during this review, with evidence.
* **Mocked** — application-level only.
* **Proposed** — needs the sandbox.

Every sandbox test runs against **synthetic canaries planted outside the mount**. It uses a real agent rather
than a unit test, and is scripted so it can be repeated.

The "Current host" column shows the result of the 2026-10-29 probe run ("probe executed" below means that
result). None of these is a sandbox result.

| # | Test | Setup | Expected (pass criterion) | Evidence | Current host | Sandbox needs |
|---|---|---|---|---|---|---|
| T1 | Authorised workspace read and write keep working | pilot container, ISC worktree | the agent reads, writes, patches and runs tests in its workspace; Git works in a new worktree | tool results, `git status` | works (probe row 1) | stages 1–3 |
| T2 | Unauthorised absolute path denied | canary in `workspaces/auvia` | guard denies (file tools); the OS gives *not found* (commands) | tool error text, audit row | guard denies, OS allows | stages 1–3 |
| T3 | Relative traversal denied | `../../auvia/...` from `/workspace` | not found | as T2 | guard denies, OS allows | stages 1–3 |
| T4 | Symlink and junction escape denied | the agent runs `ln -s /etc /workspace/x` and `ln -s ../auvia`; a host-side helper creates a Windows junction inside the mount | links resolve inside the container namespace only; no host data outside the mount; host-created junctions inside the mount do not resolve to other companies (Docker bind-mount behaviour, to verify) | `readlink`, `cat` | guard denies, OS allows; TOCTOU allowed | stages 1–3 |
| T5 | Subprocess and grandchild reach no canary | `python -c` building the path at run time; `sh -c` → `python` → `cat` | not found | output | read | stages 1–3 |
| T6 | Cross-company access denied | ISC and AUVIA containers running at the same time | neither sees the other's files or processes (`ps`, `/proc`) | output | read | stages 1–3 + a second company |
| T7 | Control files and guard cannot be changed from execution | try to write the profile `config.yaml`, `fed_guard.py`, the hook allowlist and Hermes source from inside | none of them exists in the container; their host hashes are unchanged | `sha256` before and after | written (on synthetic copies) | stages 1–3 |
| T8 | Secrets, databases, backups and host resources unreachable | search for `memory.db`, `.env`, `auth.json`, `owner.json`, backups; run `env`; look for `/mnt/c` | none found; `env` has no `*_KEY`, `*TOKEN` or `PASSWORD`; no `/mnt/c`; no Docker socket | output | read | stages 1–3 |
| T9 | Network policy | connect to `host.docker.internal:8765`, `:11434`, the LAN gateway and `1.1.1.1:443` | all fail under `none`; later, under `fed-web`: internet yes, host and private ranges no | output | loopback reachable | stages 1–3; `fed-web` also needs a firewall approval |
| T10 | Resource limits | fork bomb; 3 GB allocation; a 12 GB file in `/tmp` | stopped by the pids limit / killed / quota hit; host stays responsive; Ollama unaffected | `docker stats`, host responsiveness | not run | stages 1–3 |
| T11 | Regression | one M1 task end to end in the pilot | guard, Kanban, memory MCP, review gate and verdict all work; the permission fingerprint changes only by the approved backend lines | task events, fingerprint diff | not run | stages 1–3 |
| T12 | Host-side tools outside the hook | browser on `file://` and `http://127.0.0.1:8765`; `vision_analyze` on a host path; `computer_use` | refused, or the tool is disabled for the role | tool output | not run | a live agent + a toolset decision |

**Status.**
* Executed: the current-host column of T1–T9 only. In the probe these ran at application level (the guard) plus
  as ordinary child processes, not as real agent runs.
* Proposed: every sandbox test, T1–T12.
* Mocked: nothing.

**Acceptance.** All three conditions must hold:
* T1–T10 pass for every company in the pilot;
* T11 shows no regression;
* T12 is resolved.

Only then are OPEN-1/2/3 marked closed, and the OPEN cases in `test_security.py` replaced by these execution
tests.

## 10. Rollback

* **Per profile:** restore `config.yaml` from its backup (`terminal.backend: local`). It takes effect on the
  agent's next start; nothing else changes.
* **Containers:** `docker ps -a --filter label=hermes…`, then `docker rm -f`.
* **Images:** `docker image rm`.
* **Docker Desktop:** stop it. Remove the `.wslconfig` cap if wanted.
* **Guard change:** the container-aware mode is opt-in per hook command, so removing the flag restores today's
  behaviour.
* **Data:** no federation database or workspace content changes during any stage, so no data rollback is needed.
* **Full fallback:** the Phase 5 checkpoint `hermes-backups\phase5-checkpoint-20261028-1808` (all profile files,
  databases, owner file and patch).

## 11. Decisions required from the supervisor

1. Approve architecture **A5** (per-company containers now, Linux host later), or choose another option.
2. Approve stage 1:
   * the `.wslconfig` memory cap (proposed: 6 GB);
   * starting Docker Desktop;
   * the light image download (~1 GB);
   * optionally the browser image (~2.5 GB).
3. Approve the guard code change (container-aware mode) in stage 2.
4. Choose the Git approach: `worktree.useRelativePaths=true` for new worktrees, or the execution checkout with
   patch exchange.
5. Host-side tools for the containerised roles (browser, `computer_use`, `vision_analyze`): keep them disabled
   until T12 passes, or test them first.
6. Network policy: `none` only for now, or `fed-web` egress as well (a firewall change).
7. Rollout scope and order:
   * which company is the stage-5 pilot;
   * whether Docker Desktop stays stopped until every agent is migrated (because of `docker-users`).
8. Later, and separately: the Linux host purchase and Docker licensing.

**Interim measures (proposal only, not done).** These change no permission:
* move `fed_guard.py` out of OneDrive, to a location the agents' commands are not told about;
* add the guard's directory and `hermes-agent` to the command ban list;
* add `kanban/boards/` (other companies) and `auth.json` to the command ban list.

They narrow the literal gaps only and do not close any OPEN item.

---

## Appendix A — v1 (2026-10-28), unchanged

> Kept for history. Where v1 and v2 differ, v2 applies (see the version table).

### Proposal A — operating-system isolation for agent commands

**Status: proposal for approval. Nothing here is implemented.** OPEN-1, OPEN-2 and OPEN-3 stay open until an
approved sandbox passes the real isolation tests in section 9.
Prepared 2026-10-28 (Phase 5.4) by Spectra Group engineering for the Hermes Federation owner.

#### 1. The problem (what is proven today)

`security/test_security.py` shows, by real execution against canary and disposable data:

| Case | What happens today |
|---|---|
| OPEN-1 | A `terminal` / `execute_code` command that builds a path at run time (e.g. joins strings) can read the federation canary database. The guard only sees the command text. |
| OPEN-2 | A subprocess started by an agent can read another company's workspace file. |
| OPEN-3 | Every agent runs as the same Windows user, so anything that user can read, any agent command can read. |

What *is* a real boundary today: the structured file tools (`read_file`, `write_file`, `patch`, `search_files`) and the
Kanban tools, checked by `fed_guard.py` with `realpath` before every call, fail-closed. Commands are defence in
depth only. Proposal B (removing terminal/code execution) was cancelled by the owner; this proposal keeps full
terminal and code execution and moves them into a sandbox.

#### 2. Goal and non-goals

Goal: an agent's terminal and code-execution commands run in a container that can see **only its own company's
workspace**, holds **no secrets**, has **bounded resources** and **controlled network**, so OPEN-1/2 become
impossible by construction and OPEN-3 no longer applies to commands.

Non-goals (unchanged): the 118 agents, identities, prompts, tools and permissions; the guard (stays, as the first
layer); model calls (stay in the Hermes host process, so containers need no API keys); Ollama settings.

#### 3. Architecture

Hermes v0.22.2 already has a Docker terminal backend (`terminal.backend: docker`, see each profile's
`config.yaml`): per session it starts a container from `docker_image`, mounts the working directory at
`/workspace` (`docker_mount_cwd_to_workspace`), applies its security defaults plus `docker_extra_args`, the
`container_cpu` / `container_memory` / `container_disk` limits, and forwards only the variables listed in
`docker_forward_env`.

```
 Windows host (owner user)                         Linux VM (WSL2 / Docker Desktop) or Linux server
 ┌──────────────────────────────┐                 ┌──────────────────────────────────────────────┐
 │ Hermes process per agent     │  docker run     │ container  fed-isc-<session>                 │
 │  - model calls (keys here)   │ ──────────────► │  /workspace  = workspaces/isc/<worktree> (rw)│
 │  - file tools + fed_guard    │                 │  no other mounts, no .env, uid 1000          │
 │  - Kanban tools, memory MCP  │                 │  --network none (default)                    │
 │ Console 127.0.0.1:8765       │                 │  cpu 1, mem 2 GB, pids 256, read-only root   │
 └──────────────────────────────┘                 └──────────────────────────────────────────────┘
```

* One container per agent session (Hermes default), labelled with the agent and company; ephemeral
  (`container_persistent: false`) so nothing leaks between sessions.
* Only the commands (`terminal`, `process_manage`, `execute_code`) move into the container. The structured file
  tools keep running on the host behind the guard. To be verified in the trial: that `execute_code` follows the
  terminal backend (the Hermes docs say remote backends run it per call).

#### 4. Mounts

| Path in container | Source | Mode | Who |
|---|---|---|---|
| `/workspace` | the task's worktree under `workspaces/<company>/.worktrees/...` | read-write | the company's worker agents |
| (nothing else) | — | — | — |

Never mounted: `%LOCALAPPDATA%\hermes` (profiles, `.env`, Kanban DBs, federation DBs), `hermes-backups`, other
companies' workspaces, the user profile, the Docker socket.

Known problem to solve in the trial: a Git worktree's `.git` file holds an absolute Windows path to the main
repository, so `git` fails inside the container. Options: (a) Git ≥ 2.48 relative worktree paths
(`worktree.useRelativePaths`) and mount the company workspace root read-only plus the worktree read-write;
(b) on a Linux server, mount at the same absolute path. Option (a) is preferred for the laptop trial.

#### 5. Resource limits (starting values, tuned in the trial)

`container_cpu: 1`, `container_memory: 2048` (MB; 4096 for QA/devops), `container_disk: 10240`;
`docker_extra_args`: `--pids-limit 256`, `--read-only` with `--tmpfs /tmp:size=512m`, `--cap-drop ALL`,
`--security-opt no-new-privileges`; `docker_run_as_host_user` off, image user uid 1000 (non-root).
Hermes `timeout` / `lifetime_seconds` stay as configured. With 16 GB RAM and Ollama on the laptop, at most
3 concurrent containers on this machine.

#### 6. Secrets

`docker_forward_env: []` for every profile. Model keys stay in each profile's host `.env`, read by the Hermes
host process only. No `.env`, `owner.json`, `auth.json` or database is ever inside a container. Tasks that need a
credential (e.g. a deploy token) get it only through an explicit, per-task, owner-approved forward — not in this
proposal.

#### 7. Network

| Mode | Use | How |
|---|---|---|
| `none` (default) | all coding, tests, builds with vendored dependencies | `--network none` |
| `fed-web` (opt-in per profile) | package installs, documentation lookups | a Docker network with egress to the internet only; RFC1918, link-local, `host.docker.internal`, the Console (8765) and Ollama (11434) blocked |

`fed-web` needs firewall rules inside the Docker VM (or server) — a firewall change, so it is a separate approval.
Phase A trial uses `none` only.

#### 8. Images

| Image | Contents | Size (approx.) | For |
|---|---|---|---|
| light | Python 3.11, Node 20, git, build tools (e.g. `nikolaik/python-nodejs:python3.11-nodejs20`, pinned by digest) | ~1 GB | all worker roles |
| browser | light + Playwright Chromium and fonts | ~2.5 GB | QA / executor tasks that run browser tests from the terminal |

The Hermes browser tool itself runs on the host and is not affected. Images are pulled once (download needs
approval), pinned by digest, and rebuilt only by the owner.

#### 9. Real isolation tests (pass criteria for closing OPEN-1/2/3)

Run by a **real agent** in a container (not a unit test), with canary files planted outside the mount:

1. OPEN-1: a command builds the canary DB path at run time and reads it → *file not found*.
2. OPEN-2: a subprocess reads another company's workspace file → *not found*.
3. OPEN-3: `whoami`, `id`, listing `C:\Users\<user>` / `/mnt/c` → non-root container user, no host paths.
4. `env` contains no `*_API_KEY`, token or password.
5. Network (mode `none`): connect to `127.0.0.1:8765`, `host.docker.internal:11434`, `1.1.1.1:443` → all fail.
6. Resources: fork bomb stops at the pids limit; a 3 GB allocation is killed; the host stays responsive.
7. Writes outside `/workspace` and `/tmp` fail (read-only root).
8. Two concurrent containers of different companies cannot see each other's files or processes.
9. Regression: the guard still blocks file-tool escapes; Kanban tools, memory MCP, review gate and one M1
   task run end to end; permission fingerprint changes only by the approved backend lines.

OPEN-1/2/3 are closed only when 1–8 pass for every company in the pilot and 9 shows no regression.

#### 10. Rollout and cost

| Step | What | Needs approval |
|---|---|---|
| 0 | Start Docker Desktop (installed: Docker 28.5.1, WSL2 present, both stopped) | yes |
| 1 | Pull the light image (~1 GB) | yes (download) |
| 2 | Pilot: ISC worker profiles only (`junior-developer`, `executor`, `qa`), backend `docker`, network `none`, by a script that backs up each `config.yaml` (like Models apply) | yes (backend change) |
| 3 | Section 9 tests; report | — |
| 4 | Extend to all companies' worker roles; then business roles | yes |
| 5 | Optional: `fed-web` network; browser image; Linux server | yes, each |

Cost: laptop trial — no purchase; disk ~3–5 GB; RAM as above. A Linux server later (4 vCPU, 16 GB, no GPU,
models via hosted APIs or Ollama over Tailscale) is typically USD 40–80 per month — estimate only, no purchase
proposed now. Docker Desktop is free for personal use and small businesses (under 250 employees and
USD 10 M revenue); otherwise a paid subscription or Docker Engine on Linux.

#### 11. Rollback

Per profile: restore `config.yaml` from its backup (`terminal.backend: local`) — immediate, no restart of other
agents. Then `docker ps -a --filter label=fed` → `docker rm -f`; images may stay or be removed with
`docker image rm`. Nothing in the federation databases or workspaces changes, so no data rollback is needed.
The Phase 5 checkpoint (`hermes-backups/phase5-checkpoint-20261028-1808`) holds every profile file.

#### 12. Risks

* Worktree paths in containers (section 4) — must be solved before the pilot passes.
* `execute_code` may still run on the host — if so, it needs the same treatment or stays guard-only (documented).
* Laptop capacity: Ollama + containers compete for 16 GB RAM.
* Docker Desktop VM networking makes `fed-web` filtering harder than on a Linux server.
* Container start adds seconds per session.
* The Hermes host process still runs as the owner's Windows user; isolation covers commands, not the Hermes
  process. Full separation of the process itself needs a Linux server with one user per company.

#### 13. Decisions requested from the owner

1. Approve the laptop trial (steps 0–3) or go straight to a Linux server.
2. Approve the image download (light only, or light + browser).
3. Approve the pilot profiles (ISC workers) and network `none`.
4. Later and separately: `fed-web` firewall rules; server purchase.
