"""Build-time index of the pinned sqlglot source.

Runs inside `docker build` (and locally). Reads the pristine sqlglot checkout and
writes names.json:

- refs: identifier -> list of [file, first_line, last_line] spans of the
  *runtime* statements (inside a function or lambda) that mention it, as a
  Name, an Attribute or a string constant (getattr-style lookups).
- import_refs: identifier -> list of [file, [target names]] for import-time
  statements (module or class body) that mention it, so a changed constant can
  be followed through `B = A + ...` chains.
- dialect_parents: class name -> base class names for classes in sqlglot/dialects,
  so a change to Postgres also reaches Materialize, Redshift, ...
- test_text: test node id -> lowercase source of the test function (used to scope
  dialect-specific changes).

Nothing here looks at any patch: it is a map of the pinned commit only.
"""

import ast
import json
import os
import sys
from collections import defaultdict


def walk_py(root, sub):
    for d, _, files in os.walk(os.path.join(root, sub)):
        for f in files:
            if f.endswith(".py"):
                full = os.path.join(d, f)
                yield os.path.relpath(full, root), full


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


def mentioned(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            out.add(n.value)
    return out


def index_file(rel, src, refs, import_refs):
    tree = ast.parse(src)

    def visit_body(stmts, runtime):
        for s in stmts:
            if runtime:
                # every simple statement inside a function: record its own span
                for n in mentioned_header(s):
                    refs[n].append([rel, s.lineno, header_end(s)])
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not runtime:
                    # decorators / defaults run at import time
                    names = set()
                    for d in s.decorator_list + s.args.defaults + [x for x in s.args.kw_defaults if x]:
                        names |= mentioned(d)
                    for n in names:
                        import_refs[n].append([rel, [s.name]])
                visit_body(s.body, True)
            elif isinstance(s, ast.ClassDef):
                if not runtime:
                    for b in s.bases:
                        for n in mentioned(b):
                            import_refs[n].append([rel, [s.name]])
                visit_body(s.body, runtime)
            else:
                if not runtime:
                    tn = sorted(target_names(s))
                    for n in mentioned(s):
                        if tn:
                            import_refs[n].append([rel, tn])
                    # lambdas inside import-time code run later
                    for lam in ast.walk(s):
                        if isinstance(lam, ast.Lambda):
                            for n in mentioned(lam.body):
                                refs[n].append([rel, lam.lineno, lam.end_lineno])
                for child in child_blocks(s):
                    visit_body(child, runtime)

    visit_body(tree.body, False)


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
        first = min(b[0].lineno for b in blocks)
        return max(s.lineno, first - 1)
    return s.end_lineno


def mentioned_header(s):
    """Names in the statement itself, excluding nested statement blocks."""
    blocks = list(child_blocks(s))
    if not blocks:
        return mentioned(s)
    out = set()
    for field, value in ast.iter_fields(s):
        if field in ("body", "orelse", "finalbody", "handlers", "cases"):
            continue
        if isinstance(value, ast.AST):
            out |= mentioned(value)
        elif isinstance(value, list):
            for v in value:
                if isinstance(v, ast.AST):
                    out |= mentioned(v)
    return out


IO_HINTS = ("open(", "open_file", "load_sql", "read_text", "read_csv", "read(", "fixtures", "FIXTURES", "json.load", "csv.")


def class_setups(root):
    """Test classes (and modules) whose class/module-level setup reads files. Whatever
    it loads is shared by every test of the class, though only the first test that
    happens to run the setup is seen opening the file."""
    out = []
    for rel, full in walk_py(root, "tests"):
        src = open(full, encoding="utf-8").read()
        lines = src.splitlines()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "setUpModule":
                text = "\n".join(lines[node.lineno - 1 : node.end_lineno])
                if any(h in text for h in IO_HINTS):
                    out.append(rel)
            if isinstance(node, ast.ClassDef):
                for fn in node.body:
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in ("setUpClass", "setUp_class"):
                        text = "\n".join(lines[fn.lineno - 1 : fn.end_lineno])
                        if any(h in text for h in IO_HINTS):
                            out.append(f"{rel}::{node.name}")
    return out


def test_texts(root):
    out = {}
    for rel, full in walk_py(root, "tests"):
        if not os.path.basename(rel).startswith("test_"):
            continue
        src = open(full, encoding="utf-8").read()
        lines = src.splitlines()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef):
                for fn in cls.body:
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
                        out[f"{rel}::{cls.name}::{fn.name}"] = "\n".join(
                            lines[fn.lineno - 1 : fn.end_lineno]
                        ).lower()
            elif isinstance(cls, (ast.FunctionDef, ast.AsyncFunctionDef)) and cls.name.startswith("test"):
                out[f"{rel}::{cls.name}"] = "\n".join(lines[cls.lineno - 1 : cls.end_lineno]).lower()
    return out


def main(root, out_path):
    refs = defaultdict(list)
    import_refs = defaultdict(list)
    parents = {}
    class_bases = defaultdict(set)
    funcs = defaultdict(list)
    for sub in ("sqlglot", "tests"):
        for rel, full in walk_py(root, sub):
            src = open(full, encoding="utf-8").read()
            try:
                index_file(rel, src, refs, import_refs)
            except SyntaxError:
                continue
            if rel.startswith("sqlglot/"):
                for node in ast.walk(ast.parse(src)):
                    if isinstance(node, ast.ClassDef):
                        for b in node.bases:
                            bn = b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
                            if bn:
                                class_bases[node.name].add(bn)
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                        funcs[node.name.lower().replace("_", "")].append([rel, node.body[0].lineno, node.end_lineno])
            if rel.startswith("sqlglot/dialects/"):
                for node in ast.parse(src).body:
                    if isinstance(node, ast.ClassDef):
                        parents[node.name] = [b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in node.bases]
    out = {
        "refs": refs,
        "import_refs": import_refs,
        "dialect_parents": parents,
        "class_bases": {k: sorted(v) for k, v in class_bases.items()},
        "funcs": funcs,
        "test_text": test_texts(root),
        "class_setups": class_setups(root),
    }
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
