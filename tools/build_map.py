"""Combine coverage contexts + opens into a compact map: file -> line -> test ids."""
import glob, json, os, sys
import coverage

root = os.path.realpath(".")
cov = coverage.Coverage(data_file=".covctx")
cov.combine(data_paths=glob.glob(".covctx.*"), keep=True)
data = cov.get_data()
tests = []
tid = {}
def T(ctx):
    if ctx not in tid:
        tid[ctx] = len(tests); tests.append(ctx)
    return tid[ctx]
setidx = {}
sets = []
def S(s):
    k = tuple(sorted(s))
    if k not in setidx:
        setidx[k] = len(sets); sets.append(list(k))
    return setidx[k]
lines = {}
collect = {}
for f in sorted(data.measured_files()):
    rel = os.path.relpath(f, root)
    byline = data.contexts_by_lineno(f)
    fl = {}
    for ln, ctxs in byline.items():
        real = {T(c) for c in ctxs if "::" in c and not c.startswith("<")}
        coll = {c for c in ctxs if c.startswith("<collect>")}
        if coll or any(c in ("", "<startup>", "<between>") for c in ctxs):
            collect.setdefault(rel, []).append(ln)
        if real:
            fl[str(ln)] = S(real)
    lines[rel] = fl
opens = {}
for p in glob.glob(".opens.*.json"):
    for k, v in json.load(open(p)).items():
        for path in v:
            if "__pycache__" in path or path.startswith(".opens"):
                continue
            key = k if "::" in k and not k.startswith("<") else "<collect>"
            opens.setdefault(path, set()).add(key)
opens_out = {p: sorted(T(k) for k in v if k != "<collect>") for p, v in opens.items()}
opens_collect = sorted(p for p, v in opens.items() if "<collect>" in v)
out = {"tests": tests, "sets": sets, "lines": lines, "import_lines": {k: sorted(v) for k, v in collect.items()},
       "opens": opens_out, "opens_at_collect": opens_collect}
json.dump(out, open(sys.argv[1], "w"), separators=(",", ":"))
print(len(tests), len(sets), sum(len(v) for v in lines.values()), len(opens_out), opens_collect[:10])
