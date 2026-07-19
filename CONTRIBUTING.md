# Contributing

**This repo is Bruno Maronezzi's live BM&F auto-trading system** — real money,
real broker orders via MetaTrader 5 on Linux/Wine. The active code is the
Python under `core/`, `mt5/`, `monitoring/`, `optimization/`, `strategies/`,
`backtest/`, `scripts/`, `tests/` (refactor 2026-06-22).

> **Note (Wave 880, 2026-07-19):** an older `CONTRIBUTING.md` describing the
> archived HKUDS `agent/` + `frontend/` framework has been removed — none of
> its commands or paths worked against the live tree. The historical content
> is preserved at `archive/CONTRIBUTING_HKUDS.md` for reference.

## Authoritative docs

- **`AGENTS.md`** — repo layout, commands, conventions, safety surfaces.
- **`CLAUDE.md`** — deeper architecture notes + safety-critical surfaces
  (load-bearing: `vt_config.json` and `core/vt_emergency.py`).

Read both before touching anything. They are the source of truth for paths
and commands.

## Local verification path

```bash
ruff check .                       # config in pyproject.toml (target py312, line 120, E501 off)
python -m pytest tests/ -q         # always by path; conftest isolates config + DB
PYTHONPATH=. python backtest/backtest_v944.py   # replicates the autotrader (reads vt_config.json)
```

CI (`.github/workflows/test.yml`) runs the same `ruff` + `pytest` on the
active dirs. A frontend step in CI fails because `frontend/` is absent —
that step is broken legacy, not a real gate.

## Conventions (PT-BR)

- Comments and docstrings are PT-BR. Match that when editing existing files.
- Commits use `Wave N — <descrição>` PT-BR subjects, often suffixed
  `(Bruno)` or `(sub-agente)`. Echo the wave name into `_updated_by` in
  `vt_config.json` so lineage stays greppable.
- Only three categories may write `vt_config.json`:
  `core/vt_config_loader.py` (canonical API), `optimization/agi_v4/*`
  (the authorized optimizer), or `scripts/*` invoked **with the autotrader
  paused**. The loader enforces a write-lock + authorized-writer whitelist.
- One `sys.path.insert(0, ...)` bootstrap per top-level module — do not
  rely on packaging. `tests/conftest.py` mirrors the same path injection.
