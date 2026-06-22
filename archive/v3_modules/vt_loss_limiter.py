#!/usr/bin/env python3
"""
Smart Loss Limiter — Tiered loss protection with per-symbol pauses and smart recovery.

All thresholds are configurable via vt_config.json → risk_management section.
Hot-reload: re-reads config every N seconds (configurable).

Tiered thresholds (global daily PnL):
  -R$500  → WARNING: reduce size 50%, notify Telegram
  -R$750  → HALT 2h: pause all trading, notify Telegram
  -R$1000 → KILL SWITCH: halt rest of day, notify Telegram

Per-symbol pauses:
  If a symbol loses R$300+ in a day → pause that symbol for 2h
  If a symbol loses R$500+ in a day → pause that symbol rest of day

Smart recovery (after any halt expires):
  1. First trade after recovery: 50% position size
  2. If that trade loses → double the halt duration
  3. After a winning trade at 50% size → restore full size

State persisted to /tmp/vt_loss_limiter_state.json alongside SessionState.
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path


# --- Config defaults (overridable via vt_config.json → risk_management section) ---
DEFAULT_TIERS = [
    {"threshold": -500, "action": "warn", "label": "WARNING"},
    {"threshold": -750, "action": "halt_2h", "label": "HALT 2H"},
    {"threshold": -1000, "action": "kill", "label": "KILL SWITCH"},
]
DEFAULT_SYMBOL_WARN = -300  # per-symbol warning threshold
DEFAULT_SYMBOL_HALT = -500  # per-symbol halt threshold
DEFAULT_RECOVERY_SIZE_MULT = 0.5  # 50% size on first trade after recovery
DEFAULT_HALT_2H_MINUTES = 120
DEFAULT_DOUBLE_PAUSE_MULTIPLIER = 2  # double halt on loss during recovery
DEFAULT_HOT_RELOAD_INTERVAL = 60  # seconds


def _get_rm_config(config: dict) -> dict:
    """Extract risk_management config with fallback to legacy keys."""
    return config.get("risk_management", {})


def _get_ll_config(config: dict) -> dict:
    """Extract loss_limiter config: risk_management.loss_limiter > loss_limiter > defaults."""
    rm = _get_rm_config(config)
    if "loss_limiter" in rm:
        return rm["loss_limiter"]
    # Backward compat: legacy loss_limiter key at top level
    return config.get("loss_limiter", {})


class LossLimiterState:
    """Persistent state for the loss limiter."""

    STATE_FILE = "/tmp/vt_loss_limiter_state.json"

    def __init__(self):
        self.active_tier = None  # "warn" | "halt_2h" | "kill" | None
        self.halt_until = None  # datetime when halt expires (global)
        self.symbol_halt_until = {}  # {symbol: datetime}
        self.recovery_mode = False  # True = next trade at reduced size
        self.recovery_symbol = None  # which symbol is in recovery
        self.recovery_halt_count = 0  # how many times halt doubled
        self.last_tier_triggered = None  # last tier that fired (for dedup)
        self.notified_tiers = set()  # tiers already notified today
        self.current_day = None  # for daily reset

    def to_dict(self):
        return {
            "active_tier": self.active_tier,
            "halt_until": self.halt_until.isoformat() if self.halt_until else None,
            "symbol_halt_until": {k: v.isoformat() for k, v in self.symbol_halt_until.items()},
            "recovery_mode": self.recovery_mode,
            "recovery_symbol": self.recovery_symbol,
            "recovery_halt_count": self.recovery_halt_count,
            "last_tier_triggered": self.last_tier_triggered,
            "notified_tiers": list(self.notified_tiers),
            "current_day": str(self.current_day) if self.current_day else None,
        }

    def save(self):
        tmp = self.STATE_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.to_dict(), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp, self.STATE_FILE)
        except Exception as e:
            print(f"[LOSS_LIMITER] Erro ao salvar: {e}", flush=True)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def load(self):
        try:
            with open(self.STATE_FILE) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        saved_day = data.get("current_day")
        today = str(datetime.now().date())
        if saved_day != today:
            print(f"[LOSS_LIMITER] State de {saved_day}, hoje {today} — resetando", flush=True)
            return

        self.active_tier = data.get("active_tier")
        self.recovery_mode = data.get("recovery_mode", False)
        self.recovery_symbol = data.get("recovery_symbol")
        self.recovery_halt_count = data.get("recovery_halt_count", 0)
        self.last_tier_triggered = data.get("last_tier_triggered")
        self.notified_tiers = set(data.get("notified_tiers", []))
        self.current_day = datetime.strptime(saved_day, "%Y-%m-%d").date() if saved_day else None

        raw_halt = data.get("halt_until")
        if raw_halt:
            try:
                self.halt_until = datetime.fromisoformat(raw_halt)
            except (ValueError, TypeError):
                pass

        raw_sym = data.get("symbol_halt_until", {})
        self.symbol_halt_until = {}
        for k, v in raw_sym.items():
            try:
                self.symbol_halt_until[k] = datetime.fromisoformat(v)
            except (ValueError, TypeError):
                pass

        print(f"[LOSS_LIMITER] Restaurado: tier={self.active_tier}, recovery={self.recovery_mode}", flush=True)


class SmartLossLimiter:
    """
    Integrates with vt_autotrader SessionState to enforce tiered loss limits.
    Call check_before_trade() before opening any position.
    Call record_trade_result() after closing any position.

    All thresholds are read from vt_config.json → risk_management section.
    Supports hot-reload: call reload_config() periodically to pick up changes.
    """

    def __init__(self, config: dict):
        self.config = config
        self.state = LossLimiterState()
        self.state.load()
        self._last_config_reload = time.time()

        # Load thresholds from config
        self._load_thresholds()

    def _load_thresholds(self):
        """Load all thresholds from config (risk_management.loss_limiter)."""
        ll_config = _get_ll_config(self.config)
        rm_config = _get_rm_config(self.config)

        # Tiers (warning/halt/kill thresholds)
        self.tiers = ll_config.get("tiers", DEFAULT_TIERS)
        # If no tiers defined but individual thresholds exist, build tiers dynamically
        if not ll_config.get("tiers") and "warning_threshold" in ll_config:
            self.tiers = [
                {"threshold": ll_config.get("warning_threshold", -500), "action": "warn", "label": "WARNING"},
                {"threshold": ll_config.get("halt_threshold", -750), "action": "halt_2h", "label": "HALT 2H"},
                {"threshold": ll_config.get("kill_threshold", -1000), "action": "kill", "label": "KILL SWITCH"},
            ]

        # Per-symbol thresholds
        self.symbol_warn = ll_config.get("symbol_warn_threshold", DEFAULT_SYMBOL_WARN)
        self.symbol_halt = ll_config.get("symbol_halt_threshold", DEFAULT_SYMBOL_HALT)

        # Recovery
        smart_recovery = rm_config.get("smart_recovery", {})
        self.recovery_size_mult = smart_recovery.get(
            "first_trade_size_factor", ll_config.get("recovery_size_mult", DEFAULT_RECOVERY_SIZE_MULT)
        )

        # Halt duration
        self.halt_2h_minutes = ll_config.get("halt_duration_minutes", DEFAULT_HALT_2H_MINUTES)

        # Double pause multiplier
        self.double_pause_mult = (
            smart_recovery.get(
                "double_pause_multiplier", ll_config.get("double_pause_multiplier", DEFAULT_DOUBLE_PAUSE_MULTIPLIER)
            )
            if smart_recovery.get("double_pause_on_loss", True)
            else 1
        )

        # Hot-reload interval
        self.hot_reload_interval = rm_config.get("hot_reload_interval_seconds", DEFAULT_HOT_RELOAD_INTERVAL)

    def _maybe_reload_config(self):
        """Re-read config from disk if hot-reload interval has elapsed."""
        now = time.time()
        if now - self._last_config_reload >= self.hot_reload_interval:
            try:
                from vt_config_loader import load_config

                new_config = load_config()
                if new_config:
                    self.config = new_config
                    self._load_thresholds()
                    self._last_config_reload = now
            except Exception as e:
                print(f"[LOSS_LIMITER] Hot-reload erro: {e}", flush=True)

    def _now(self):
        return datetime.now()

    def _reset_daily(self):
        """Reset state if day changed."""
        today = self._now().date()
        if self.state.current_day != today:
            self.state = LossLimiterState()
            self.state.current_day = today
            self.state.save()
            print(f"[LOSS_LIMITER] Reset diário para {today}", flush=True)

    def _notify(self, msg: str):
        """Send Telegram notification (best effort)."""
        try:
            from vt_hermes_helper import hermes_send

            hermes_send("telegram:-1004284773048", msg)
        except Exception:
            pass

    def _eval_tiers(self, daily_pnl: float):
        """Evaluate tiered thresholds and trigger actions."""
        self._reset_daily()
        now = self._now()

        # Sort tiers by threshold (most negative first = most severe)
        sorted_tiers = sorted(self.tiers, key=lambda t: t["threshold"])

        triggered = None
        for tier in sorted_tiers:
            if daily_pnl <= tier["threshold"]:
                triggered = tier
                break  # Most severe tier first, stop at first match

        if triggered is None:
            # PnL recovered above all tiers — clear tier state
            if self.state.active_tier:
                print(f"[LOSS_LIMITER] PnL R${daily_pnl:.2f} recuperou — limpando tier", flush=True)
                self.state.active_tier = None
                self.state.last_tier_triggered = None
                self.state.save()
            return

        tier_label = triggered["label"]
        tier_action = triggered["action"]
        tier_key = f"{tier_label}_{triggered['threshold']}"

        # Dedup: don't re-trigger same tier
        if self.state.last_tier_triggered == tier_key:
            return

        self.state.active_tier = tier_action
        self.state.last_tier_triggered = tier_key

        # Notify if not already notified this tier today
        if tier_key not in self.state.notified_tiers:
            msg = (
                f"🚨 LOSS LIMITER: {tier_label}\nPnL diário: R$ {daily_pnl:.2f}\nThreshold: R$ {triggered['threshold']}"
            )
            if tier_action == "warn":
                msg += f"\n⚠️ Reduzindo tamanho {self.recovery_size_mult * 100:.0f}%"
            elif tier_action == "halt_2h":
                msg += f"\n🛑 Trading pausado por {self.halt_2h_minutes}min"
            elif tier_action == "kill":
                msg += "\n💀 KILL SWITCH — Trading encerrado hoje"

            self._notify(msg)
            self.state.notified_tiers.add(tier_key)

        # Execute action
        if tier_action == "halt_2h" and not self.state.halt_until:
            self.state.halt_until = now + timedelta(minutes=self.halt_2h_minutes)
            print(f"[LOSS_LIMITER] HALT 2H ativado até {self.state.halt_until}", flush=True)
        elif tier_action == "kill":
            # Kill switch: halt until end of day (23:59)
            end_of_day = now.replace(hour=23, minute=59, second=59)
            self.state.halt_until = end_of_day
            print(f"[LOSS_LIMITER] KILL SWITCH ativado — halt até {end_of_day}", flush=True)

        self.state.save()

    def _check_symbol_halt(self, symbol: str, symbol_pnl: float) -> bool:
        """Check per-symbol halt. Returns True if symbol is halted."""
        now = self._now()

        # Check existing halt
        sym_halt = self.state.symbol_halt_until.get(symbol)
        if sym_halt and now < sym_halt:
            return True
        elif sym_halt and now >= sym_halt:
            # Halt expired
            del self.state.symbol_halt_until[symbol]
            self.state.save()

        # Evaluate per-symbol thresholds
        if symbol_pnl <= self.symbol_halt:
            self.state.symbol_halt_until[symbol] = now + timedelta(minutes=self.halt_2h_minutes)
            msg = (
                f"🛑 SYMBOL HALT: {symbol}\n"
                f"PnL do símbolo: R$ {symbol_pnl:.2f}\n"
                f"Threshold: R$ {self.symbol_halt}\n"
                f"Pausado por {self.halt_2h_minutes}min"
            )
            self._notify(msg)
            self.state.save()
            print(f"[LOSS_LIMITER] {symbol} HALT — PnL R${symbol_pnl:.2f}", flush=True)
            return True
        elif symbol_pnl <= self.symbol_warn:
            # Warning only, don't halt
            print(f"[LOSS_LIMITER] {symbol} WARNING — PnL R${symbol_pnl:.2f}", flush=True)

        return False

    def check_before_trade(self, symbol: str, daily_pnl: float, symbol_pnl: float) -> dict:
        """
        Call BEFORE opening any trade.

        Returns dict:
            {"allowed": bool, "size_mult": float, "reason": str}

        size_mult: 1.0 = full size, 0.5 = half size (recovery mode)
        """
        self._reset_daily()
        self._maybe_reload_config()
        now = self._now()

        # Evaluate global tiers
        self._eval_tiers(daily_pnl)

        # Check global halt
        if self.state.halt_until and now < self.state.halt_until:
            remaining = (self.state.halt_until - now).total_seconds() / 60
            return {
                "allowed": False,
                "size_mult": 0.0,
                "reason": f"GLOBAL HALT ativo — {remaining:.0f}min restantes (tier: {self.state.active_tier})",
            }

        # Check per-symbol halt
        if self._check_symbol_halt(symbol, symbol_pnl):
            sym_halt = self.state.symbol_halt_until.get(symbol)
            remaining = (sym_halt - now).total_seconds() / 60 if sym_halt else 0
            return {
                "allowed": False,
                "size_mult": 0.0,
                "reason": f"SYMBOL HALT: {symbol} pausado — {remaining:.0f}min restantes",
            }

        # Check if in recovery mode (first trade after halt)
        if self.state.recovery_mode:
            print(
                f"[LOSS_LIMITER] RECOVERY MODE — tamanho {self.recovery_size_mult * 100:.0f}%",
                flush=True,
            )
            return {
                "allowed": True,
                "size_mult": self.recovery_size_mult,
                "reason": f"RECOVERY: tamanho {self.recovery_size_mult * 100:.0f}% (primeiro trade pós-halt)",
            }

        # Check warning tier (reduce size)
        if self.state.active_tier == "warn":
            return {
                "allowed": True,
                "size_mult": self.recovery_size_mult,
                "reason": f"WARNING: tamanho reduzido {self.recovery_size_mult * 100:.0f}% (PnL diário negativo)",
            }

        return {"allowed": True, "size_mult": 1.0, "reason": "OK"}

    def record_trade_result(self, symbol: str, pnl: float, daily_pnl: float):
        """
        Call AFTER closing any trade.

        Handles:
        - Recovery mode exit (win at 50% → full size)
        - Recovery mode loss → double halt
        - State persistence
        """
        self._reset_daily()
        self._maybe_reload_config()

        # If in recovery mode and this trade just closed
        if self.state.recovery_mode and self.state.recovery_symbol == symbol:
            if pnl >= 0:
                # Win during recovery → exit recovery mode, restore full size
                print(
                    f"[LOSS_LIMITER] RECOVERY: {symbol} ganhou R${pnl:.2f} — saindo do recovery mode",
                    flush=True,
                )
                self._notify(f"✅ RECOVERY: {symbol} ganhou R${pnl:.2f} — tamanho normal restaurado")
                self.state.recovery_mode = False
                self.state.recovery_symbol = None
                self.state.recovery_halt_count = 0
            else:
                # Loss during recovery → double the halt
                self.state.recovery_halt_count += 1
                base_minutes = self.halt_2h_minutes
                double_factor = self.double_pause_mult**self.state.recovery_halt_count
                halt_minutes = base_minutes * double_factor
                self.state.halt_until = self._now() + timedelta(minutes=halt_minutes)
                self.state.recovery_mode = False
                self.state.recovery_symbol = None
                print(
                    f"[LOSS_LIMITER] RECOVERY LOSS: {symbol} perdeu R${pnl:.2f} — "
                    f"halt dobrado para {halt_minutes}min (count={self.state.recovery_halt_count})",
                    flush=True,
                )
                self._notify(
                    f"❌ RECOVERY LOSS: {symbol} perdeu R${pnl:.2f}\n"
                    f"Halt dobrado: {halt_minutes}min (base={base_minutes}×{double_factor})"
                )

            self.state.save()

        # After any global halt expires, enter recovery mode for next trade
        if self.state.halt_until and self._now() >= self.state.halt_until:
            # Halt just expired — set recovery for next trade
            self.state.halt_until = None
            self.state.recovery_mode = True
            self.state.recovery_symbol = None  # any symbol can be the recovery trade
            self.state.save()
            print("[LOSS_LIMITER] Halt expirou — recovery mode ativado para próximo trade", flush=True)

    def get_size_multiplier(self, symbol: str, daily_pnl: float, symbol_pnl: float) -> float:
        """
        Get the position size multiplier for a given symbol.
        Returns 1.0 (full), 0.5 (reduced), or 0.0 (blocked).
        """
        result = self.check_before_trade(symbol, daily_pnl, symbol_pnl)
        return result["size_mult"]

    def is_halted(self, symbol: str = None) -> tuple[bool, str]:
        """
        Check if trading is halted (global or per-symbol).
        Returns (is_halted, reason).
        """
        now = self._now()
        self._reset_daily()
        self._maybe_reload_config()

        if self.state.halt_until and now < self.state.halt_until:
            remaining = (self.state.halt_until - now).total_seconds() / 60
            return True, f"Global halt: {remaining:.0f}min restantes (tier={self.state.active_tier})"

        if symbol:
            sym_halt = self.state.symbol_halt_until.get(symbol)
            if sym_halt and now < sym_halt:
                remaining = (sym_halt - now).total_seconds() / 60
                return True, f"{symbol} halt: {remaining:.0f}min restantes"

        return False, "OK"

    def get_status(self) -> dict:
        """Get current limiter status for reporting."""
        self._reset_daily()
        now = self._now()
        return {
            "active_tier": self.state.active_tier,
            "global_halt": (
                (self.state.halt_until - now).total_seconds() / 60
                if self.state.halt_until and now < self.state.halt_until
                else 0
            ),
            "symbol_halts": {
                k: (v - now).total_seconds() / 60 for k, v in self.state.symbol_halt_until.items() if now < v
            },
            "recovery_mode": self.state.recovery_mode,
            "recovery_symbol": self.state.recovery_symbol,
            "recovery_halt_count": self.state.recovery_halt_count,
        }

    def get_config_summary(self) -> dict:
        """Return current active config values for reporting."""
        return {
            "tiers": self.tiers,
            "symbol_warn": self.symbol_warn,
            "symbol_halt": self.symbol_halt,
            "recovery_size_mult": self.recovery_size_mult,
            "halt_2h_minutes": self.halt_2h_minutes,
            "double_pause_mult": self.double_pause_mult,
            "hot_reload_interval": self.hot_reload_interval,
        }


# --- Convenience singleton ---
_limiter = None


def get_limiter(config: dict = None) -> SmartLossLimiter:
    """Get or create the singleton limiter instance."""
    global _limiter
    if _limiter is None:
        if config is None:
            from vt_config_loader import load_config

            config = load_config()
        _limiter = SmartLossLimiter(config)
    return _limiter


def reload_limiter(config: dict):
    """Force reload limiter with new config (for hot-reload)."""
    global _limiter
    _limiter = SmartLossLimiter(config)
    return _limiter
