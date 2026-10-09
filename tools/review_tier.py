#!/usr/bin/env python3
"""Which reviewer a pull request gets, from the paths it changes.

    python tools/review_tier.py FILES.txt [--rev SHA --present PRESENT.txt]
        -> prints gate, security, code or data

Used by .github/workflows/independent-review.yml. FILES.txt is one path per
line, as GitHub's own API lists the pull request's files (both sides of a
rename), never the list the pull request uploaded about itself. With --rev,
every changed .py file still present (PRESENT.txt) is read from that commit
as a git object (never checked out, never run) and its imports inspected;
one that cannot be read counts as SECURITY.

  GATE      the review gate itself. Never reviewed by a model: the owner must
            review it, because a change here could rewrite its own review.
  SECURITY  anything that can change what runs, what is fetched, or what
            guards the repository: .github/**, .claude/**, the polite HTTP
            client and its robots allowlist, tools/*fetch*, any path with
            "guard" or "hook" in it, .gitattributes, .gitmodules, the council
            fetch registries and every tools module they import or name, and
            any .py whose new content (comments stripped) imports a network
            or process module in any form, relative imports included, or
            uses __import__, importlib, exec(, eval(, os.system, os.popen.
  CODE      everything not positively known to be data. A new extension is
            code until somebody says otherwise.
  DATA      .json / .geojson / .csv / .md / .txt outside tools/ (except
            tools/fixtures/), and anything that is not code inside one of
            the published data directories.

EVERY PATH IS LOWER-CASED BEFORE EVERY RULE. Windows and macOS checkouts are
case-insensitive, so `Tools/Robots_Override.json` is the robots allowlist on
the machine that runs it; it was rated DATA until this said so.

The strongest tier any one path earns is the tier of the whole change.
"""
import ast
import io
import os
import re
import subprocess
import sys
import tokenize

HERE = os.path.dirname(os.path.abspath(__file__))

#: The review gate. A pull request touching any of these is the owner's.
GATE_EXACT = frozenset((
    ".github/workflows/pr-capture.yml",
    ".github/workflows/independent-review.yml",
    "tools/test_review_gate.py",
    "docs/review-gate-setup.md",
))
GATE_PREFIXES = (".github/review/", "tools/review_")
GATE_NAMES = frozenset(("conftest.py", "codeowners"))

SECURITY_EXACT = frozenset((
    "tools/polite_http.py",
    "tools/robots_override.json",
))
SECURITY_DIRS = frozenset((".github", ".claude"))
SECURITY_WORDS = ("guard", "hook")
SECURITY_NAMES = frozenset((".gitattributes", ".gitmodules"))

#: The council fetch registries. They, and every tools module they import
#: or name, decide what is fetched from whom.
REGISTRIES = ("council_sources.py", "council_ways.py", "home_collector.py")

#: A .py file importing one of these can reach the network or run a program
#: that does (subprocess: `curl`).
NETWORK_MODULES = frozenset((
    "socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx",
    "aiohttp", "subprocess", "polite_http"))
#: Ways to reach a module, or run code, that no import statement shows:
#: `__import__('soc' + 'ket')`, importlib, exec, eval, os.system, os.popen.
DYNAMIC = re.compile(r"\b__import__\b|\bimportlib\b|\bexec\s*\(|\beval\s*\("
                     r"|\bos\s*\.\s*(?:system|popen)\b"
                     r"|\bfrom\s+os\s+import\b[^\n]*\b(?:system|popen)\b")
_IMPORT = re.compile(r"^\s*import\s+([\w\s.,]+)", re.M)
_FROM = re.compile(r"^\s*from\s+(\.*)\s*([\w.]*)\s+import\s+\(?([\w\s.,*]+)",
                   re.M)

CODE_SUFFIXES = (".py", ".yml", ".yaml", ".sh", ".dart", ".cmd", ".bat",
                 ".ps1", ".js", ".mjs", ".cjs", ".ts", ".toml", ".cfg",
                 ".ini", ".html", ".htm", ".svg")
CODE_NAMES = frozenset(("makefile", "pyproject.toml", "setup.py",
                        "setup.cfg", "package.json", "package-lock.json",
                        "dockerfile"))
_REQUIREMENTS = re.compile(r"^requirements.*\.(txt|in)$")

DATA_SUFFIXES = (".json", ".geojson", ".csv", ".md", ".txt")
DATA_DIRS = frozenset((
    "changes", "containers", "council-ucrs", "council-ways", "height",
    "home-collected", "local-rules", "manual", "names", "packages",
    "published", "routing", "satellite", "status", "trips", "tro",
))


def _norm(path):
    path = path.strip().replace("\\", "/").lower()
    while path.startswith("./"):
        path = path[2:]
    return path


def _parts(path):
    return [p for p in path.split("/") if p]


def is_gate(path):
    path = _norm(path)
    parts = _parts(path)
    if not parts:
        return False
    return (path in GATE_EXACT or path.startswith(GATE_PREFIXES)
            or parts[-1] in GATE_NAMES)


def registry_modules(tools_dir=HERE):
    """{"tools/x.py"} for the registries and every sibling module they
    import or name as `x.py`, read from main's copy (trusted)."""
    out = set()
    for reg in REGISTRIES:
        out.add("tools/" + reg)
        path = os.path.join(tools_dir, reg)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        names = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.py\b", text))
        try:
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Import):
                    names.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module \
                        and not node.level:
                    names.add(node.module.split(".")[0])
        except SyntaxError:
            pass
        for name in names:
            if os.path.exists(os.path.join(tools_dir, name + ".py")):
                out.add("tools/%s.py" % name.lower())
    return out


def strip_comments(source):
    """Python source with every comment removed, by the tokenizer (so a #
    inside a string stays). Raises on source the tokenizer refuses."""
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT:
            out.append(tok)
    return tokenize.untokenize(out)


def _names(text):
    return [n.strip().split(" as ")[0].strip()
            for n in text.replace("(", " ").replace(")", " ").split(",")
            if n.strip()]


def _network(name):
    return bool(name) and name.split(".")[0] in NETWORK_MODULES


def imports_network(source):
    """True when Python source imports a network or process module in any
    import form (relative ones too: `from . import polite_http`), reaches
    one dynamically (__import__, importlib, exec, eval, os.system,
    os.popen), or cannot be read well enough to say it does not."""
    try:
        code = strip_comments(source)
        tree = ast.parse(source)
    except (SyntaxError, ValueError, tokenize.TokenError,
            IndentationError):
        return True
    if DYNAMIC.search(code):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_network(a.name) for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if _network(node.module):
                return True
            if node.level and any(_network(a.name) for a in node.names):
                return True
    # The same rules over the text, in case the tree and the text disagree.
    for m in _IMPORT.finditer(code):
        if any(_network(n.split()[0]) for n in _names(m.group(1))
               if n.split()):
            return True
    for m in _FROM.finditer(code):
        dots, module, names = m.groups()
        if _network(module):
            return True
        if dots and any(_network(n) for n in _names(names)):
            return True
    return False


def is_security(path, source=None, registry=frozenset()):
    path = _norm(path)
    parts = _parts(path)
    if not parts:
        return False
    if path in SECURITY_EXACT or path in registry:
        return True
    if any(p in SECURITY_DIRS for p in parts):
        return True
    if parts[0] == "tools" and "fetch" in parts[-1]:
        return True
    if any(word in p for p in parts for word in SECURITY_WORDS):
        return True
    if parts[-1] in SECURITY_NAMES:
        return True
    if path.endswith(".py") and source is not None \
            and imports_network(source):
        return True
    return False


def is_data(path):
    path = _norm(path)
    parts = _parts(path)
    if not parts:
        return False
    name = parts[-1]
    if name in CODE_NAMES or _REQUIREMENTS.match(name) \
            or name.endswith(CODE_SUFFIXES):
        return False
    if parts[0] == "tools" and not path.startswith("tools/fixtures/"):
        return False
    if name.endswith(DATA_SUFFIXES):
        return True
    return parts[0] in DATA_DIRS and len(parts) > 1


def tier_of(paths, sources=None, registry=None):
    """-> "gate", "security", "code" or "data" for the whole list.

    sources: {path: new text} for the changed .py files still present.
    registry: the paths registry_modules() returns (computed if None).
    """
    sources = {_norm(k): v for k, v in (sources or {}).items()}
    registry = registry if registry is not None else registry_modules()
    paths = [p for p in (_norm(p) for p in paths) if p]
    if any(is_gate(p) for p in paths):
        return "gate"
    if any(is_security(p, sources.get(p), registry) for p in paths):
        return "security"
    if all(is_data(p) for p in paths):
        return "data"
    return "code"


#: What a present .py file that could not be read is taken to contain: a
#: NUL, which no parser accepts, so imports_network() calls it SECURITY.
UNREADABLE = "\x00"


def sources_at(rev, present):
    """{path: text} for every present .py path at rev, read as an object.
    One that cannot be read is UNREADABLE, never left out."""
    out = {}
    for path in present:
        if not path.lower().endswith(".py"):
            continue
        r = subprocess.run(["git", "cat-file", "blob", "%s:%s" % (rev, path)],
                           capture_output=True)
        out[path] = (r.stdout.decode("utf-8", errors="replace")
                     if r.returncode == 0 else UNREADABLE)
    return out


def main(argv):
    args = argv[1:]
    opts = {}
    for flag in ("--rev", "--present"):
        if flag in args:
            i = args.index(flag)
            opts[flag] = args[i + 1]
            del args[i:i + 2]
    if len(args) != 1 or ("--rev" in opts) != ("--present" in opts):
        sys.stderr.write("usage: review_tier.py FILES.txt "
                         "[--rev SHA --present PRESENT.txt]\n")
        return 2
    with open(args[0], encoding="utf-8") as fh:
        paths = [p for p in fh.read().splitlines() if p.strip()]
    sources = {}
    if "--rev" in opts:
        with open(opts["--present"], encoding="utf-8") as fh:
            present = [p for p in fh.read().splitlines() if p.strip()]
        sources = sources_at(opts["--rev"], present)
    print(tier_of(paths, sources))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
