"""Build step: unpack the pinned sqlglot source (sqlglot/ and tests/ only) and index it."""

import os
import shutil
import subprocess
import sys
import tarfile

archive, dest = sys.argv[1], sys.argv[2]
with tarfile.open(archive) as tar:
    members = []
    for m in tar.getmembers():
        parts = m.name.split("/", 1)
        if len(parts) < 2 or not parts[1].startswith(("sqlglot/", "tests/")):
            continue
        if not (m.isfile() or m.isdir()):
            continue
        m.name = parts[1]
        members.append(m)
    tar.extractall(dest, members=members, filter="data")
here = os.path.dirname(os.path.abspath(__file__))
subprocess.run([sys.executable, os.path.join(here, "build_index.py"), dest, os.path.join(here, "names.json")], check=True)
# readable by the unprivileged user the scorer runs as
for root, dirs, files in os.walk(here):
    for d in dirs:
        os.chmod(os.path.join(root, d), 0o755)
    for f in files:
        os.chmod(os.path.join(root, f), 0o644)
print("prepared", sum(len(f) for _, _, f in os.walk(dest)), "source files")
