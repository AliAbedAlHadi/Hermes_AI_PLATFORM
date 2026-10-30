"""Run Hermes as the separate agent account (G2-2) from the Console, which runs as the owner.

The Console never hands user text to a command line. For each run it:
  1. creates <root>/runs/<uuid>/ and writes request.json (argv, HERMES_HOME, timeout);
  2. starts, AS the agent account via the Windows Secondary Logon service, only the fixed command
       <runtime python> -I <runtime>/agent_exec.py <run dir>
     using the agent's credential stored DPAPI-encrypted for the owner (2_store_credential.ps1): only the owner's
     Windows account on this machine can decrypt it, and the agent account cannot read the owner's profile;
  3. reads result.json and removes the run folder.
There is no listener: agents have nothing to call. server.py uses it only for the G2-2 pilot (Pilot below, profile
sbx-executor in <root>/home-pilot); the live federation still runs as the owner (see deploy/windows-agent-user/README.md).
"""
import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(r"C:\HermesFederation")
CRED = Path.home() / "AppData" / "Local" / "hermes-federation-launcher" / "agent-credential.xml"

# Fixed script: every value arrives through an environment variable, nothing is interpolated into the script text.
PS_LAUNCH = (
    "$ErrorActionPreference='Stop';"
    "try {"
    "$c = Import-Clixml -LiteralPath $env:FED_AGENT_CRED;"
    "$p = Start-Process -FilePath $env:FED_AGENT_PY -ArgumentList @('-I', $env:FED_AGENT_SCRIPT, $env:FED_AGENT_RUN) "
    "-Credential $c -WorkingDirectory $env:FED_AGENT_RUN -LoadUserProfile -WindowStyle Hidden -Wait -PassThru;"
    "exit $p.ExitCode"
    "} catch { [Console]::Error.WriteLine('LAUNCH ERROR: ' + $_.Exception.Message); exit 3 }")


class AgentRunnerError(Exception):
    pass


def powershell_launcher(cred):
    def launch(py, script, run, timeout):
        env = {"FED_AGENT_CRED": str(cred), "FED_AGENT_PY": str(py), "FED_AGENT_SCRIPT": str(script),
               "FED_AGENT_RUN": str(run), "SystemRoot": r"C:\Windows", "PATH": r"C:\Windows\System32;C:\Windows\System32\WindowsPowerShell\v1.0"}
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                            PS_LAUNCH], capture_output=True, text=True, timeout=timeout + 30, env=env, cwd=str(run))
        if r.returncode != 0 and not (Path(run) / "result.json").exists():
            msg = next((l for l in r.stderr.splitlines() if l.startswith("LAUNCH ERROR")), r.stderr.strip()[-600:])
            raise AgentRunnerError(f"launch as agent failed (rc {r.returncode}): {msg}")
    return launch


class AgentRunner:
    def __init__(self, root=ROOT, hermes_home=None, cred=CRED, launcher=None, python=None, runs=None):
        self.root = Path(root)
        # one run folder per identity (runs/<identity>): an identity cannot read another one's requests or results
        self.runs = Path(runs) if runs else self.root / "runs"
        self.runtime = self.root / "runtime"
        self.hermes_home = Path(hermes_home or self.root / "home-pilot")
        self.hermes_exe = self.runtime / "bin" / "hermes.exe"
        self.python = Path(python) if python else next(
            (p for p in sorted((self.runtime / "tools").rglob("python.exe")) if "venv" not in p.parts), None)
        self.launch = launcher or powershell_launcher(cred)
        for p in (self.root, self.python or Path("x")):
            if " " in str(p):
                raise AgentRunnerError(f"paths with spaces are not supported: {p}")

    def _run(self, script, payload_name, payload, timeout):
        if self.python is None:
            raise AgentRunnerError("no runtime python found under " + str(self.runtime / "tools"))
        run = self.runs / uuid.uuid4().hex
        run.mkdir(parents=True)
        try:
            (run / payload_name).write_text(json.dumps(payload), encoding="utf-8")
            started = time.time()
            self.launch(self.python, self.runtime / script, run, timeout)
            res = run / "result.json"
            if not res.exists():
                raise AgentRunnerError(f"no result from the agent account after {time.time() - started:.0f}s")
            return json.loads(res.read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(run, ignore_errors=True)

    def hermes(self, *argv, timeout=120, cwd=None, env=None):
        """Same contract as server.hermes(): (returncode, stdout, stderr)."""
        r = self._run("agent_exec.py", "request.json",
                      {"argv": list(argv), "timeout": timeout, "cwd": str(cwd) if cwd else None, "env": env or {},
                       "hermes_home": str(self.hermes_home), "hermes_exe": str(self.hermes_exe)}, timeout)
        return r["rc"], r["stdout"], r["stderr"]

    def probe(self, spec, timeout=120):
        return self._run("agent_probe.py", "probe.json", spec, timeout)["rows"]


class PilotUnavailable(AgentRunnerError):
    pass


class PilotBusy(AgentRunnerError):
    pass


class Pilot:
    """The G2-2 pilot as the Console sees it: one synthetic agent (sbx-executor) run as the agent account, one turn at
    a time. Nothing here touches the live federation's homes, profiles or databases."""
    PROFILE = "sbx-executor"
    IDENTITY = "hermes-agent"
    MAX_MESSAGE = 20_000

    def __init__(self, root=ROOT, cred=CRED, runner=None):
        self.root, self.cred, self._runner = Path(root), Path(cred), runner
        self.lock = threading.Lock()
        self.last = None

    def missing(self):
        """Why the pilot cannot run here, or None (checked on every call: setup may finish while the Console runs)."""
        need = [(self.root / "runtime" / "bin" / "hermes.exe", "the pilot runtime (1_setup_pilot.ps1)"),
                (self.root / "home-pilot" / "profiles" / self.PROFILE / "config.yaml", "the pilot profile"),
                (self.cred, "the agent credential (2_store_credential.ps1)")]
        if self._runner is not None:
            return None
        return next((why for path, why in need if not path.is_file()), None)

    def status(self):
        why = self.missing()
        return {"available": why is None, "reason": f"missing {why}" if why else "", "profile": self.PROFILE,
                "identity": self.IDENTITY, "busy": self.lock.locked(), "last": self.last}

    def chat(self, message, timeout=900):
        message = (message or "").strip()
        if not message or len(message) > self.MAX_MESSAGE:
            raise ValueError(f"message required (at most {self.MAX_MESSAGE} characters)")
        why = self.missing()
        if why:
            raise PilotUnavailable(f"the pilot is not set up: missing {why}")
        if not self.lock.acquire(blocking=False):
            raise PilotBusy("the pilot agent is already answering; try again when it finishes")
        started = time.time()
        try:
            if self._runner is None:
                self._runner = AgentRunner(self.root, cred=self.cred)
            rc, out, err = self._runner.hermes("-p", self.PROFILE, "-z", message, timeout=timeout)
            ok = rc == 0 and bool(out.strip())
            reply = out.strip() if ok else (err.strip()[-800:] or f"the pilot agent did not answer (exit {rc})")
            self.last = {"at": started, "ok": ok, "seconds": round(time.time() - started, 1)}
            return {"reply": reply, "ok": ok, "seconds": self.last["seconds"], "identity": self.IDENTITY,
                    "profile": self.PROFILE}
        except AgentRunnerError:
            self.last = {"at": started, "ok": False, "seconds": round(time.time() - started, 1)}
            raise
        finally:
            self.lock.release()
