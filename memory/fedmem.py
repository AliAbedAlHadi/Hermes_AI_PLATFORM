"""Hermes Federation scoped memory (R12.01-R12.05) with position-based override.

One SQLite database holds every memory layer. Permissions are decided here, in code, from the
caller's identity (its Hermes profile name, fixed in its config.yaml) — never from what the model says.

Scopes (string form):
  global                          everyone reads; written only through lesson promotion
  entity:<co>                     one company (federation management is co "federation")
  department:<co>:<dept>          one department of one company
  project:<co>:<slug>             members of one project
  agent:<profile>                 one agent's own memory
  admin:<co>:<group>              leadership memory, grouped by position (see ADMIN_GROUPS)
  customer:<co>:<cus_id>          one customer, held by its lead company (R12.01, R12.03)
  workorder:<wo_id>               the shared space of one cross-company work order (R11.05)

Cross-company work (R03.05, R11.05): companies never read each other's scopes. The lead company sends a
work order to a supplier; while it is accepted and active, the supplier's assigned team can read exactly
the records the work order grants, and both sides write in its workorder scope. Closing, cancelling or
revising the work order ends the grants.

Company keys are the profile prefixes: isc, auvia, tabbeqai, spectra, spectrasolutions, federation.

Customer binding: when an agent talks to an authenticated customer, the caller (console or channel
gateway) starts it with FED_CUSTOMER=<cus_id>. The memory server then shows that customer only, so a
customer cannot reach another customer's memory by asking the agent for it. Session scope is not built yet.
"""
import secrets
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OWNER = "owner"  # the human owner, acting through the Federation Console

COMPANY_PREFIX = {"isc": "isc", "auvia": "auvia", "tabbeq-ai": "tabbeqai",
                  "spectra": "spectra", "spectra-solutions": "spectrasolutions"}

# Every role belongs to one department. Edit here to move a role; the org version changes with it.
DEPARTMENTS = {
    "management": ["ceo", "delivery-director", "engineering-director"],
    "front-office": ["receptionist", "customer-success"],
    "business": ["business-analyst", "marketing"],
    "finance": ["finance"],
    "delivery": ["project-manager", "document-operations"],
    "engineering": ["solution-architect", "team-lead", "senior-developer", "junior-developer", "executor", "optimizer"],
    "quality": ["reviewer", "verifier", "qa"],
    "operations": ["devops", "release-controller"],
    "security": ["security"],
}
FED_DEPARTMENTS = {
    "federation-management": ["hermes-admin", "coo"],
    "federation-operations": ["modelops", "finops", "infra-recovery"],
    "federation-governance": ["knowledge", "security-policy", "audit"],
}

# Authority level: a higher level may override a lower one inside the same company.
LEVEL = {
    "ceo": 70,
    "delivery-director": 60, "engineering-director": 60,
    "project-manager": 50, "team-lead": 50, "solution-architect": 50,
    "senior-developer": 40, "business-analyst": 40, "security": 40, "finance": 40,
    "devops": 40, "release-controller": 40,
    "junior-developer": 30, "executor": 30, "optimizer": 30, "qa": 30, "reviewer": 30, "verifier": 30,
    "customer-success": 30, "marketing": 30, "document-operations": 30,
    "receptionist": 20,
}
FED_LEVEL = {"hermes-admin": 90, "coo": 80}  # other federation officers: 60
OWNER_LEVEL = 100

# Admin memory is grouped by position: a group is readable and writable from its minimum level up.
ADMIN_GROUPS = {"executive": 70, "directors": 60, "managers": 50}
FED_ADMIN_GROUPS = {"super": 90, "officers": 60}

# Roles that serve customers: they read and write customer memory of their own company.
CUSTOMER_ROLES = {"receptionist", "customer-success", "business-analyst", "project-manager", "delivery-director", "finance"}
CUSTOMER_ID = re.compile(r"^cus_[0-9a-f]{8}$")

# Work order lifecycle. draft waits for the lead CEO (who approves subcontracting); revision_pending waits for
# the supplier to accept the new requirements revision (R11.05: a changed input pauses dependent work).
WO_OPEN = ("draft", "requested", "accepted", "in_progress", "delivered", "revision_pending")
WO_GRANTING = ("accepted", "in_progress", "delivered")
WORK_ORDER_ID = re.compile(r"^wo_[0-9a-f]{8}$")
COMPANIES = tuple(COMPANY_PREFIX.values())

SUPER = {OWNER, "fed-hermes-admin"}                      # read everything
READ_ALL = SUPER | {"fed-audit"}                         # audit reads everything, writes nothing outside its scopes
STEWARDS = {OWNER, "fed-hermes-admin", "fed-knowledge"}  # promote lessons into global memory

KINDS = ("fact", "decision", "preference", "procedure", "lesson", "verdict", "owner_goal", "spend_policy")
OWNER_ONLY_KINDS = {"owner_goal", "spend_policy"}        # R13.01: no agent may erase owner goals or raise spending
VERDICT_ROLES = {"reviewer", "verifier"}                 # a verdict is replaced only by a new review, never by rank
STATUSES = ("claim", "verified")                         # R12.02: claims are kept apart from verified facts
CONFIDENTIALITY = ("internal", "confidential", "restricted")

SECRET_RE = re.compile(
    r"gsk_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[bpa]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:\b(?:password|passwd|api[_-]?key|secret|token)\s*[:=]\s*\S{6,})")


class Denied(Exception):
    pass


# ---------------------------------------------------------------- organisation

def default_roster():
    return HERE.parent / "roster" / "roster.json"


def load_org(roster_path=None):
    """Return {profile: agent} built from roster.json plus the department and level tables above."""
    roster = json.loads(Path(roster_path or default_roster()).read_text(encoding="utf-8"))
    role_dept = {r: d for d, roles in DEPARTMENTS.items() for r in roles}
    fed_dept = {r: d for d, roles in FED_DEPARTMENTS.items() for r in roles}
    agents = {}
    for f in roster["federation"]:
        key = f["id"].split(".", 1)[1]
        agents[f"fed-{key}"] = {"profile": f"fed-{key}", "company": "federation", "role": key,
                                "department": fed_dept[key], "level": FED_LEVEL.get(key, 60),
                                "display": f["display_name"]}
    for c in roster["companies"]:
        co = COMPANY_PREFIX[c["id"]]
        for t in roster["role_templates"]:
            role = t["role"]
            agents[f"{co}-{role}"] = {"profile": f"{co}-{role}", "company": co, "role": role,
                                      "department": role_dept[role], "level": LEVEL[role],
                                      "display": f"{t['display_name']} ({c['official_name']})"}
    agents[OWNER] = {"profile": OWNER, "company": "federation", "role": "owner",
                     "department": "federation-management", "level": OWNER_LEVEL, "display": "Owner"}
    return agents


def org_version(org):
    """Permission revision (R12.03): changes whenever a role, department or level changes."""
    blob = json.dumps({p: [a["company"], a["department"], a["level"]] for p, a in sorted(org.items())},
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def admin_groups(co):
    return FED_ADMIN_GROUPS if co == "federation" else ADMIN_GROUPS


# ---------------------------------------------------------------- policy (pure functions)

def parse_scope(scope):
    parts = scope.split(":")
    kind = parts[0]
    shape = {"global": 1, "entity": 2, "agent": 2, "department": 3, "project": 3, "admin": 3, "customer": 3,
             "workorder": 2}
    if kind not in shape or len(parts) != shape[kind] or not all(parts):
        raise Denied(f"bad scope '{scope}'")
    return parts


def scope_company(scope, org):
    parts = parse_scope(scope)
    if parts[0] == "global":
        return None
    if parts[0] == "agent":
        return org[parts[1]]["company"] if parts[1] in org else None
    if parts[0] == "workorder":
        return None
    return parts[1]


def customer_access(me, co, cus):
    """Customer memory: a bound session sees its own customer only; otherwise customer-facing roles,
    leaders (60+) and members of a project linked to the customer, all in the lead company."""
    if me.get("bound"):
        return cus == me["bound"] and co == me["company"]
    if me["company"] != co:
        return False
    return me["role"] in CUSTOMER_ROLES or me["level"] >= 60 or (co, cus) in me.get("via", ())


def can_read(me, scope, org, members):
    """members: set of (company, project) the caller belongs to; me["via"]: customers reached through them."""
    if me["profile"] in READ_ALL:
        return True
    parts = parse_scope(scope)
    if parts[0] == "customer":
        return customer_access(me, parts[1], parts[2])
    if parts[0] == "workorder":
        return not me.get("bound") and parts[1] in me.get("wos", ())
    kind, co, lvl = parts[0], me["company"], me["level"]
    if kind == "global":
        return True
    if me.get("bound"):  # in a customer conversation: global, the company and that customer only
        return kind == "entity" and parts[1] == co
    if kind == "agent":
        other = org.get(parts[1])
        # yourself, or a leader (level 50+) above you in the same company
        return parts[1] == me["profile"] or (
            other is not None and other["company"] == co and lvl >= 50 and lvl > other["level"])
    if parts[1] != co:  # entities are isolated from each other (R12.03)
        return False
    if kind == "entity":
        return True
    if kind == "department":
        return parts[2] == me["department"] or lvl >= 60
    if kind == "project":
        return (co, parts[2]) in members or lvl >= 60
    if kind == "admin":
        need = admin_groups(co).get(parts[2])
        return need is not None and lvl >= need
    return False


def can_write(me, scope, org, members):
    if me["profile"] == "fed-audit":  # audit observes, it does not change other scopes
        return scope in ("agent:fed-audit", "entity:federation")
    parts = parse_scope(scope)
    kind = parts[0]
    if kind == "global":
        return False  # only through lesson promotion
    if me["profile"] in SUPER:
        return kind != "agent" or parts[1] == me["profile"]
    if kind == "customer":
        return customer_access(me, parts[1], parts[2])
    if kind == "workorder":
        return not me.get("bound") and parts[1] in me.get("wos_open", ())
    co, lvl = me["company"], me["level"]
    if kind == "agent":
        return parts[1] == me["profile"]
    if parts[1] != co:
        return False
    if kind == "entity":
        return lvl >= 50
    if kind == "department":
        return parts[2] == me["department"] or lvl >= 60
    if kind == "project":
        return (co, parts[2]) in members or lvl >= 50
    if kind == "admin":
        need = admin_groups(co).get(parts[2])
        return need is not None and lvl >= need
    return False


def override_check(me, rec, org, members):
    """Return None if `me` may supersede `rec`, else the reason it may not."""
    if rec["superseded_by"] or rec["deleted"]:
        return "record is already superseded or deleted; override the current revision"
    if not can_read(me, rec["scope"], org, members):
        return "you cannot see this record"
    author = org.get(rec["author"], {"level": 0, "company": None, "role": ""})
    if rec["kind"] in OWNER_ONLY_KINDS:
        return None if me["profile"] == OWNER else "only the owner can change owner goals and spending policy"
    if rec["kind"] == "verdict":
        same_co = me["company"] == author["company"]
        if me["profile"] == OWNER or (me["role"] in VERDICT_ROLES and same_co):
            return None
        return "a review verdict is replaced only by a new review (reviewer/verifier), not by rank"
    if rec["scope"] == "global":
        return None if me["profile"] in STEWARDS else "global memory changes only through the knowledge steward"
    if me["profile"] == rec["author"]:
        return None  # correcting your own record
    if me["profile"] in SUPER:
        return None
    if me["company"] != author["company"]:
        return "cross-company override is not allowed; use a work order or ask Hermes Admin"
    if not can_write(me, rec["scope"], org, members):
        return "you cannot write to this scope"
    if me["level"] <= author["level"]:
        return f"your level ({me['level']}) is not above the author's ({author['level']}); escalate"
    return None


def readable_scopes_sql(me, org, members):
    """SQL filter applied BEFORE retrieval; rows are re-checked with can_read AFTER (R12.03)."""
    if me["profile"] in READ_ALL:
        return "1=1", []
    co, lvl = me["company"], me["level"]
    conds, args = ["scope = 'global'", "scope = ?"], [f"entity:{co}"]
    if me.get("bound"):
        conds.append("scope = ?"); args.append(f"customer:{co}:{me['bound']}")
        return "(" + " OR ".join(conds) + ")", args
    conds.append("scope = ?"); args.append(f"agent:{me['profile']}")
    wos = sorted(me.get("wos", ()))
    if wos:
        conds.append(f"scope IN ({','.join('?' * len(wos))})"); args += [f"workorder:{w}" for w in wos]
    grants = sorted(me.get("grants", ()))
    if grants:  # object grants from accepted work orders (R11.05)
        conds.append(f"id IN ({','.join('?' * len(grants))})"); args += grants
    if lvl >= 50:
        below = [p for p, a in org.items() if a["company"] == co and a["level"] < lvl]
        if below:
            conds.append(f"scope IN ({','.join('?' * len(below))})"); args += [f"agent:{p}" for p in below]
    if lvl >= 60:
        conds.append("scope LIKE ?"); args.append(f"department:{co}:%")
        conds.append("scope LIKE ?"); args.append(f"project:{co}:%")
    else:
        conds.append("scope = ?"); args.append(f"department:{co}:{me['department']}")
        mine = [p for c, p in members if c == co]
        if mine:
            conds.append(f"scope IN ({','.join('?' * len(mine))})"); args += [f"project:{co}:{p}" for p in mine]
    groups = [g for g, need in admin_groups(co).items() if lvl >= need]
    if groups:
        conds.append(f"scope IN ({','.join('?' * len(groups))})"); args += [f"admin:{co}:{g}" for g in groups]
    if me.get("bound"):
        conds.append("scope = ?"); args.append(f"customer:{co}:{me['bound']}")
    elif me["role"] in CUSTOMER_ROLES or lvl >= 60:
        conds.append("scope LIKE ?"); args.append(f"customer:{co}:%")
    else:
        via = sorted(c for c2, c in me.get("via", ()) if c2 == co)
        if via:
            conds.append(f"scope IN ({','.join('?' * len(via))})"); args += [f"customer:{co}:{c}" for c in via]
    return "(" + " OR ".join(conds) + ")", args


# ---------------------------------------------------------------- store

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL,
  company TEXT,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'claim',
  confidentiality TEXT NOT NULL DEFAULT 'internal',
  author TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  supersedes INTEGER REFERENCES records(id),
  superseded_by INTEGER REFERENCES records(id),
  change_reason TEXT NOT NULL DEFAULT '',
  retention_days INTEGER,
  deleted INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  org_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_scope ON records(scope, superseded_by, deleted);
CREATE TABLE IF NOT EXISTS project_members (
  company TEXT NOT NULL, project TEXT NOT NULL, agent TEXT NOT NULL,
  added_by TEXT NOT NULL, added_at REAL NOT NULL,
  PRIMARY KEY (company, project, agent)
);
CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY,                 -- cus_xxxxxxxx
  name TEXT NOT NULL,
  company TEXT NOT NULL,               -- lead company, accountable to the customer (R11.05)
  language TEXT NOT NULL DEFAULT 'ar',
  channel TEXT NOT NULL DEFAULT '',    -- whatsapp, telegram, email, portal
  handle TEXT NOT NULL DEFAULT '',     -- the channel identity (phone, username, address)
  details TEXT NOT NULL DEFAULT '',    -- important facts about the customer
  status TEXT NOT NULL DEFAULT 'active',
  login_hash TEXT,                     -- sha256 of the portal login code; the code itself is never stored
  created_by TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS customers_handle ON customers(company, channel, handle) WHERE handle != '';
CREATE TABLE IF NOT EXISTS project_customers (
  company TEXT NOT NULL, project TEXT NOT NULL, customer TEXT NOT NULL REFERENCES customers(id),
  PRIMARY KEY (company, project)
);
CREATE TABLE IF NOT EXISTS work_orders (
  id TEXT PRIMARY KEY,                       -- wo_xxxxxxxx
  title TEXT NOT NULL,
  lead_company TEXT NOT NULL,                -- accountable to the customer (R03.05)
  supplier_company TEXT NOT NULL,
  customer TEXT,                             -- optional cus_ id of the lead company's customer
  project TEXT,                              -- optional lead project slug
  work_scope TEXT NOT NULL,
  acceptance_criteria TEXT NOT NULL,
  permitted_sharing TEXT NOT NULL,
  outputs TEXT NOT NULL,
  cost_allocation TEXT NOT NULL,
  status TEXT NOT NULL,
  requirements_revision INTEGER NOT NULL DEFAULT 1,
  kanban_task TEXT NOT NULL DEFAULT '',      -- board:task_id on the supplier board
  delivery TEXT NOT NULL DEFAULT '',         -- delivery summary from the supplier
  delivery_revision INTEGER,                 -- requirements revision the delivery answers
  created_by TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS wo_grants (
  work_order TEXT NOT NULL REFERENCES work_orders(id), record INTEGER NOT NULL REFERENCES records(id),
  granted_by TEXT NOT NULL, PRIMARY KEY (work_order, record)
);
CREATE TABLE IF NOT EXISTS wo_team (
  work_order TEXT NOT NULL REFERENCES work_orders(id), agent TEXT NOT NULL, PRIMARY KEY (work_order, agent)
);
CREATE TABLE IF NOT EXISTS wo_events (
  id INTEGER PRIMARY KEY, work_order TEXT NOT NULL, ts REAL NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY,
  proposed_by TEXT NOT NULL, department TEXT NOT NULL, text TEXT NOT NULL,
  source_record INTEGER, state TEXT NOT NULL DEFAULT 'pending',
  decided_by TEXT, decided_at REAL, global_record INTEGER, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '', result TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
  org_version TEXT NOT NULL
);
"""


def default_db():
    if os.environ.get("FED_MEMORY_DB"):  # isolated deployments and tests
        return Path(os.environ["FED_MEMORY_DB"])
    home = os.environ.get("HERMES_HOME") or (
        os.path.join(os.environ["LOCALAPPDATA"], "hermes") if os.name == "nt" else os.path.expanduser("~/.hermes"))
    return Path(home) / "federation" / "memory.db"


class Memory:
    def __init__(self, profile, db_path=None, roster_path=None, viewer=None, customer=None):
        """viewer: set when the owner looks through an agent's eyes in the console; the audit shows both.
        customer: bind this session to one authenticated customer (see module docstring)."""
        self.org = load_org(roster_path)
        self.actor = f"{viewer} as {profile}" if viewer else profile
        if profile not in self.org:
            raise Denied(f"unknown agent '{profile}'")
        self.me = dict(self.org[profile])
        self.version = org_version(self.org)
        path = Path(db_path or default_db())
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        if customer:
            row = self.db.execute("SELECT company FROM customers WHERE id=? AND status='active'", (customer,)).fetchone()
            if row is None or row[0] != self.me["company"] or profile in READ_ALL:
                raise Denied(f"cannot bind {profile} to customer {customer}")
            self.me["bound"] = customer
            self.actor = f"{self.actor} for {customer}"

    # -- helpers
    def _members(self):
        rows = self.db.execute("SELECT company, project FROM project_members WHERE agent=?", (self.me["profile"],))
        members = {(r[0], r[1]) for r in rows}
        rows = self.db.execute("SELECT pc.company, pc.customer FROM project_customers pc JOIN project_members pm "
                               "ON pm.company = pc.company AND pm.project = pc.project WHERE pm.agent=?",
                               (self.me["profile"],))
        self.me["via"] = {(r[0], r[1]) for r in rows}
        self._work_order_reach(members)
        return members

    def _wo_side(self, wo, members=None):
        """'lead', 'supplier' or None: is the caller a participant of this work order, and on which side?"""
        me, co, lvl = self.me, self.me["company"], self.me["level"]
        if me.get("bound"):
            return None
        if wo["lead_company"] == co and (wo["created_by"] == me["profile"] or lvl >= 60 or (
                wo["project"] and (co, wo["project"]) in (members if members is not None else self._members()))):
            return "lead"
        if wo["supplier_company"] == co and (lvl >= 60 or self.db.execute(
                "SELECT 1 FROM wo_team WHERE work_order=? AND agent=?", (wo["id"], me["profile"])).fetchone()):
            return "supplier"
        return None

    def _work_order_reach(self, members):
        """Work orders the caller takes part in, which are open, and the records they grant to the caller."""
        co = self.me["company"]
        rows = self.db.execute("SELECT * FROM work_orders WHERE lead_company=? OR supplier_company=?", (co, co)).fetchall()
        wos, open_, grants = set(), set(), set()
        for wo in rows:
            side = self._wo_side(dict(wo), members)
            if not side:
                continue
            wos.add(wo["id"])
            if wo["status"] in WO_OPEN:
                open_.add(wo["id"])
            if side == "supplier" and wo["status"] in WO_GRANTING:
                grants |= {r[0] for r in self.db.execute("SELECT record FROM wo_grants WHERE work_order=?", (wo["id"],))}
        self.me["wos"], self.me["wos_open"], self.me["grants"] = wos, open_, grants

    def _audit(self, action, target, result, detail=""):
        self.db.execute("INSERT INTO audit (ts, actor, action, target, result, detail, org_version) VALUES (?,?,?,?,?,?,?)",
                        (time.time(), self.actor, action, str(target), result, detail[:500], self.version))
        self.db.commit()

    def _deny(self, action, target, why):
        self._audit(action, target, "denied", why)
        raise Denied(why)

    def resolve_scope(self, scope):
        """Accept short forms so agents need not spell their own company: entity, department, agent,
        project:<slug>, admin:<group>. Full forms pass through."""
        s = (scope or "").strip().lower()
        co = self.me["company"]
        short = {"entity": f"entity:{co}", "company": f"entity:{co}",
                 "department": f"department:{co}:{self.me['department']}", "agent": f"agent:{self.me['profile']}",
                 "me": f"agent:{self.me['profile']}", "global": "global"}
        if self.me.get("bound"):
            short["customer"] = f"customer:{co}:{self.me['bound']}"
        if s in short:
            return short[s]
        parts = s.split(":")
        if parts[0] == "workorder" and len(parts) == 2:
            return s
        if parts[0] == "customer" and len(parts) == 2:
            return f"customer:{co}:{parts[1]}"
        if parts[0] in ("project", "admin") and len(parts) == 2:
            return f"{parts[0]}:{co}:{parts[1]}"
        if parts[0] == "department" and len(parts) == 2:
            return f"department:{co}:{parts[1]}"
        parse_scope(s)
        return s

    def _check_text(self, *texts):
        for t in texts:
            if t and SECRET_RE.search(t):
                self._deny("secret", "", "text looks like it contains a secret (key, token or password); "
                           "secrets never go in memory")

    def _row(self, rid):
        r = self.db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
        if r is None:
            raise Denied(f"record {rid} not found")
        return dict(r)

    @staticmethod
    def _public(r):
        keys = ("id", "scope", "kind", "title", "body", "tags", "status", "confidentiality", "author", "source",
                "revision", "supersedes", "superseded_by", "change_reason", "created_at")
        out = {k: r[k] for k in keys}
        out["created_at"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"]))
        return out

    # -- operations
    def whoami(self):
        members = self._members()
        sql, args = readable_scopes_sql(self.me, self.org, members)
        me = dict(self.me)
        me["projects"] = sorted(p for c, p in members)
        me.pop("via", None)
        for k in ("wos", "wos_open", "grants"):  # sets are internal; report them as sorted lists
            me[k] = sorted(me.get(k) or [])
        if self.me.get("bound"):
            me["serving_customer"] = self._customer_row(self.me["bound"], full=True)
        me["org_version"] = self.version
        if self.me["profile"] in READ_ALL:
            me["can_read"] = "every scope"
        else:
            co = self.me["company"]
            me["can_read"] = ["global", f"entity:{co}", f"agent:{self.me['profile']}"] + [
                a for a in args if a.startswith(("department", "project", "admin", "customer"))]
        me["write_short_forms"] = ["agent", "department", "entity (level 50+)", "project:<slug>", "admin:<group>"]
        me["admin_groups_for_me"] = [g for g, n in admin_groups(self.me["company"]).items() if self.me["level"] >= n]
        return me

    def write(self, scope, kind, title, body, status="claim", confidentiality="internal", source="", tags="",
              retention_days=None, _supersedes=None, _reason="", _revision=1):
        scope = self.resolve_scope(scope)
        if self.me.get("bound"):
            # Everything learned in a customer conversation belongs to that customer, never to the agent's
            # own notes (shared by its next customers) or the company.
            scope = f"customer:{self.me['company']}:{self.me['bound']}"
        if kind not in KINDS:
            raise Denied(f"kind must be one of {', '.join(KINDS)}")
        if status not in STATUSES or confidentiality not in CONFIDENTIALITY:
            raise Denied("bad status or confidentiality")
        if not title.strip() or not body.strip():
            raise Denied("title and body are required")
        self._check_text(title, body, source, tags)
        if _supersedes is None:
            if kind in OWNER_ONLY_KINDS and self.me["profile"] != OWNER:
                self._deny("write", scope, "only the owner records owner goals and spending policy")
            if kind == "verdict" and self.me["role"] not in VERDICT_ROLES and self.me["profile"] != OWNER:
                self._deny("write", scope, "only reviewer and verifier agents record verdicts")
            if not can_write(self.me, scope, self.org, self._members()):
                self._deny("write", scope, f"{self.me['profile']} may not write to {scope}")
        cur = self.db.execute(
            "INSERT INTO records (scope, company, kind, title, body, tags, status, confidentiality, author, source, "
            "revision, supersedes, change_reason, retention_days, created_at, org_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scope, scope_company(scope, self.org), kind, title.strip(), body.strip(), tags, status, confidentiality,
             self.me["profile"], source, _revision, _supersedes, _reason, retention_days, time.time(), self.version))
        rid = cur.lastrowid
        self.db.commit()
        self._audit("write" if _supersedes is None else "override", scope, "ok", f"record {rid}")
        return {"id": rid, "scope": scope}

    def search(self, query="", scope=None, kind=None, limit=10, include_history=False):
        members = self._members()
        where, args = readable_scopes_sql(self.me, self.org, members)
        where = [where, "deleted = 0"]
        if not include_history:
            where.append("superseded_by IS NULL")
        if scope:
            where.append("scope = ?"); args.append(self.resolve_scope(scope))
        if kind:
            where.append("kind = ?"); args.append(kind)
        for term in [t for t in re.split(r"\s+", query or "") if t][:6]:
            where.append("(title LIKE ? OR body LIKE ? OR tags LIKE ?)"); args += [f"%{term}%"] * 3
        rows = self.db.execute(f"SELECT * FROM records WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?",
                               (*args, max(1, min(int(limit or 10), 50)))).fetchall()
        out = [self._public(dict(r)) for r in rows
               if can_read(self.me, r["scope"], self.org, members) or r["id"] in self.me.get("grants", ())]
        self._audit("search", scope or "*", "ok", f"q={query!r} n={len(out)}")
        return out

    def get(self, rid):
        r = self._row(rid)
        if not can_read(self.me, r["scope"], self.org, self._members()) and rid not in self.me.get("grants", ()):
            self._deny("get", rid, "you cannot see this record")
        chain, cur = [], r
        while cur["supersedes"]:
            cur = self._row(cur["supersedes"])
            chain.append({k: cur[k] for k in ("id", "author", "revision", "change_reason")} | {"body": cur["body"]})
        self._audit("get", rid, "ok")
        return {"record": self._public(r), "history": chain}

    def override(self, rid, body, reason, title=None, status=None):
        """Supersede a record with a new revision. The old one stays, marked superseded (R12.02)."""
        rec = self._row(rid)
        if not (reason or "").strip():
            raise Denied("a reason is required for every override")
        why = override_check(self.me, rec, self.org, self._members())
        if why:
            self._deny("override", rid, why)
        new = self.write(rec["scope"], rec["kind"], title or rec["title"], body, status or rec["status"],
                         rec["confidentiality"], rec["source"], rec["tags"], rec["retention_days"],
                         _supersedes=rid, _reason=f"{reason.strip()} (by {self.me['profile']})",
                         _revision=rec["revision"] + 1)
        self.db.execute("UPDATE records SET superseded_by=? WHERE id=?", (new["id"], rid))
        self.db.commit()
        return {"id": new["id"], "supersedes": rid, "revision": rec["revision"] + 1}

    def project(self, action, project, agent=None):
        project = re.sub(r"[^a-z0-9-]", "-", (project or "").lower()).strip("-")
        if not project:
            raise Denied("project slug required")
        co = self.me["company"]
        if action == "list":
            rows = self.db.execute("SELECT agent, added_by FROM project_members WHERE company=? AND project=?", (co, project))
            return [dict(r) for r in rows]
        if self.me["level"] < 50 and self.me["profile"] not in SUPER:
            self._deny(f"project.{action}", project, "only managers (level 50+) manage project members")
        target = self.org.get(agent or "")
        if target is None:
            raise Denied(f"unknown agent '{agent}'")
        if self.me["profile"] not in SUPER and target["company"] != co:
            self._deny(f"project.{action}", project, "agents from another company join through a work order")
        tco = target["company"]
        if action == "add":
            self.db.execute("INSERT OR IGNORE INTO project_members VALUES (?,?,?,?,?)",
                            (tco, project, agent, self.me["profile"], time.time()))
        elif action == "remove":
            self.db.execute("DELETE FROM project_members WHERE company=? AND project=? AND agent=?", (tco, project, agent))
        else:
            raise Denied("action must be add, remove or list")
        self.db.commit()
        self._audit(f"project.{action}", f"project:{tco}:{project}", "ok", agent)
        return {"ok": True, "scope": f"project:{tco}:{project}"}

    def lesson(self, action, text=None, department=None, lesson_id=None, sanitized_text=None, source_record=None):
        """propose: any agent offers a lesson for all companies; approve/reject: the knowledge steward (R12.04)."""
        if action == "propose":
            if not (text or "").strip():
                raise Denied("text required")
            self._check_text(text)
            cur = self.db.execute("INSERT INTO lessons (proposed_by, department, text, source_record, created_at) "
                                  "VALUES (?,?,?,?,?)",
                                  (self.me["profile"], department or self.me["department"], text.strip(),
                                   source_record, time.time()))
            self.db.commit()
            self._audit("lesson.propose", cur.lastrowid, "ok")
            return {"lesson_id": cur.lastrowid, "state": "pending"}
        if self.me["profile"] not in STEWARDS:
            self._deny(f"lesson.{action}", lesson_id or "", "only the knowledge steward reviews lessons")
        if action == "list":
            rows = self.db.execute("SELECT * FROM lessons WHERE state='pending' ORDER BY id").fetchall()
            return [dict(r) for r in rows]
        row = self.db.execute("SELECT * FROM lessons WHERE id=? AND state='pending'", (lesson_id,)).fetchone()
        if row is None:
            raise Denied(f"no pending lesson {lesson_id}")
        if action == "reject":
            self.db.execute("UPDATE lessons SET state='rejected', decided_by=?, decided_at=? WHERE id=?",
                            (self.me["profile"], time.time(), lesson_id))
            self.db.commit()
            self._audit("lesson.reject", lesson_id, "ok")
            return {"lesson_id": lesson_id, "state": "rejected"}
        if action != "approve":
            raise Denied("action must be propose, list, approve or reject")
        final = (sanitized_text or row["text"]).strip()
        self._check_text(final)
        cur = self.db.execute(
            "INSERT INTO records (scope, company, kind, title, body, tags, status, confidentiality, author, source, "
            "created_at, org_version) VALUES ('global', NULL, 'lesson', ?, ?, ?, 'verified', 'internal', ?, ?, ?, ?)",
            (final.split("\n", 1)[0][:80], final, f"department:{row['department']}", self.me["profile"],
             f"lesson:{lesson_id}", time.time(), self.version))
        self.db.execute("UPDATE lessons SET state='approved', decided_by=?, decided_at=?, global_record=? WHERE id=?",
                        (self.me["profile"], time.time(), cur.lastrowid, lesson_id))
        self.db.commit()
        self._audit("lesson.approve", lesson_id, "ok", f"global record {cur.lastrowid}")
        return {"lesson_id": lesson_id, "state": "approved", "global_record": cur.lastrowid}

    # -- customers
    def _customer_row(self, cid, full=False):
        r = self.db.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
        if r is None:
            return None
        r = dict(r)
        r.pop("login_hash", None)
        r["has_login"] = bool(self.db.execute("SELECT login_hash FROM customers WHERE id=?", (cid,)).fetchone()[0])
        r["created_at"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"]))
        if full:
            r["projects"] = [x[0] for x in self.db.execute(
                "SELECT project FROM project_customers WHERE customer=? ORDER BY project", (cid,))]
        return r

    def _can_see_customer(self, cid):
        r = self.db.execute("SELECT company FROM customers WHERE id=?", (cid,)).fetchone()
        if r is None:
            return False
        self._members()
        return self.me["profile"] in READ_ALL or customer_access(self.me, r[0], cid)

    def customer(self, action, customer_id=None, name=None, channel=None, handle=None, language=None,
                 details=None, project=None):
        """register / find / get / list / update / link_project. Customers belong to the lead company."""
        co = self.me["company"]
        serving = self.me["role"] in CUSTOMER_ROLES or self.me["level"] >= 60 or self.me["profile"] in SUPER
        bound = self.me.get("bound")
        if action == "register":
            if bound:
                self._deny("customer.register", name or "", f"this customer is already registered as {bound}; "
                           "save what they tell you with memory_write (scope 'customer')")
            if not serving or self.me["profile"] == "fed-audit":
                self._deny("customer.register", name or "", "only customer-facing roles and leaders register customers")
            if not (name or "").strip():
                raise Denied("customer name required")
            self._check_text(name, details, handle)
            lead = co
            if self.me["profile"] in SUPER and customer_id in COMPANY_PREFIX.values():
                lead = customer_id  # the owner / Hermes Admin may register for any company
            cid = "cus_" + secrets.token_hex(4)
            try:
                self.db.execute("INSERT INTO customers (id, name, company, language, channel, handle, details, "
                                "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                (cid, name.strip(), lead, language or "ar", (channel or "").lower(), (handle or "").strip(),
                                 (details or "").strip(), self.me["profile"], time.time()))
            except sqlite3.IntegrityError:
                raise Denied("a customer with this channel and handle already exists; use find")
            self.db.commit()
            self._audit("customer.register", cid, "ok", lead)
            return {"id": cid, "company": lead, "scope": f"customer:{lead}:{cid}"}
        if action == "find":  # exact channel identity only: no browsing of the customer list
            if bound:
                return [self._customer_row(bound)]
            if not serving:
                self._deny("customer.find", handle or "", "only customer-facing roles look up customers")
            rows = self.db.execute("SELECT id FROM customers WHERE handle=? AND handle != '' AND (company=? OR ?)",
                                   ((handle or "").strip(), co, self.me["profile"] in SUPER)).fetchall()
            self._audit("customer.find", handle or "", "ok", f"n={len(rows)}")
            return [self._customer_row(r[0]) for r in rows]
        if action == "list":
            if bound:
                return [self._customer_row(bound)]
            if self.me["profile"] in READ_ALL:
                rows = self.db.execute("SELECT id FROM customers ORDER BY created_at DESC").fetchall()
            elif self.me["level"] >= 50 or self.me["role"] in {"customer-success", "delivery-director", "finance"}:
                rows = self.db.execute("SELECT id FROM customers WHERE company=? ORDER BY created_at DESC", (co,)).fetchall()
            else:
                self._deny("customer.list", co, "your role cannot list customers; use find with the channel identity")
            return [self._customer_row(r[0]) for r in rows]
        if not customer_id or not self._can_see_customer(customer_id):
            self._deny(f"customer.{action}", customer_id or "", "you cannot see this customer")
        if action == "get":
            self._audit("customer.get", customer_id, "ok")
            return self._customer_row(customer_id, full=True)
        if action == "update":
            if bound and self.me["role"] not in CUSTOMER_ROLES:
                self._deny("customer.update", customer_id, "read only")
            self._check_text(details, name)
            fields = {k: v for k, v in (("name", name), ("language", language), ("details", details),
                                        ("channel", channel), ("handle", handle)) if v}
            for k, v in fields.items():
                self.db.execute(f"UPDATE customers SET {k}=? WHERE id=?", (v.strip(), customer_id))
            self.db.commit()
            self._audit("customer.update", customer_id, "ok", ", ".join(fields))
            return self._customer_row(customer_id, full=True)
        if action == "link_project":
            if bound or (self.me["level"] < 50 and self.me["profile"] not in SUPER):
                self._deny("customer.link_project", customer_id, "only managers (level 50+) link projects")
            lead = self.db.execute("SELECT company FROM customers WHERE id=?", (customer_id,)).fetchone()[0]
            slug = re.sub(r"[^a-z0-9-]", "-", (project or "").lower()).strip("-")
            if not slug:
                raise Denied("project slug required")
            self.db.execute("INSERT OR REPLACE INTO project_customers VALUES (?,?,?)", (lead, slug, customer_id))
            self.db.commit()
            self._audit("customer.link_project", customer_id, "ok", f"project:{lead}:{slug}")
            return {"customer": customer_id, "project": f"project:{lead}:{slug}"}
        raise Denied("action must be register, find, get, list, update or link_project")

    # -- cross-company work orders (R03.05, R11.05)
    def _wo(self, wo_id):
        r = self.db.execute("SELECT * FROM work_orders WHERE id=?", (wo_id or "",)).fetchone()
        if r is None:
            raise Denied(f"work order {wo_id} not found")
        return dict(r)

    def _wo_event(self, wo_id, action, detail=""):
        self.db.execute("INSERT INTO wo_events (work_order, ts, actor, action, detail) VALUES (?,?,?,?,?)",
                        (wo_id, time.time(), self.actor, action, detail[:500]))
        self.db.execute("UPDATE work_orders SET updated_at=? WHERE id=?", (time.time(), wo_id))
        self.db.commit()
        self._audit(f"work_order.{action}", wo_id, "ok", detail)

    def _wo_view(self, wo):
        out = dict(wo)
        for k in ("created_at", "updated_at"):
            out[k] = time.strftime("%Y-%m-%d %H:%M", time.localtime(out[k]))
        out["team"] = [r[0] for r in self.db.execute("SELECT agent FROM wo_team WHERE work_order=?", (wo["id"],))]
        out["granted_records"] = [{"id": r[0], "title": r[1], "scope": r[2]} for r in self.db.execute(
            "SELECT r.id, r.title, r.scope FROM wo_grants g JOIN records r ON r.id = g.record WHERE g.work_order=?",
            (wo["id"],))]
        out["shared_scope"] = f"workorder:{wo['id']}"
        out["events"] = [{"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(e[0])), "by": e[1], "action": e[2],
                          "detail": e[3]} for e in self.db.execute(
            "SELECT ts, actor, action, detail FROM wo_events WHERE work_order=? ORDER BY id", (wo["id"],))]
        return out

    def _wo_grant(self, wo_id, record_ids):
        lead = self._wo(wo_id)["lead_company"]
        members = self._members()
        for rid in record_ids or []:
            rec = self._row(int(rid))
            kind = parse_scope(rec["scope"])[0]
            if kind not in ("entity", "department", "project", "customer") or scope_company(rec["scope"], self.org) != lead:
                self._deny("work_order.grant", wo_id, f"record {rid} is not a company, department, project or "
                                                      f"customer record of {lead}")
            if rec["kind"] in OWNER_ONLY_KINDS or not can_read(self.me, rec["scope"], self.org, members):
                self._deny("work_order.grant", wo_id, f"you cannot grant record {rid}")
            self.db.execute("INSERT OR IGNORE INTO wo_grants VALUES (?,?,?)", (wo_id, int(rid), self.me["profile"]))
        if record_ids:
            self._wo_event(wo_id, "grant", "records " + ", ".join(str(r) for r in record_ids))

    def _wo_team(self, wo, team):
        agents = [a.strip() for a in (team or []) if a and a.strip()]
        if not agents:
            raise Denied("name the supplier team (agent profiles) that will do the work")
        for a in agents:
            if self.org.get(a, {}).get("company") != wo["supplier_company"]:
                raise Denied(f"{a} is not an agent of {wo['supplier_company']}")
        self.db.execute("DELETE FROM wo_team WHERE work_order=?", (wo["id"],))
        self.db.executemany("INSERT INTO wo_team VALUES (?,?)", [(wo["id"], a) for a in agents])

    def work_order(self, action, wo_id=None, **f):
        """create, approve, accept, reject, start, link_task, deliver, close, rework, revise, cancel, get, list."""
        me, co, lvl = self.me, self.me["company"], self.me["level"]
        if me.get("bound"):
            self._deny(f"work_order.{action}", wo_id or "", "work orders are not handled in a customer conversation")
        sup = me["profile"] in SUPER
        if action == "list":
            if me["profile"] in READ_ALL:
                rows = self.db.execute("SELECT * FROM work_orders ORDER BY updated_at DESC").fetchall()
            else:
                self._members()
                rows = [r for r in self.db.execute("SELECT * FROM work_orders ORDER BY updated_at DESC")
                        if r["id"] in me["wos"]]
            return [{k: r[k] for k in ("id", "title", "lead_company", "supplier_company", "status",
                                       "requirements_revision")} for r in rows]
        if action == "create":
            supplier = (f.get("supplier") or "").strip().lower()
            if co == "federation" or lvl < 50:
                self._deny("work_order.create", supplier, "work orders are created by managers (level 50+) of a company")
            if supplier not in COMPANIES or supplier == co:
                raise Denied(f"supplier must be another company: {', '.join(c for c in COMPANIES if c != co)}")
            for k in ("title", "work_scope", "acceptance_criteria"):
                if not (f.get(k) or "").strip():
                    raise Denied(f"{k} is required")
            texts = {k: (f.get(k) or "").strip() for k in ("title", "work_scope", "acceptance_criteria",
                                                          "permitted_sharing", "outputs", "cost_allocation")}
            self._check_text(*texts.values())
            texts["permitted_sharing"] = texts["permitted_sharing"] or "Only the records granted on this work order."
            texts["outputs"] = texts["outputs"] or "OPEN"
            texts["cost_allocation"] = texts["cost_allocation"] or "OPEN"
            customer = f.get("customer") or None
            if customer and not self._can_see_customer(customer):
                self._deny("work_order.create", customer, "you cannot see this customer")
            project = re.sub(r"[^a-z0-9-]", "-", (f.get("project") or "").lower()).strip("-") or None
            wid = "wo_" + secrets.token_hex(4)
            status = "requested" if lvl >= 70 else "draft"  # the CEO approves subcontracting (role card)
            now = time.time()
            self.db.execute(
                "INSERT INTO work_orders (id, title, lead_company, supplier_company, customer, project, work_scope, "
                "acceptance_criteria, permitted_sharing, outputs, cost_allocation, status, created_by, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (wid, texts["title"], co, supplier, customer, project, texts["work_scope"],
                 texts["acceptance_criteria"], texts["permitted_sharing"], texts["outputs"],
                 texts["cost_allocation"], status, me["profile"], now, now))
            self._wo_event(wid, "create", f"{co} -> {supplier}, status {status}")
            self._wo_grant(wid, f.get("grant_records"))
            return self._wo_view(self._wo(wid))

        wo = self._wo(wo_id)
        side = "admin" if sup else self._wo_side(wo)
        if side is None and me["profile"] not in READ_ALL:
            self._deny(f"work_order.{action}", wo_id, "you are not part of this work order")
        if action == "get":
            return self._wo_view(wo)
        if me["profile"] == "fed-audit":
            self._deny(f"work_order.{action}", wo_id, "audit observes work orders, it does not change them")
        st = wo["status"]

        def need(states, who_ok, why):
            if st not in states:
                raise Denied(f"cannot {action} a work order in status {st}")
            if not (sup or who_ok):
                self._deny(f"work_order.{action}", wo_id, why)

        def move(new, detail=""):
            self.db.execute("UPDATE work_orders SET status=? WHERE id=?", (new, wo_id))
            self._wo_event(wo_id, action, f"{st} -> {new}" + (f": {detail}" if detail else ""))
            return self._wo_view(self._wo(wo_id))

        lead_mgr = side == "lead" and lvl >= 50
        sup_lead = side == "supplier" and lvl >= 60
        sup_team = side == "supplier"
        reason = (f.get("reason") or "").strip()
        self._check_text(reason)
        if action == "approve":
            need(("draft",), side == "lead" and lvl >= 70, "the lead company's CEO approves subcontracting")
            return move("requested")
        if action == "accept":
            need(("requested", "revision_pending"), sup_lead, "the supplier's leadership (level 60+) accepts work orders")
            self._wo_team(wo, f.get("team"))
            return move("accepted", "team " + ", ".join(f.get("team") or []))
        if action == "reject":
            need(("requested", "revision_pending"), sup_lead, "the supplier's leadership (level 60+) rejects work orders")
            if not reason:
                raise Denied("a reason is required")
            return move("rejected", reason)
        if action == "start":
            need(("accepted",), sup_team, "only the supplier team starts the work")
            return move("in_progress")
        if action == "link_task":
            need(WO_OPEN, sup_team, "only the supplier team links its Kanban task")
            task = (f.get("task") or "").strip()
            if not re.fullmatch(r"[a-z]+:t_[0-9a-f]{6,16}", task):
                raise Denied("task must look like <board>:t_xxxxxxxx")
            self.db.execute("UPDATE work_orders SET kanban_task=? WHERE id=?", (task, wo_id))
            self._wo_event(wo_id, "link_task", task)
            return self._wo_view(self._wo(wo_id))
        if action == "deliver":
            need(("in_progress",), sup_team, "only the supplier team delivers")
            summary = (f.get("delivery") or "").strip()
            if not summary:
                raise Denied("delivery summary required: what was done, where the outputs are, test results")
            self._check_text(summary)
            self.db.execute("UPDATE work_orders SET delivery=?, delivery_revision=requirements_revision WHERE id=?",
                            (summary, wo_id))
            return move("delivered")
        if action == "close":
            need(("delivered",), lead_mgr, "the lead company (level 50+) accepts the delivery and closes")
            return move("closed", reason)
        if action == "rework":
            need(("delivered",), lead_mgr, "the lead company (level 50+) asks for rework")
            if not reason:
                raise Denied("say what must be reworked")
            return move("in_progress", reason)
        if action == "revise":
            need(("requested", "accepted", "in_progress", "delivered"), lead_mgr,
                 "the lead company (level 50+) revises requirements")
            changes = {k: (f.get(k) or "").strip() for k in ("work_scope", "acceptance_criteria", "permitted_sharing",
                                                            "outputs", "cost_allocation") if (f.get(k) or "").strip()}
            if not changes and not f.get("grant_records"):
                raise Denied("give the changed requirements")
            self._check_text(*changes.values())
            for k, v in changes.items():
                self.db.execute(f"UPDATE work_orders SET {k}=? WHERE id=?", (v, wo_id))
            # R11.05: a changed material input invalidates the delivered evidence and pauses the work.
            self.db.execute("UPDATE work_orders SET requirements_revision=requirements_revision+1 WHERE id=?", (wo_id,))
            self._wo_grant(wo_id, f.get("grant_records"))
            return move("revision_pending", ", ".join(changes) or "grants")
        if action == "cancel":
            need(WO_OPEN, side == "lead" and (lvl >= 60 or wo["created_by"] == me["profile"]),
                 "the lead company's creator or leadership cancels")
            return move("cancelled", reason)
        raise Denied("unknown work order action")

    def customer_login_code(self, customer_id):
        """Owner only: issue a new portal login code (shown once; only its hash is kept)."""
        if self.me["profile"] != OWNER:
            self._deny("customer.login_code", customer_id, "only the owner issues customer login codes")
        if not self._customer_row(customer_id):
            raise Denied("unknown customer")
        code = secrets.token_urlsafe(9)
        self.db.execute("UPDATE customers SET login_hash=? WHERE id=?",
                        (hashlib.sha256(code.encode()).hexdigest(), customer_id))
        self.db.commit()
        self._audit("customer.login_code", customer_id, "ok")
        return {"customer": customer_id, "code": code}

    def customer_session_version(self, customer_id):
        """Owner context: the credential a portal session is bound to (the current login code), or None if the
        customer is inactive or has no code. A new code or a deactivation therefore ends every older session."""
        if self.me["profile"] != OWNER:
            return None
        r = self.db.execute("SELECT login_hash, status FROM customers WHERE id=?", (customer_id or "",)).fetchone()
        return r[0][:16] if r and r[0] and r[1] == "active" else None

    def customer_set_status(self, customer_id, status):
        """Owner only: deactivate (or reactivate) a customer. Deactivation ends their portal sessions."""
        if self.me["profile"] != OWNER:
            self._deny("customer.status", customer_id, "only the owner changes a customer's status")
        if status not in ("active", "inactive"):
            raise Denied("status must be active or inactive")
        if not self._customer_row(customer_id):
            raise Denied("unknown customer")
        self.db.execute("UPDATE customers SET status=? WHERE id=?", (status, customer_id))
        self.db.commit()
        self._audit("customer.status", customer_id, "ok", status)
        return self._customer_row(customer_id, full=True)

    def customer_by_code(self, code):
        """Owner context: resolve a portal login code to its customer, or None."""
        if self.me["profile"] != OWNER or not code:
            return None
        h = hashlib.sha256(code.strip().encode()).hexdigest()
        r = self.db.execute("SELECT id FROM customers WHERE login_hash=? AND status='active'", (h,)).fetchone()
        self._audit("customer.login", r[0] if r else "?", "ok" if r else "denied")
        return self._customer_row(r[0], full=True) if r else None

    def audit_log(self, limit=100):
        if self.me["profile"] not in READ_ALL:
            self._deny("audit", "", "only the owner, Hermes Admin and Audit read the memory audit log")
        rows = self.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
