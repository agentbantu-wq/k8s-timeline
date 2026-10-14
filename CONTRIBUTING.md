# Contributing to k8s-timeline

## Ground rules

- **Stdlib only.** No new dependencies — the zero-install story is a feature. If you need a library, you probably need a smaller idea.
- **Read-only by default.** The tool never mutates cluster state. Keep it that way.
- **Test everything.** `python -m pytest` must pass. Collector tests use the `FakeClient` in `tests/test_collectors.py` — no live cluster needed.

## Adding a collector

1. Add a `collect_*` function in `src/k8s_timeline/collectors.py` returning raw entry dicts (`timestamp`, `category`, `kind`, `name`, `namespace`, `level`, `reason`, `message`, `count`).
2. Wire it into `cmd_build` in `cli.py`.
3. Add tests with `FakeClient`.

## Release checklist

- Bump `__version__` in `src/k8s_timeline/__init__.py` and `pyproject.toml`
- `python -m pytest`
- Tag and create a GitHub Release (drives the Zenodo DOI)
