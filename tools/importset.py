"""Lines of sqlglot that run while sqlglot and every dialect are being loaded.
Their effects live on in module and class state."""
import coverage, json, os, sys, importlib, pkgutil
root = os.path.realpath(".")
cov = coverage.Coverage(data_file=None, source=[os.path.join(root, "sqlglot")])
cov.start()
import sqlglot
from sqlglot.dialects.dialect import Dialect, Dialects
for d in Dialects:
    try:
        Dialect.get_or_raise(d.value)
    except Exception as e:
        print("skip", d, e, file=sys.stderr)
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if m.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(m.name)
    except Exception as e:
        print("skip", m.name, e, file=sys.stderr)
cov.stop()
data = cov.get_data()
out = {os.path.relpath(f, root): sorted(data.lines(f)) for f in data.measured_files()}
json.dump(out, open(sys.argv[1], "w"))
print(sum(len(v) for v in out.values()))
