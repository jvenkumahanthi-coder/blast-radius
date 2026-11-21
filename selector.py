"""Blast Radius selector.

For each patch against the pinned sqlglot commit, pick the tests that can catch
it, using maps of the pinned commit only:

- covmap.json.gz: per-test line coverage of sqlglot/ and tests/, and which data
  files (fixtures) each test opened.
- names.json (built in the image from the pinned source): where each identifier
  is used, so a changed import-time constant reaches the code that reads it.

When the analysis is not confident, it falls back to running everything on 8
workers (about 30 simulated seconds), which is far cheaper than a 300 s miss.
"""

import ast
import gzip
import json
import os
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
APP = Path(os.environ.get("BR_APP", "/app"))
SRC = Path(os.environ.get("BR_SRC", APP / "src"))
WORKER_MS = 2000
MAX_WORKERS = 8
ALL = "ALL"
# Diagnostic switch: run everything wherever the analysis would run nothing.
DIAG_EMPTY_ALL = True

DIALECT_DIRS = ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/", "sqlglot/tokenizers/")


def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- diff parsing

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """-> list of dicts: old, new, hunks[(old_start, old_len, lines)]"""
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "binary": False}
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
        elif cur is not None and line.startswith("--- "):
            p = line[4:].split("\t")[0]
            cur["old"] = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif cur is not None and line.startswith("+++ "):
            p = line[4:].split("\t")[0]
            cur["new"] = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif cur is not None and line.startswith("Binary files"):
            cur["binary"] = True
        elif cur is not None and line.startswith("new file mode"):
            cur["created"] = True
        elif cur is not None and line.startswith("deleted file mode"):
            cur["deleted"] = True
        elif cur is not None and line.startswith(("rename from ", "rename to ")):
            cur["renamed"] = True
        elif cur is not None and line.startswith("@@"):
            m = HUNK.match(line)
            os_, ol = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            nl = int(m.group(4) if m.group(4) is not None else 1)
            body = []
            i += 1
            o_seen = n_seen = 0
            while i < len(lines) and (o_seen < ol or n_seen < nl):
                l = lines[i]
                if l.startswith("\\"):
                    i += 1
                    continue
                tag = l[:1] if l else " "
                body.append((tag, l[1:]))
                if tag in (" ", "-"):
                    o_seen += 1
                if tag in (" ", "+"):
                    n_seen += 1
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
            cur["hunks"].append((os_, ol, body))
            continue
        i += 1
    return files


def apply_hunks(old_lines, hunks):
    """-> (new_lines, removed_old_line_numbers, insert_anchors, added_new_line_numbers, old_to_new)"""
    new = []
    removed, anchors, added = set(), set(), set()
    pos = 1  # next old line (1-based) to copy
    for os_, ol, body in hunks:
        start = os_ if ol > 0 else os_ + 1
        while pos < start:
            new.append(old_lines[pos - 1])
            pos += 1
        for tag, text in body:
            if tag == " ":
                new.append(old_lines[pos - 1] if pos - 1 < len(old_lines) else text)
                pos += 1
            elif tag == "-":
                removed.add(pos)
                pos += 1
            elif tag == "+":
                new.append(text)
                added.add(len(new))
                anchors.add(pos - 1)
                anchors.add(pos)
    while pos <= len(old_lines):
        new.append(old_lines[pos - 1])
        pos += 1
    return new, removed, anchors, added


def inert(text):
    s = text.strip()
    return s == "" or s.startswith("#")


# ---------------------------------------------------------------- AST helpers


def child_blocks(s):
    for field in ("body", "orelse", "finalbody"):
        v = getattr(s, field, None)
        if isinstance(v, list) and v and isinstance(v[0], ast.stmt):
            yield v
    for h in getattr(s, "handlers", []) or []:
        yield h.body
    for c in getattr(s, "cases", []) or []:
        yield c.body


def header_end(s):
    blocks = list(child_blocks(s))
    if blocks:
        return max(s.lineno, min(b[0].lineno for b in blocks) - 1)
    return s.end_lineno


def locate(tree, line):
    """Innermost statement whose header covers `line`.

    -> (kind, span, stmt, chain) with kind "runtime" (inside a function/lambda
    body) or "import" (module/class level, decorators, defaults, bases), or None.
    """
    best = None

    def visit(stmts, runtime, chain):
        nonlocal best
        for s in stmts:
            if s.lineno <= line <= s.end_lineno:
                start = min([s.lineno] + [d.lineno for d in getattr(s, "decorator_list", [])])
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if s.body and line >= s.body[0].lineno:
                        visit(s.body, True, chain + [s])
                        if best is None:
                            best = ("runtime", (s.body[0].lineno, s.end_lineno), s, chain + [s])
                    else:
                        best = ("import" if not runtime else "runtime", (start, header_end(s)), s, chain)
                    return
                if isinstance(s, ast.ClassDef):
                    if s.body and line >= s.body[0].lineno:
                        visit(s.body, runtime, chain + [s])
                        if best is None:
                            best = ("import" if not runtime else "runtime", (s.lineno, s.end_lineno), s, chain + [s])
                    else:
                        best = ("import" if not runtime else "runtime", (start, header_end(s)), s, chain)
                    return
                he = header_end(s)
                if line <= he:
                    kind = "runtime" if runtime else "import"
                    if not runtime:
                        for lam in ast.walk(s):
                            if isinstance(lam, ast.Lambda) and lam.body.lineno <= line <= lam.body.end_lineno:
                                best = ("runtime", (lam.lineno, lam.end_lineno), s, chain)
                                return
                    best = (kind, (s.lineno, he), s, chain)
                    return
                for b in child_blocks(s):
                    visit(b, runtime, chain + [s])
                    if best is not None:
                        return
                # between blocks (e.g. `else:` line)
                best = ("runtime" if runtime else "import", (s.lineno, s.end_lineno), s, chain)
                return

    visit(tree.body, False, [])
    return best


class _Canon(ast.NodeTransformer):
    """`x is None` and `x == None` behave the same unless x overrides __eq__ to equal
    None, which nothing in sqlglot does; same for `is not` / `!=`."""

    def visit_Compare(self, node):
        self.generic_visit(node)
        ops = []
        for op, right in zip(node.ops, node.comparators):
            if isinstance(right, ast.Constant) and right.value is None:
                op = ast.Eq() if isinstance(op, (ast.Is, ast.Eq)) else ast.NotEq() if isinstance(op, (ast.IsNot, ast.NotEq)) else op
            ops.append(op)
        node.ops = ops
        return node


def canonical(tree):
    import copy

    return ast.dump(_Canon().visit(copy.deepcopy(tree)))


def target_names(node):
    out = set()
    targets = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(node.name)
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif isinstance(n, ast.Attribute):
                out.add(n.attr)
    return out


# ---------------------------------------------------------------- selector


class Selector:
    def __init__(self):
        tj = json.loads((DATA / "tests.json").read_text())
        self.tests = tj["tests"]
        self.num = {t["id"]: t["number"] for t in self.tests}
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in self.tests}
        self.by_file = {}
        for t in self.tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
        with gzip.open(APP / "covmap.json.gz", "rt") as f:
            cm = json.load(f)
        # map coverage test index -> tests.json number (unknown ids dropped)
        idx_to_num = [self.num.get(t) for t in cm["tests"]]
        self.sets = [frozenset(n for n in (idx_to_num[i] for i in s) if n is not None) for s in cm["sets"]]
        self.lines = cm["lines"]
        self.opens = {p: {idx_to_num[i] for i in v if idx_to_num[i] is not None} for p, v in cm["opens"].items()}
        self.opens_at_collect = set(cm["opens_at_collect"])
        names = json.loads((APP / "names.json").read_text())
        self.refs = names["refs"]
        self.import_refs = names["import_refs"]
        self.parents = names["dialect_parents"]
        self.class_bases = names.get("class_bases", {})
        self.funcs = names.get("funcs", {})
        self.test_text = {self.num[k]: v for k, v in names["test_text"].items() if k in self.num}
        self._fixture_text = {}

    # coverage of a line span in a file
    def cov(self, rel, a, b):
        fl = self.lines.get(rel, {})
        out = set()
        for ln in range(a, b + 1):
            s = fl.get(str(ln))
            if s is not None:
                out |= self.sets[s]
        return out

    def file_touched(self, rel):
        out = set()
        for s in self.lines.get(rel, {}).values():
            out |= self.sets[s]
        return out

    # ----- import-time names
    def names_reach(self, names, depth=4):
        """Tests covering runtime uses of `names`, following import-time assignments."""
        seen = set()
        frontier = set(names)
        out = set()
        for _ in range(depth):
            nxt = set()
            for n in frontier:
                if n in seen:
                    continue
                seen.add(n)
                for rel, a, b in self.refs.get(n, []):
                    out |= self.cov(rel, a, b)
                for rel, tn in self.import_refs.get(n, []):
                    nxt |= set(tn)
            frontier = nxt - seen
        return out

    def dialect_of(self, rel):
        if rel.startswith(DIALECT_DIRS):
            return Path(rel).stem
        return None

    def dialect_scope(self, d):
        """Tests that plausibly run dialect `d` (or a dialect built on it)."""
        # dialect class names in this file and every subclass
        family = {d}
        cls_of = {k.lower(): k for k in self.parents}
        changed = True
        while changed:
            changed = False
            for cls, bases in self.parents.items():
                if cls.lower() not in family and any(b.lower() in family for b in bases):
                    family.add(cls.lower())
                    changed = True
        out = set()
        for fam in family:
            out |= self.by_file.get(f"tests/dialects/test_{fam}.py", set())
            for sub in DIALECT_DIRS:
                out |= self.file_touched(f"{sub}{fam}.py")
            pat = re.compile(r"\b" + re.escape(fam) + r"\b")
            for num, text in self.test_text.items():
                if pat.search(text):
                    out.add(num)
            for path, users in self.opens.items():
                if pat.search(self.fixture_text(path)):
                    out |= users
        return out

    def class_scope(self, cls):
        """Tests that plausibly use class `cls` or a subclass of it, or None if unsure."""
        names = {cls.name}
        changed = True
        while changed:
            changed = False
            for c, bases in self.class_bases.items():
                if c not in names and any(b in names for b in bases):
                    names.add(c)
                    changed = True
        if len(names) > 40:
            return None  # a base class with many subclasses: too broad to reason about
        out = set()
        words = set()
        for n in names:
            for rel, a, b in self.refs.get(n, []):
                out |= self.cov(rel, a, b)
            for rel, a, b in self.funcs.get(n.lower(), []) + self.funcs.get(n.lower() + "sql", []):
                out |= self.cov(rel, a, b)
            words.add(n.lower())
            words.add(re.sub(r"(?<!^)(?=[A-Z])", "_", n).lower())
        # explicit SQL names declared in the class body
        for node in ast.walk(cls):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_sql_names" for t in node.targets):
                for c in ast.walk(node.value):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        words.add(c.value.lower())
        pats = [re.compile(r"\b" + re.escape(w) + r"\b") for w in words if len(w) > 2]
        for num, text in self.test_text.items():
            if any(p.search(text) for p in pats):
                out.add(num)
        for path, users in self.opens.items():
            ft = self.fixture_text(path)
            if any(p.search(ft) for p in pats):
                out |= users
        return out

    def fixture_text(self, path):
        if path not in self._fixture_text:
            try:
                p = SRC / path
                self._fixture_text[path] = p.read_text(errors="ignore").lower() if p.suffix in (".sql", ".json", ".txt", ".csv") else ""
            except OSError:
                self._fixture_text[path] = ""
        return self._fixture_text[path]

    # ----- one python file
    def python_file(self, rel, fdiff):
        old_path = SRC / rel
        if not old_path.exists():
            return ALL, "missing source"
        old_src = old_path.read_text(encoding="utf-8")
        old_lines = old_src.split("\n")
        new_lines, removed, anchors, added = apply_hunks(old_lines, fdiff["hunks"])
        new_src = "\n".join(new_lines)
        try:
            old_tree = ast.parse(old_src)
        except SyntaxError:
            return ALL, "old unparsable"
        try:
            new_tree = ast.parse(new_src)
        except SyntaxError:
            return self.broken_module(rel), "syntax error"
        if ast.dump(old_tree) == ast.dump(new_tree):
            return set(), "ast unchanged"
        if canonical(old_tree) == canonical(new_tree):
            return set(), "equivalent"

        sel = set()
        reasons = []
        changed_old = {ln for ln in removed if not inert(old_lines[ln - 1])}
        real_added = {ln for ln in added if not inert(new_lines[ln - 1])}
        # anchors for real additions only
        add_anchor = set()
        for os_, ol, body in fdiff["hunks"]:
            pos = os_ if ol > 0 else os_ + 1
            for tag, text in body:
                if tag == "+" and not inert(text):
                    add_anchor |= {pos - 1, pos}
                if tag in (" ", "-"):
                    pos += 1

        def handle_old_line(ln):
            loc = locate(old_tree, ln)
            if loc is None:
                return ALL
            kind, (a, b), stmt, chain = loc
            if kind == "import":
                self.import_touched[rel] = new_src
            if kind == "runtime":
                got = self.cov(rel, a, b)
                if not got:
                    fn = next((c for c in reversed(chain) if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
                    if fn is not None and fn.body:
                        got = self.cov(rel, fn.body[0].lineno, fn.end_lineno)
                return got
            return self.import_change(rel, stmt, chain)

        for ln in sorted(changed_old):
            r = handle_old_line(ln)
            if r == ALL:
                return ALL, f"unlocated old line {ln}"
            sel |= r
        if real_added:
            # what kind of code was added? look at the new tree
            for ln in sorted(real_added):
                loc = locate(new_tree, ln)
                if loc is None:
                    return ALL, f"unlocated new line {ln}"
                kind, _, stmt, chain = loc
                if kind == "import":
                    self.import_touched[rel] = new_src
                    r = self.import_change(rel, stmt, chain)
                    if r == ALL:
                        return ALL, "import add"
                    sel |= r
            for ln in sorted(add_anchor):
                if 1 <= ln <= len(old_lines) and not inert(old_lines[ln - 1]):
                    r = handle_old_line(ln)
                    if r == ALL:
                        return ALL, f"unlocated anchor {ln}"
                    sel |= r
                elif 1 <= ln <= len(old_lines):
                    # blank/comment neighbour: walk to nearest code line both ways
                    for step in (-1, 1):
                        k = ln
                        while 1 <= k <= len(old_lines) and inert(old_lines[k - 1]):
                            k += step
                        if 1 <= k <= len(old_lines):
                            r = handle_old_line(k)
                            if r == ALL:
                                return ALL, "anchor walk"
                            sel |= r
        if not sel:
            # a real change that no test reaches: nothing can fail on it, unless the map is blind
            return set(), "no coverage"
        return sel, "ok"

    def test_module_guard(self, rel, chain):
        return set()

    def broken_module(self, rel):
        if rel.startswith("tests/"):
            return ALL
        return ALL

    def import_change(self, rel, stmt, chain):
        """A change to code that runs at import time (module or class body)."""
        names = target_names(stmt)
        for c in chain:
            if isinstance(c, ast.ClassDef):
                names.add(c.name)
        if rel.startswith("tests/"):
            # module/class-level code in a test module: every test in the file can see it
            out = set(self.by_file.get(rel, set()))
            out |= self.names_reach(target_names(stmt))
            return out
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.body:
            # a def line, default argument or annotation: it only matters when the
            # function runs, and every call runs its body
            out = self.cov(rel, stmt.body[0].lineno, stmt.end_lineno)
            if stmt.decorator_list:
                out |= self.names_reach({stmt.name})
            return out if out else set()
        own = target_names(stmt)
        if not own:
            return ALL
        reach = self.names_reach(own)
        # tests that read the name directly (e.g. a test walking every module's table)
        direct = set()
        for n in own:
            for r, a, b in self.refs.get(n, []):
                if r.startswith("tests/"):
                    direct |= self.cov(r, a, b)
        d = self.dialect_of(rel)
        classes = [c for c in chain if isinstance(c, ast.ClassDef)]
        if isinstance(stmt, ast.ClassDef) and not classes:
            classes = []  # a class header change: its bases/decorators affect the class everywhere
        if classes and not d:
            scope = self.class_scope(classes[-1])
            if scope is not None:
                narrowed = reach & scope
                return (narrowed if narrowed else scope) | direct
        if d:
            scope = self.dialect_scope(d)
            narrowed = reach & scope
            return narrowed | (scope if not narrowed else set()) | direct
        if not reach:
            return ALL
        return reach

    # ----- data files
    def data_file(self, rel):
        if rel in self.opens_at_collect:
            return ALL
        if rel in self.opens:
            return set(self.opens[rel])
        if rel.startswith("tests/"):
            stem = Path(rel).stem
            hits = {n for n, text in self.test_text.items() if stem.lower() in text}
            return hits if hits else ALL
        if rel.endswith((".md", ".rst", ".txt")) or rel.startswith(("posts/", "docs/", "benchmarks/")):
            return set()
        return ALL

    CONFIG = ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py", "setup.py", "requirements")

    def outside_file(self, rel):
        """A file outside sqlglot/ and tests/: only tests that name it can see it,
        unless it configures the test run itself."""
        name = Path(rel).name
        if rel.count("/") == 0 and name.startswith(self.CONFIG):
            return ALL
        if rel.startswith(("posts/", "docs/", "benchmarks/", "pdoc/", ".github/")) or name.endswith((".md", ".rst", ".png", ".txt")):
            return set()
        parts = {name.lower(), rel.lower()}
        top = rel.split("/")[0].lower()
        keys = set(parts)
        if top != name.lower():
            keys.add(top)
        hits = set()
        for tf, nums in self.by_file.items():
            try:
                text = (SRC / tf).read_text(errors="ignore").lower()
            except OSError:
                continue
            if any(k in text for k in keys):
                hits |= nums
        return hits

    # ----- import-time changes can break the import itself
    PROBE_DIR = Path(os.environ.get("BR_PROBE", "/tmp/probe"))

    def probe(self, changed):
        """Import the patched modules in a fresh interpreter. A crash there fails every
        test that imports them, which no coverage map can see."""
        import shutil
        import subprocess

        root = self.PROBE_DIR
        if not (root / "sqlglot").exists():
            shutil.copytree(SRC / "sqlglot", root / "sqlglot", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(SRC / "tests", root / "tests", ignore=shutil.ignore_patterns("fixtures", "__pycache__"))
        saved = {}
        try:
            for rel, new_src in changed.items():
                p = root / rel
                saved[rel] = p.read_text(encoding="utf-8") if p.exists() else None
                p.write_text(new_src, encoding="utf-8")
            lines = ["import importlib, sqlglot", "from sqlglot.dialects.dialect import Dialect"]
            for rel in changed:
                mod = rel[:-3].replace("/", ".")
                if mod.endswith(".__init__"):
                    mod = mod[: -len(".__init__")]
                lines.append(f"importlib.import_module({mod!r})")
                d = self.dialect_of(rel)
                if d:
                    lines.append(
                        f"try:\n    _d = Dialect.get_or_raise({d!r})\nexcept ValueError:\n    _d = None\n"
                        f"if _d is not None:\n    sqlglot.transpile('SELECT a, b FROM t WHERE c = 1', read=_d, write=_d)"
                    )
            lines.append("sqlglot.transpile('SELECT a, b FROM t WHERE c = 1')")
            env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")
            r = subprocess.run([sys.executable, "-c", "\n".join(lines)], cwd=str(root), env=env,
                               capture_output=True, timeout=20)
            return r.returncode == 0
        except Exception:
            return False
        finally:
            for rel, old in saved.items():
                p = root / rel
                if old is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_text(old, encoding="utf-8")

    def select(self, patch_text):
        self.import_touched = {}
        sel, why = self._select(patch_text)
        self.import_touched.pop("sqlglot/__main__.py", None)  # importing it runs the CLI
        if sel != ALL and self.import_touched:
            if not self.probe(self.import_touched):
                return ALL, why + ";import probe failed"
            why += ";import probe ok"
        return sel, why

    def _select(self, patch_text):
        files = parse_patch(patch_text)
        if not files:
            return set(), "empty"
        sel = set()
        why = []
        for f in files:
            rel = f["old"] or f["new"]
            if f.get("binary") or f.get("renamed") or f.get("deleted") or f["old"] is None or f["new"] is None:
                return ALL, f"structural change {rel}"
            if rel.endswith(".py") and (rel.startswith("sqlglot/") or rel.startswith("tests/")):
                r, reason = self.python_file(rel, f)
            elif not rel.startswith(("sqlglot/", "tests/")):
                r, reason = self.outside_file(rel), "outside"
            else:
                r, reason = self.data_file(rel), "data"
            why.append(f"{rel}:{reason}")
            if r == ALL:
                return ALL, ";".join(why)
            sel |= r
        return sel, ";".join(why)


def split(numbers, ms):
    """Choose the worker count and LPT assignment that minimise max load + 2 s each."""
    nums = sorted(numbers, key=lambda n: (-ms[n], n))
    if not nums:
        return []
    best = None
    for k in range(1, MAX_WORKERS + 1):
        loads = [0] * k
        workers = [[] for _ in range(k)]
        for n in nums:
            j = loads.index(min(loads))
            workers[j].append(n)
            loads[j] += ms[n]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, [w for w in workers if w])
    return best[1]


def main():
    s = Selector()
    all_nums = set(s.ms)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            sel, why = s.select(patch.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # never crash: run everything
            sel, why = ALL, f"error {e!r}"
        if DIAG_EMPTY_ALL and sel != ALL and not sel:
            sel, why = ALL, why + ";diag: empty -> all"
        if sel == ALL:
            sel = all_nums
        sel = {n for n in sel if n in s.ms}
        workers = split(sel, s.ms)
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        log(f"{patch.stem}: {len(sel)} tests ({why})")


if __name__ == "__main__":
    main()
