"""
TDD dos locks de escrita do vt_config_loader — anti-race contra rewrite
involuntária (incidente 01/07/2026: config regrediu de 580→18 linhas duas
vezes em poucas horas).

Cobre:
  - is_write_locked() / assert_write_unlocked()
  - acquire_write_lock(operator, reason) → bool
  - release_write_lock()
  - Stale force-acquire (>300s)
  - save_full_config: lock é held durante _atomic_write, levantado em raise
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import vt_config_loader


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _lock_isolation(tmp_path, monkeypatch):
    """Cada teste roda com CONFIG_PATH/LOCK_PATH em tmp_path.

    Garante que o lock file de teste não toca o real vt_config.json.lock
    (o autotrader PID 665279 está rodando em paralelo).

    Para tests que exercitam save_full_config/save_params: neutraliza
    _assert_authorized_writer (o whitelist tem teste próprio em
    test_config_write_separation.py — aqui isolamos lock-only).
    """
    real_config = vt_config_loader.CONFIG_PATH
    real_lock = vt_config_loader.LOCK_PATH

    tmp_cfg = tmp_path / "vt_config_test.json"
    tmp_cfg.write_text(
        json.dumps({
            "symbols": ["WIN", "WDO"],
            "strategy": {},
            "wdo": {},
            "win": {},
            "_version": 1,
        }),
        encoding="utf-8",
    )
    tmp_lock = tmp_path / "vt_config_test.json.lock"

    monkeypatch.setattr(vt_config_loader, "CONFIG_PATH", tmp_cfg)
    monkeypatch.setattr(vt_config_loader, "LOCK_PATH", tmp_lock)
    monkeypatch.setattr(vt_config_loader, "_config", None)
    monkeypatch.setattr(vt_config_loader, "_mtime", 0)
    # Não enforçar whitelist nos testes deste arquivo (escopo = lock only).
    monkeypatch.setattr(vt_config_loader, "_assert_authorized_writer", lambda: None)

    yield

    # Cleanup defensivo
    try:
        tmp_lock.unlink()
    except FileNotFoundError:
        pass


def _fake_valid_cfg() -> dict:
    """Mínimo válido que passa o checksum do load_config()."""
    return {
        "symbols": ["WIN", "WDO"],
        "strategy": {},
        "wdo": {"variance_min_atr": 1.2},
        "win": {"variance_min_atr": 1.3},
        "_version": 1,
    }


# ────────────────────────────────────────────────────────────────────
# RED: testes que especificam a API nova
# ────────────────────────────────────────────────────────────────────


def test_lock_acquire_succeeds_when_free():
    """Lock livre → acquire retorna True e cria sidecar .lock."""
    assert not vt_config_loader.is_write_locked()

    acquired = vt_config_loader.acquire_write_lock(
        operator="test_writer", reason="unit_test"
    )
    assert acquired is True, "acquire_write_lock deve retornar bool True"
    try:
        assert vt_config_loader.is_write_locked()

        meta = json.loads(vt_config_loader.LOCK_PATH.read_text(encoding="utf-8"))
        assert meta["operator"] == "test_writer"
        assert meta["reason"] == "unit_test"
        assert meta["pid"] == os.getpid()
        assert "started_at" in meta
    finally:
        vt_config_loader.release_write_lock()
    assert not vt_config_loader.is_write_locked()


def test_lock_acquire_fails_when_held(monkeypatch):
    """Lock held por outro PID vivo → acquire retorna False; NÃO sobrescreve.

    Cenário real (anti-race): algum writer em OUTRO processo tem lock vivo.
    Nosso acquire deve recusar (False), e o sidecar deve continuar intacto.
    """
    # Força acquire_write_lock de PIDs diferentes: pid self ≠ pid do lock
    # pré-existente.
    monkeypatch.setattr(vt_config_loader, "_is_lock_process_alive",
                        lambda _meta: True)

    # Cria lock simulando estar "em outro PID vivo" via payload custom.
    tmp_lock = vt_config_loader.LOCK_PATH
    tmp_lock.parent.mkdir(parents=True, exist_ok=True)
    other_meta = {
        "operator": "owner",
        "reason": "first_write",
        "started_at": "2026-07-01T10:00:00",
        "started_at_ts": time.time(),  # fresco
        "pid": 999999,  # PID inexistente / "de outro processo"
    }
    tmp_lock.write_text(json.dumps(other_meta), encoding="utf-8")
    assert vt_config_loader.is_write_locked()

    acquired = vt_config_loader.acquire_write_lock(
        operator="racer", reason="concurrent_write"
    )
    assert acquired is False, "lock já held por outro pid vivo → deve retornar False"
    # Sidecar NÃO deve ter sido sobrescrito pelo racer
    meta = json.loads(tmp_lock.read_text(encoding="utf-8"))
    assert meta["operator"] == "owner", (
        "lock falho não pode sobrescrever metadata do owner"
    )
    assert meta["reason"] == "first_write"


def test_lock_release_lets_new_acquire():
    """Depois de release → novo acquire funciona normalmente."""
    assert vt_config_loader.acquire_write_lock("first", "r1") is True
    vt_config_loader.release_write_lock()
    assert not vt_config_loader.is_write_locked()

    assert vt_config_loader.acquire_write_lock("second", "r2") is True
    try:
        meta = json.loads(vt_config_loader.LOCK_PATH.read_text(encoding="utf-8"))
        assert meta["operator"] == "second"
        assert meta["reason"] == "r2"
    finally:
        vt_config_loader.release_write_lock()


def test_assert_write_unlocked_raises_when_locked(monkeypatch):
    """assert_write_unlocked deve raise RuntimeError se lock vivo de OUTRO PID."""
    # Simula lock de OUTRO processo vivo
    monkeypatch.setattr(vt_config_loader, "_is_lock_process_alive",
                        lambda _meta: True)
    other_meta = {
        "operator": "owner",
        "reason": "concurrent",
        "started_at": "2026-07-01T10:00:00",
        "started_at_ts": time.time(),
        "pid": 999999,
    }
    vt_config_loader.LOCK_PATH.write_text(json.dumps(other_meta), encoding="utf-8")

    with pytest.raises(RuntimeError, match="lock"):
        vt_config_loader.assert_write_unlocked()

    # Cleanup: remover lock para que próximo teste não pegue estado
    vt_config_loader.LOCK_PATH.unlink()
    # Livre: passa
    vt_config_loader.assert_write_unlocked()


def test_stale_lock_is_force_acquired(monkeypatch):
    """Lock com mais de 300s é tratado como stale e sobrescrito."""
    vt_config_loader.acquire_write_lock("stale_writer", "old")
    # Força timestamp velho (> 600s atrás) para garantir stale
    stale_meta = json.loads(vt_config_loader.LOCK_PATH.read_text(encoding="utf-8"))
    stale_meta["started_at"] = time.time() - 600
    # Mantém campo `started_at_ts` (epoch) usado pelo checker de staleness
    stale_meta["started_at_ts"] = time.time() - 600
    vt_config_loader.LOCK_PATH.write_text(
        json.dumps(stale_meta), encoding="utf-8"
    )

    # Novo acquire DEVE tomar o lock (stale > 300s)
    acquired = vt_config_loader.acquire_write_lock("new_owner", "fresh")
    assert acquired is True
    try:
        meta = json.loads(vt_config_loader.LOCK_PATH.read_text(encoding="utf-8"))
        assert meta["operator"] == "new_owner"
        assert meta["reason"] == "fresh"
    finally:
        vt_config_loader.release_write_lock()


def test_save_full_config_uses_lock(monkeypatch):
    """save_full_config: lock está HELD durante _atomic_write e liberado depois."""
    held_during_write = []

    def spy_atomic(cfg: dict) -> bool:
        held_during_write.append(vt_config_loader.is_write_locked())
        return True  # simula sucesso sem mexer em disco

    monkeypatch.setattr(vt_config_loader, "_atomic_write", spy_atomic)

    cfg = _fake_valid_cfg()
    assert vt_config_loader.save_full_config(
        cfg, updated_by="test_writer"
    ) is True

    assert held_during_write == [True], "lock deve estar held durante write"
    assert not vt_config_loader.is_write_locked(), "lock deve ser liberado após save"


def test_save_full_config_blocks_when_already_locked(monkeypatch):
    """save_full_config NÃO pode sobrescrever lock de outro writer (raise)."""
    monkeypatch.setattr(vt_config_loader, "_is_lock_process_alive",
                        lambda _meta: True)
    other_meta = {
        "operator": "other_writer",
        "reason": "concurrent",
        "started_at": "2026-07-01T10:00:00",
        "started_at_ts": time.time(),
        "pid": 999999,
    }
    vt_config_loader.LOCK_PATH.write_text(json.dumps(other_meta), encoding="utf-8")

    with pytest.raises(RuntimeError, match="lock"):
        vt_config_loader.save_full_config(
            _fake_valid_cfg(), updated_by="racer"
        )

    # Owner lock INTACTO
    meta = json.loads(vt_config_loader.LOCK_PATH.read_text(encoding="utf-8"))
    assert meta["operator"] == "other_writer"

    # Cleanup
    vt_config_loader.LOCK_PATH.unlink()


def test_save_full_config_releases_lock_on_exception(monkeypatch):
    """Se _atomic_write joga, lock DEVE ser liberado (try/finally)."""
    def boom(cfg: dict) -> bool:
        raise IOError("disk cheia")

    monkeypatch.setattr(vt_config_loader, "_atomic_write", boom)

    with pytest.raises(IOError, match="disk cheia"):
        vt_config_loader.save_full_config(
            _fake_valid_cfg(), updated_by="writer"
        )

    assert not vt_config_loader.is_write_locked(), (
        "lock precisa ser liberado mesmo em raise"
    )


def test_save_params_acquires_and_releases_lock(monkeypatch):
    """save_params também usa o lock (mesmo wrap)."""
    held_during_write = []

    def spy_atomic(cfg: dict) -> bool:
        held_during_write.append(vt_config_loader.is_write_locked())
        return True

    monkeypatch.setattr(vt_config_loader, "_atomic_write", spy_atomic)

    assert vt_config_loader.save_params(
        "wdo", {"variance_min_atr": 2.5}, updated_by="optimizer"
    ) is True

    assert held_during_write == [True]
    assert not vt_config_loader.is_write_locked()


def test_release_write_lock_is_idempotent():
    """release_write_lock não joga se lock já não existe."""
    # Sem lock existente — não pode raise
    vt_config_loader.release_write_lock()
    vt_config_loader.release_write_lock()
    # Após acquire + release duplicado
    vt_config_loader.acquire_write_lock("op", "r")
    vt_config_loader.release_write_lock()
    vt_config_loader.release_write_lock()
