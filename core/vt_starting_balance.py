"""
Wave 1C.3 (Vibe-Trading): starting_balance persistente para o dia.

Bug historico (Pitfall #20 do skill `vibe-trading-watchdog-sync`):
`monitoring/vt_copilot.py:739` tinha `base_balance = 1002230.57` HARDCODED
como fallback quando o saldo MT5 nao casava com o que o DB tinha.
Resultado: o intraday report mostrava PnL errado acumulado (R$403,83 de
drift so entre 02/07 e 08/07). O comment antigo sugeria ler
`/tmp/vt_autotrader_state.json`, mas o autotrader (Fase 3 desde 01/07)
NAO escreve mais nesse path (state virou projecao em memoria).

Fix: snapshot diario do saldo MT5 no startup do daemon, gravado em
`/tmp/vt_intraday_starting_balance.json` (path novo, nao mexe no
STATE_FILE legado). O copilot le esse helper em vez do STATE_FILE.

Schema do arquivo JSON:
    {
        "date": "YYYY-MM-DD",
        "balance": float,
        "ts": "<iso timestamp>",
        "source": "autotrader_startup" | "manual"
    }

Idempotencia: `set_today_starting_balance` recusa overwrite se ja
existe entrada para hoje. Defesa em profundidade: o caller
(autotrader.record_starting_balance) tambem checa `is None` antes
de chamar.

Sanity: `set_today_starting_balance` rejeita 0 / negativo / > 10M
(montante absurdo pra mini-contrato BM&F). Quem chama precisa ter
um valor razoavel de saldo MT5.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, date
from pathlib import Path
from typing import Optional


# Path NOVO. NAO confundir com STATE_FILE legado em /tmp/vt_autotrader_state.json
# (esse foi descontinuado na Fase 3 / 2026-07-01 e NAO deve ser tocado aqui).
STARTING_BALANCE_PATH = Path("/tmp/vt_intraday_starting_balance.json")

# Sanity range. Saldo MT5 normal da conta demo ~R$ 1M. Acima de 10M eh
# erro / dado lixo; abaixo/igual a zero eh conta zerada / erro de leitura.
MIN_BALANCE = 0.0
MAX_BALANCE = 10_000_000.0


def _today_str() -> str:
    """YYYY-MM-DD em horario local (B3 = America/Sao_Paulo, mas o sistema
    ja roda com TZ do Brasil configurada via OS env)."""
    return date.today().isoformat()


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [STARTING-BALANCE] {msg}", flush=True)


def get_today_starting_balance() -> Optional[float]:
    """Retorna o saldo de abertura gravado para hoje, ou None.

    Logica:
    - Sem arquivo: retorna None (helper eh opcional, fallback fica em caller).
    - Arquivo de outro dia: retorna None (stale, nao cruzar dias).
    - Arquivo de hoje: retorna `balance` (float).

    NUNCA levanta excecao para o caller — qualquer erro de I/O ou JSON
    malformed retorna None + log. O caller cai no fallback dele.
    """
    try:
        if not STARTING_BALANCE_PATH.exists():
            _log(f"sem snapshot em {STARTING_BALANCE_PATH} — caller usa fallback")
            return None

        raw = STARTING_BALANCE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)

        if not isinstance(data, dict):
            _log("snapshot malformado (nao-dict) — caller usa fallback")
            return None

        snap_date = data.get("date")
        balance = data.get("balance")
        source = data.get("source", "?")

        if snap_date != _today_str():
            _log(
                f"snapshot eh de {snap_date} (hoje {_today_str()}) "
                f"— caller usa fallback (nao cruzamos dia)"
            )
            return None

        if not isinstance(balance, (int, float)):
            _log(f"snapshot de hoje tem balance invalido ({type(balance).__name__}) — caller usa fallback")
            return None

        _log(
            f"baseline {snap_date} = R$ {float(balance):,.2f} "
            f"(source={source}) — caller vai usar"
        )
        return float(balance)

    except Exception as e:
        _log(f"erro ao ler snapshot: {type(e).__name__}: {e} — caller usa fallback")
        return None


def set_today_starting_balance(balance: float, source: str = "manual") -> bool:
    """Grava snapshot de starting balance para HOJE em disco (atomic write).

    Returns:
        True  — escreveu novo snapshot.
        False — JA existia snapshot para hoje (idempotente, NAO pisa em valor
                ja gravado; defesa contra restart mid-day sobrescrever baseline).

    Raises:
        ValueError se `balance` estiver fora de (0, 10_000_000).
            Cobre 0, negativo, None, NaN-ish, valor absurdo.

    Atomic write: escreve em <path>.tmp e faz os.rename. Garante que
    outro processo lendo simultaneamente nao veja arquivo truncado.
    """
    # Sanity check primeiro: se o caller mandou lixo, NAO tocamos em disco.
    if not isinstance(balance, (int, float)):
        raise ValueError(
            f"balance precisa ser float, recebeu {type(balance).__name__}"
        )
    balance_f = float(balance)
    if not (MIN_BALANCE < balance_f < MAX_BALANCE):
        raise ValueError(
            f"balance fora da faixa sanity ({MIN_BALANCE}, {MAX_BALANCE}): "
            f"{balance_f}"
        )

    # Idempotencia do dia: se ja tem entrada de hoje, nao pisa.
    existing = None
    if STARTING_BALANCE_PATH.exists():
        try:
            existing = json.loads(STARTING_BALANCE_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = None  # arquivo corrompido: vai sobrescrever

    if isinstance(existing, dict) and existing.get("date") == _today_str():
        _log(
            f"snapshot de hoje ja existe ({existing.get('date')} = "
            f"R$ {float(existing.get('balance', 0)):,.2f}, "
            f"source={existing.get('source')}); recusando overwrite "
            f"com R$ {balance_f:,.2f} (source={source})"
        )
        return False

    payload = {
        "date": _today_str(),
        "balance": balance_f,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": source,
    }

    # Atomic write via tmp sidecar + rename.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".vt_intraday_starting_balance_", suffix=".tmp", dir="/tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(STARTING_BALANCE_PATH))
    except Exception:
        # Limpa tmp sidecar em caso de erro pra nao deixar lixo em /tmp.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    _log(
        f"snapshot gravado: {STARTING_BALANCE_PATH} = "
        f"R$ {balance_f:,.2f} (source={source}, date={payload['date']})"
    )
    return True
