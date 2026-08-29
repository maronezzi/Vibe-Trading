"""
vt_balance_history.py
=====================
Wave N+1B (09/07/2026): Histórico de saldo MT5 para gerar gráfico intraday
quando MT5 demo não persiste deals E DB trades são todos GHOST (filtrados).

Por que existe:
- check_intraday_stats() usa MT5 history ou DB trades para construir pnl_series
  → série temporal usada por render_pnl_chart() para plotar PnL acumulado.
- MT5 demo (XP Investimentos?) não persiste deals → MT5 history vazio.
- DB trades de hoje estão com exit_reason='GHOST' (bug autotrader não
  pegou exit_price real) → filtro intencional Bruno 02/07 os exclui.
- Resultado: pnl_series=[] → gráfico placeholder "Sem trades fechados".

Solução: a cada chamada do copilot, gravar (ts, balance, pnl_delta) em
arquivo JSON local. Quando MT5/DB estão vazios, construir pnl_series a
partir deste histórico.

Arquivo: /tmp/vt_intraday_balance_history.json
Formato: [{"ts": "2026-07-09T10:20:36", "balance": 1002250.0, "pnl_delta": 19.43}]
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("/tmp/vt_intraday_balance_history.json")

# Lock para concorrência entre copilot, watchdog, autotrader
_lock = threading.Lock()


def append_snapshot(
    path: Path | str = DEFAULT_PATH,
    balance: float = 0.0,
    pnl_delta: float = 0.0,
    source: str = "MT5_STATUS",
) -> None:
    """Adiciona snapshot (ts, balance, pnl_delta, source) ao histórico.

    Dedup: se último snapshot tem o mesmo balance dentro de 60s, não appenda
    (evita poluição quando MT5 status() é chamado em rajada por múltiplos
    módulos).
    """
    path = Path(path)
    with _lock:
        history = _read_unsafe(path)
        ts_now = datetime.now().isoformat(timespec="seconds")

        # Dedup: mesmo balance nos últimos 60s → no-op
        if history:
            try:
                last_ts = datetime.fromisoformat(history[-1]["ts"])
                if (
                    history[-1]["balance"] == balance
                    and (datetime.now() - last_ts).total_seconds() < 60
                ):
                    return
            except (KeyError, ValueError):
                pass

        history.append(
            {
                "ts": ts_now,
                "balance": round(float(balance), 2),
                "pnl_delta": round(float(pnl_delta), 2),
                "source": source,
            }
        )

        _atomic_write_unsafe(path, history)


def read_history(path: Path | str = DEFAULT_PATH) -> list[dict[str, Any]]:
    """Lê histórico descartando snapshots de dias anteriores."""
    with _lock:
        history = _read_unsafe(path)

    today = datetime.now().date()
    filtered = []
    for h in history:
        try:
            ts = datetime.fromisoformat(h["ts"])
            if ts.date() == today:
                filtered.append(h)
        except (KeyError, ValueError):
            continue
    return filtered


def clear(path: Path | str = DEFAULT_PATH) -> None:
    """Limpa arquivo (uso de teste ou reset manual)."""
    with _lock:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


# ──────────────────────────── helpers internos ────────────────────────────


def _read_unsafe(path: Path) -> list[dict[str, Any]]:
    """Lê JSON sem lock (uso interno). Retorna [] se vazio/ausente."""
    try:
        if not path.exists():
            return []
        raw = path.read_text().strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _atomic_write_unsafe(path: Path, data: list) -> None:
    """Escreve atomicamente: escreve em temp + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
