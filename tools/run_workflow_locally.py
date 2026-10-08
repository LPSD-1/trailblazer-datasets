"""Run one of this repo's GitHub Actions workflows on a local machine.

For when Actions cannot run (minutes, an outage, a suspended account) and
the data still has to be refreshed. The workflow files stay the one recipe:
this reads them and runs their steps in order, exactly as written, so a
change to a workflow reaches the local run with no second copy to keep in
step.

    python tools/run_workflow_locally.py refresh-data          # lanes
    python tools/run_workflow_locally.py council-orders traffic-orders
    python tools/run_workflow_locally.py traffic-orders --input force=true
    python tools/run_workflow_locally.py refresh-data --list   # steps, no run

RUN IT IN A CLONE OF ITS OWN. The jobs end with `git reset --hard
origin/main` when a push races another job, and the checkout step here
does the same before every run (keeping only the cache folders the
workflow names), so any uncommitted work in the clone is lost. The runner
refuses to start in a clone with uncommitted changes to tracked files.

Secrets, never printed (each step's output is masked the way Actions masks
it):
  * an environment variable of the secret's own name, if set;
  * else KEY=VALUE lines in the file named by TB_SECRETS_ENV (D-TRO);
  * DATASET_KEY_B64 / TB_SIGNING_KEY_PEM from the files in TB_KEYS_DIR
    (dataset-encryption-key-256.b64, dataset-signing-ed25519-private.pem);
  * GITHUB_TOKEN / github.token from `gh auth token`.
A secret found nowhere is empty, as on Actions, and the workflow's own
guard says so.

What differs from a runner, on purpose:
  * actions/checkout fetches and resets to origin/main; setup-python is
    the local Python; actions/cache steps are skipped, because the cached
    folders simply stay on disk between local runs.
  * `/tmp` in a step is a per-run folder that both bash and Python see
    (Git Bash and Windows Python disagree about where /tmp is).
  * github.run_id is "local-<time>": an alarm issue a local run raises
    links to no Actions run.
"""
import argparse
import datetime
import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
STATE = os.path.join(ROOT, ".tb-local")      # gitignored: logs and lock
REPOSITORY = "LPSD-1/trailblazer-datasets"

KEY_FILES = {
    "DATASET_KEY_B64": "dataset-encryption-key-256.b64",
    "TB_SIGNING_KEY_PEM": "dataset-signing-ed25519-private.pem",
}


class ExprError(Exception):
    pass


# -- GitHub expressions ------------------------------------------------------

_TOKEN = re.compile(r"""\s*(?:
    (?P<str>'(?:[^']|'')*')
  | (?P<num>-?\d+(?:\.\d+)?)
  | (?P<op>&&|\|\||==|!=|<=|>=|<|>|!|\(|\)|,)
  | (?P<name>[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_*][A-Za-z0-9_-]*)*)
)""", re.X)


def _tokens(text):
    pos, out = 0, []
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _TOKEN.match(text, pos)
        if not m:
            raise ExprError("cannot read %r at %r" % (text, text[pos:]))
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def truthy(v):
    return not (v is None or v is False or v == "" or v == 0)


def as_text(v):
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _loose_equal(a, b):
    # Actions compares strings case-insensitively and coerces mixed types
    # to numbers; booleans arrive here as strings from `inputs`. A missing
    # value (null) and '' both coerce to 0, so an output a step never set
    # equals '' - refresh-data.yml's `steps.poi_age.outputs.regions != ''`
    # depends on exactly that.
    if a is None or b is None:
        return as_text(a) == as_text(b)
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    if isinstance(a, bool) or isinstance(b, bool):
        return as_text(a).lower() == as_text(b).lower()
    return a == b


class Expr(object):
    """Just enough of the Actions expression language for these files:
    literals, context paths, ! && || == != (and < > for completeness),
    and success() failure() always() cancelled() hashFiles()."""

    def __init__(self, text, ctx):
        self.toks = _tokens(text)
        self.i = 0
        self.ctx = ctx

    def value(self):
        v = self._or()
        if self.i != len(self.toks):
            raise ExprError("unexpected %r" % (self.toks[self.i][1],))
        return v

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def _eat(self, op):
        if self._peek() == ("op", op):
            self.i += 1
            return True
        return False

    def _or(self):
        v = self._and()
        while self._eat("||"):
            rhs = self._and()
            v = v if truthy(v) else rhs
        return v

    def _and(self):
        v = self._cmp()
        while self._eat("&&"):
            rhs = self._cmp()
            v = rhs if truthy(v) else v
        return v

    def _cmp(self):
        v = self._not()
        for op in ("==", "!=", "<=", ">=", "<", ">"):
            if self._eat(op):
                rhs = self._not()
                if op == "==":
                    return _loose_equal(v, rhs)
                if op == "!=":
                    return not _loose_equal(v, rhs)
                return {"<": v < rhs, ">": v > rhs,
                        "<=": v <= rhs, ">=": v >= rhs}[op]
        return v

    def _not(self):
        if self._eat("!"):
            return not truthy(self._not())
        return self._atom()

    def _atom(self):
        kind, text = self._peek()
        if kind is None:
            raise ExprError("expression ends early")
        self.i += 1
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return float(text)
        if kind == "op" and text == "(":
            v = self._or()
            if not self._eat(")"):
                raise ExprError("missing )")
            return v
        if kind == "name":
            if text in ("true", "false"):
                return text == "true"
            if text == "null":
                return None
            if self._eat("("):
                args = []
                if not self._eat(")"):
                    args.append(self._or())
                    while self._eat(","):
                        args.append(self._or())
                    if not self._eat(")"):
                        raise ExprError("missing ) after %s(" % text)
                return self.ctx.call(text, args)
            return self.ctx.lookup(text)
        raise ExprError("unexpected %r" % text)


class Context(object):
    def __init__(self, data, workspace):
        self.data = data
        self.workspace = workspace
        self.job_failed = False

    def lookup(self, path):
        cur = self.data
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
        return cur

    def call(self, name, args):
        if name == "success":
            return not self.job_failed
        if name == "failure":
            return self.job_failed
        if name == "always":
            return True
        if name == "cancelled":
            return False
        if name == "hashFiles":
            return hash_files(self.workspace, [as_text(a) for a in args])
        if name in ("contains", "startsWith", "endsWith"):
            a, b = as_text(args[0]).lower(), as_text(args[1]).lower()
            return {"contains": b in a, "startsWith": a.startswith(b),
                    "endsWith": a.endswith(b)}[name]
        raise ExprError("function %s() is not supported here" % name)

    def evaluate(self, text):
        return Expr(text, self).value()

    def substitute(self, text):
        if not isinstance(text, str):
            return text
        return re.sub(r"\$\{\{(.*?)\}\}",
                      lambda m: as_text(self.evaluate(m.group(1))), text,
                      flags=re.S)

    def condition(self, cond):
        """A step's `if:`. Absent means success(); a bare expression
        without a status function is implicitly ANDed with success()."""
        if cond is None or cond == "":
            return not self.job_failed
        if isinstance(cond, bool):
            return cond and not self.job_failed
        text = cond.strip()
        m = re.fullmatch(r"\$\{\{(.*)\}\}", text, flags=re.S)
        if m:
            text = m.group(1)
        v = truthy(self.evaluate(text))
        if not re.search(r"\b(success|failure|always|cancelled)\s*\(", text):
            v = v and not self.job_failed
        return v


def hash_files(workspace, patterns):
    files = []
    for pat in patterns:
        files += [p for p in glob.glob(os.path.join(workspace, pat),
                                       recursive=True) if os.path.isfile(p)]
    if not files:
        return ""
    h = hashlib.sha256()
    for p in sorted(set(files)):
        with open(p, "rb") as fh:
            h.update(hashlib.sha256(fh.read()).digest())
    return h.hexdigest()


def read_kv_file(path):
    """$GITHUB_OUTPUT / $GITHUB_ENV: name=value, or name<<DELIM ... DELIM."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^([^=<]+)<<(.+)$", line)
        if m:
            name, delim, buf = m.group(1), m.group(2), []
            i += 1
            while i < len(lines) and lines[i] != delim:
                buf.append(lines[i])
                i += 1
            out[name] = "\n".join(buf)
        elif "=" in line:
            name, value = line.split("=", 1)
            out[name] = value
        i += 1
    return out


# -- secrets -----------------------------------------------------------------

def load_secrets():
    secrets = {}
    env_file = os.environ.get("TB_SECRETS_ENV")
    if env_file and os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    secrets[k.strip()] = v.strip().strip('"').strip("'")
    keys_dir = os.environ.get("TB_KEYS_DIR")
    if keys_dir:
        for name, fname in KEY_FILES.items():
            p = os.path.join(keys_dir, fname)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as fh:
                    secrets[name] = fh.read().strip() + (
                        "\n" if name.endswith("_PEM") else "")
    try:
        token = subprocess.run(["gh", "auth", "token"], capture_output=True,
                               text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        token = ""
    if token:
        secrets["GITHUB_TOKEN"] = token
    for name in list(secrets) + list(KEY_FILES) + ["DTRO_CLIENT_ID",
                                                   "DTRO_CLIENT_SECRET"]:
        if os.environ.get(name):
            secrets[name] = os.environ[name]
    return secrets


class SecretsDict(dict):
    """A missing secret is the empty string, as on Actions."""

    def get(self, key, default=None):
        return dict.get(self, key, "")


def masker(secrets):
    values = set()
    for v in secrets.values():
        v = v.strip()
        if len(v) >= 6:
            values.add(v)
            for line in v.splitlines():
                if len(line.strip()) >= 6:
                    values.add(line.strip())
    ordered = sorted(values, key=len, reverse=True)

    def mask(text):
        for v in ordered:
            text = text.replace(v, "***")
        return text
    return mask


# -- the run -----------------------------------------------------------------

def find_bash():
    for p in (r"C:\Program Files\Git\bin\bash.exe",
              r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if os.path.exists(p):
            return p
    found = shutil.which("bash")
    # Windows' own System32\bash.exe is WSL, not Git Bash.
    if found and "system32" not in found.lower():
        return found
    sys.exit("Git Bash not found: install Git for Windows")


def load_workflow(name):
    path = name if os.path.exists(name) else os.path.join(
        WORKFLOWS, name if name.endswith(".yml") else name + ".yml")
    with open(path, encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)
    # PyYAML reads the bare key `on` as True.
    wf["on"] = wf.get("on", wf.pop(True, None)) or {}
    return path, wf


def dispatch_inputs(wf, given):
    trig = wf["on"].get("workflow_dispatch") if isinstance(wf["on"],
                                                           dict) else None
    spec = (trig or {}).get("inputs") or {}
    inputs = {}
    for name, d in spec.items():
        inputs[name] = d.get("default", False if d.get("type") == "boolean"
                             else "")
    for name, value in given.items():
        if name not in spec:
            sys.exit("%s has no input %r (it has: %s)"
                     % (wf.get("name"), name, ", ".join(spec) or "none"))
        if spec[name].get("type") == "boolean":
            value = value.lower() in ("1", "true", "yes")
        inputs[name] = value
    return inputs


def cache_paths(wf):
    paths = set()
    for job in wf["jobs"].values():
        for step in job.get("steps", []):
            if "actions/cache" in (step.get("uses") or ""):
                for p in str((step.get("with") or {}).get("path", "")).split():
                    paths.add(p.strip())
    return sorted(p for p in paths if p)


def ordered_jobs(wf):
    done, order, jobs = set(), [], wf["jobs"]
    while len(order) < len(jobs):
        progressed = False
        for name, job in jobs.items():
            if name in done:
                continue
            needs = job.get("needs") or []
            needs = [needs] if isinstance(needs, str) else needs
            if all(n in done for n in needs):
                order.append(name)
                done.add(name)
                progressed = True
        if not progressed:
            sys.exit("jobs depend on each other in a loop")
    return order


def checkout(keep, log):
    def git(*args):
        r = subprocess.run(["git"] + list(args), cwd=ROOT,
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("git %s: %s" % (" ".join(args),
                                               r.stderr.strip()))
        return r.stdout
    # Files as a Linux runner sees them. Git for Windows checks text out
    # with CRLF, and bash then fails on `$'\r': command not found` in
    # every tools/*.sh a step runs.
    git("config", "core.autocrlf", "input")
    git("fetch", "--quiet", "origin", "main")
    git("checkout", "--quiet", "-B", "main", "origin/main")
    git("reset", "--quiet", "--hard", "origin/main")
    if "w/crlf" in git("ls-files", "--eol", "--", "tools/*.sh"):
        git("rm", "-rq", "--cached", ".")
        git("reset", "--quiet", "--hard", "origin/main")
    excludes = []
    for p in keep + [".tb-local"]:
        excludes += ["-e", p.rstrip("/")]
    git("clean", "-ffdxq", *excludes)
    log("  checked out %s" % git("rev-parse", "--short", "HEAD").strip())


class Logger(object):
    def __init__(self, path, mask):
        self.fh = open(path, "a", encoding="utf-8")
        self.mask = mask

    def __call__(self, text):
        text = self.mask(text)
        print(text, flush=True)
        self.fh.write(text + "\n")
        self.fh.flush()


def run_step(bash, script, env, cwd, timeout, log):
    fd, path = tempfile.mkstemp(suffix=".sh", dir=env["RUNNER_TEMP"])
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(script)
    proc = subprocess.Popen(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", path],
        cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for raw in proc.stdout:
            log("    " + raw.decode("utf-8", "replace").rstrip("\r\n"))
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        log("    ::error::step timed out")
        return 124
    finally:
        os.unlink(path)


def run_job(wf_path, wf, job_name, inputs, secrets, args, bash, log,
            results):
    job = wf["jobs"][job_name]
    run_id = "local-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    tmp = tempfile.mkdtemp(prefix="tb-run-").replace("\\", "/")
    data = {
        "inputs": inputs,
        "secrets": SecretsDict(secrets),
        "github": {"run_id": run_id, "run_attempt": "1",
                   "server_url": "https://github.com",
                   "repository": REPOSITORY,
                   "token": secrets.get("GITHUB_TOKEN", ""),
                   "workspace": ROOT.replace("\\", "/"),
                   "event_name": "workflow_dispatch", "ref": "refs/heads/main",
                   "ref_name": "main", "actor": "local"},
        "runner": {"os": "Windows", "temp": tmp},
        "steps": {}, "env": {}, "needs": results,
        "job": {"status": "success"},
    }
    ctx = Context(data, ROOT)
    needs = job.get("needs") or []
    needs = [needs] if isinstance(needs, str) else needs
    if any(results.get(n, {}).get("result") != "success" for n in needs) \
            and not re.search(r"always\(\)", str(job.get("if", ""))):
        log("job %s: skipped (a job it needs did not succeed)" % job_name)
        return "skipped"
    if job.get("if") is not None and not ctx.condition(job.get("if")):
        log("job %s: skipped by its if:" % job_name)
        return "skipped"

    base_env = dict(os.environ)
    base_env.update({
        "CI": "true", "GITHUB_ACTIONS": "", "TB_LOCAL_RUN": "1",
        "GITHUB_WORKSPACE": ROOT, "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_RUN_ID": run_id, "GITHUB_SERVER_URL": "https://github.com",
        "RUNNER_TEMP": tmp, "TMPDIR": tmp,
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
    })
    for k in KEY_FILES:      # never leak a key into a step that did not ask
        base_env.pop(k, None)
    for k in ("DTRO_CLIENT_ID", "DTRO_CLIENT_SECRET", "GH_TOKEN",
              "GITHUB_TOKEN"):
        base_env.pop(k, None)
    for scope in (wf.get("env") or {}, job.get("env") or {}):
        for k, v in scope.items():
            data["env"][k] = as_text(ctx.substitute(v))
    persisted = {}
    timeout = int(job.get("timeout-minutes", 360)) * 60

    log("job %s (%d steps)" % (job_name, len(job["steps"])))
    for n, step in enumerate(job["steps"], 1):
        label = step.get("name") or step.get("uses") or "step %d" % n
        sid = step.get("id")
        uses = step.get("uses") or ""
        try:
            go = ctx.condition(step.get("if"))
        except ExprError as e:
            log("  [%2d] %s: cannot read its if: (%s)" % (n, label, e))
            ctx.job_failed = True
            continue
        if not go:
            log("  [%2d] %s: skipped" % (n, label))
            if sid:
                data["steps"][sid] = {"outputs": {}, "outcome": "skipped",
                                      "conclusion": "skipped"}
            continue
        if uses:
            if "actions/checkout" in uses:
                log("  [%2d] checkout: reset to origin/main, keeping %s"
                    % (n, ", ".join(cache_paths(wf)) or "no cache"))
                if not args.list:
                    checkout(cache_paths(wf), log)
            else:
                log("  [%2d] %s: %s" % (n, label,
                                        "local Python" if "setup-python" in uses
                                        else "kept on disk between runs"
                                        if "actions/cache" in uses
                                        else "not run locally"))
            if sid:
                data["steps"][sid] = {"outputs": {}, "outcome": "success",
                                      "conclusion": "success"}
            continue
        out_file = os.path.join(tmp, "output-%d" % n)
        env_file = os.path.join(tmp, "env-%d" % n)
        for p in (out_file, env_file):
            open(p, "w").close()
        env = dict(base_env)
        env.update(data["env"])
        env.update(persisted)
        for k, v in (step.get("env") or {}).items():
            env[k] = as_text(ctx.substitute(v))
        # An empty GH_TOKEN makes gh refuse its own login; unset uses it.
        for k in ("GH_TOKEN", "GITHUB_TOKEN"):
            if k in env and not env[k]:
                del env[k]
        env.update({"GITHUB_OUTPUT": out_file, "GITHUB_ENV": env_file,
                    "GITHUB_STEP_SUMMARY": os.path.join(tmp, "summary.md"),
                    "GITHUB_PATH": os.path.join(tmp, "path")})
        script = ctx.substitute(step.get("run", ""))
        # One /tmp for bash and Python alike.
        script = re.sub(r"(?<![\w./-])/tmp(?=/|\b)", tmp, script)
        cwd = os.path.join(ROOT, step["working-directory"]) \
            if step.get("working-directory") else ROOT
        log("  [%2d] %s" % (n, label))
        if args.list:
            continue
        rc = run_step(bash, script, env, cwd, timeout, log)
        outcome = "success" if rc == 0 else "failure"
        conclusion = "success" if (rc == 0 or str(
            step.get("continue-on-error")).lower() == "true") else "failure"
        if sid:
            data["steps"][sid] = {"outputs": read_kv_file(out_file),
                                  "outcome": outcome,
                                  "conclusion": conclusion}
        persisted.update(read_kv_file(env_file))
        if rc != 0:
            log("       exit %d%s" % (rc, " (continue-on-error)"
                                      if conclusion == "success" else ""))
        if conclusion == "failure":
            ctx.job_failed = True
            data["job"]["status"] = "failure"
    shutil.rmtree(tmp, ignore_errors=True)
    return "failure" if ctx.job_failed else "success"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workflows", nargs="+",
                    help="workflow file names, with or without .yml")
    ap.add_argument("--job", help="run only this job")
    ap.add_argument("--input", action="append", default=[],
                    metavar="NAME=VALUE", help="a workflow_dispatch input")
    ap.add_argument("--list", action="store_true",
                    help="show the steps and which would run; run nothing")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="start even with uncommitted changes (they will "
                         "be LOST at checkout)")
    args = ap.parse_args(argv)

    os.makedirs(os.path.join(STATE, "logs"), exist_ok=True)
    dirty = subprocess.run(["git", "status", "--porcelain",
                            "--untracked-files=no"], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
    if dirty and not args.list and not args.allow_dirty:
        sys.exit("This clone has uncommitted changes, and the checkout step "
                 "resets it to origin/main. Run in a clone kept for local "
                 "runs (see the file header).\n" + dirty)
    lock = os.path.join(STATE, "lock")
    if not args.list:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            sys.exit("Another local run holds %s. If none is running, "
                     "delete that file." % lock)
    secrets = load_secrets()
    mask = masker(secrets)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    log = Logger(os.path.join(STATE, "logs", "run-%s.log" % stamp), mask)
    bash = find_bash()
    given = dict(kv.split("=", 1) for kv in args.input)
    failed = []
    try:
        for name in args.workflows:
            path, wf = load_workflow(name)
            log("== %s (%s)" % (wf.get("name"), os.path.basename(path)))
            missing = [k for k in ("DATASET_KEY_B64", "TB_SIGNING_KEY_PEM",
                                   "DTRO_CLIENT_ID", "DTRO_CLIENT_SECRET",
                                   "GITHUB_TOKEN")
                       if ("secrets." + k) in open(path, encoding="utf-8")
                       .read() and not secrets.get(k)]
            if missing:
                log("  secrets this workflow uses that were not found: %s"
                    % ", ".join(missing))
            inputs = dispatch_inputs(wf, {k: v for k, v in given.items()
                                          if k in ((wf["on"].get(
                                              "workflow_dispatch") or {})
                                              .get("inputs") or {})})
            results = {}
            for job_name in ordered_jobs(wf):
                if args.job and job_name != args.job:
                    continue
                r = run_job(path, wf, job_name, inputs, secrets, args, bash,
                            log, results)
                results[job_name] = {"result": r}
                log("job %s: %s" % (job_name, r))
                if r == "failure":
                    failed.append("%s/%s" % (os.path.basename(path),
                                             job_name))
    finally:
        if not args.list and os.path.exists(lock):
            os.unlink(lock)
    if failed:
        log("FAILED: %s" % ", ".join(failed))
        return 1
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
