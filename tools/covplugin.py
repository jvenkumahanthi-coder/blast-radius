"""pytest plugin: per-test line coverage contexts + per-test data-file opens."""
import json, os, sys
import coverage
import concurrent.futures as _cf


class _InlinePool(_cf.ThreadPoolExecutor):
    """Process pools run work in other processes, where per-test coverage is lost.
    For mapping only, run that work on one thread in this process instead."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        super().__init__(max_workers=1, initializer=initializer, initargs=initargs)

    def submit(self, fn, /, *args, **kwargs):
        # keep the pickling a real process pool does on the way in and out
        import pickle
        fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))

        def run():
            return pickle.loads(pickle.dumps(fn(*args, **kwargs)))

        return super().submit(run)


_cf.ProcessPoolExecutor = _InlinePool

ROOT = os.path.realpath(os.getcwd())
_cov = coverage.Coverage(data_file=os.environ.get("COVFILE", ".covctx"),
                         source=[os.path.join(ROOT, "sqlglot"), os.path.join(ROOT, "tests")],
                         data_suffix=True)
_cov.start()
_current = ["<startup>"]
_opens = {}

def _hook(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        try:
            p = os.fsdecode(args[0])
            if not os.path.isabs(p):
                p = os.path.join(os.getcwd(), p)
            p = os.path.realpath(p)
        except Exception:
            return
        if p.startswith(ROOT + os.sep) and not p.endswith((".py", ".pyc")):
            _opens.setdefault(_current[0], set()).add(os.path.relpath(p, ROOT))
sys.addaudithook(_hook)

import pytest

@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    prev = _current[0]
    _current[0] = "<collect>" + collector.nodeid
    _cov.switch_context(_current[0])
    yield
    _current[0] = prev
    _cov.switch_context(prev)

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _current[0] = item.nodeid
    _cov.switch_context(item.nodeid)
    yield
    _current[0] = "<between>"
    _cov.switch_context("<between>")

def pytest_unconfigure(config):
    _cov.stop()
    _cov.save()
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(f".opens.{wid}.json", "w") as f:
        json.dump({k: sorted(v) for k, v in _opens.items()}, f)
