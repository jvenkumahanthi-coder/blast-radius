# Blast Radius entry

Test selection for sqlglot changes (Builders Buildathon, RWX track).

## How it works

Everything is a map of the pinned sqlglot commit (`eb11aec`); nothing is keyed by patch.

1. **Per-test coverage** (`covmap.json.gz`): every sqlglot test was run once with
   line coverage switched per test, so for each line of `sqlglot/` and `tests/` we
   know which tests execute it. Work that the optimizer and executor tests send to a
   process pool was run in-process (with the same pickling) so it is attributed to
   the right test. The same run recorded which data files (fixtures) each test opens.
2. **Source index** (`names.json`, built in the image by `build_index.py` from the
   pinned source): where every identifier is used, at run time and at import time.
3. **Selection** (`selector.py`), for each file in a patch:
   - Python file whose AST is unchanged (comments, blank lines): nothing.
   - Changed line inside a function: tests covering the whole enclosing statement.
   - Added lines: tests covering the neighbouring statements.
   - Import-time change (module or class body): tests covering run-time uses of the
     assigned names, followed through import-time assignments; for dialect files,
     narrowed to tests that touch that dialect or a dialect built on it.
   - Fixture or other data file: the tests that opened it.
   - Code that runs while sqlglot is imported (builder functions, class hooks):
     what it builds outlives the import, so add the tests that use its closures and
     the tables built from it (`importset.json.gz`: lines run while sqlglot and every
     dialect load). Import-time changes are also imported in a scratch copy; a crash
     there runs every test.
   - Anything the analysis cannot place: every test, on 8 workers.
4. **Workers**: longest-first assignment, choosing the worker count that minimises
   busiest load + 2 s per worker.

## Rebuilding the coverage map

`tools/covplugin.py` and `tools/build_map.py`. With the packages in the kit's
`requirements.lock` installed and the sqlglot checkout as the working directory:

```sh
SKIP_INTEGRATION=0 PYTHONPATH=.:/path/to/tools python -m pytest -p covplugin -n 4 tests
python /path/to/tools/build_map.py covmap.json && gzip -9 covmap.json
```

## Code brought in

Only the Python standard library at run time. The build downloads the pinned
sqlglot source from its own GitHub repository. The coverage map was built with
`coverage` and the packages listed in the kit's `requirements.lock`.
