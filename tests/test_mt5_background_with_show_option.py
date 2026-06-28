"""
test_mt5_background_with_show_option.py
=========================================
TDD: garante que MT5 pode ser iniciado em background (Xvfb invisível)
mas pode ser visualizado sob demanda (x11vnc + vncviewer/x2go).

PROBLEMA 2026-06-26 (Bruno):
  MT5 roda em Xvfb :99 (invisível). Bruno quer:
  1) MT5 em background SEMPRE (não abrir janela no desktop dele)
  2) Poder abrir MT5 visualmente quando quiser (botão "ver MT5")

FIX Wave 10 (2026-06-26, Bruno):
  - start_mt5linux.sh --background (default, invisível)
  - scripts/mt5_show.sh inicia x11vnc + mostra viewer
  - vt_pre_flight.py auto-inicia MT5 se não estiver rodando
"""
import os
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"


class TestMt5BackgroundWithShowOption(unittest.TestCase):
    """MT5 roda em background mas pode ser visualizado sob demanda."""

    SCRIPT_PATH = Path(PROJECT_ROOT) / "scripts" / "start_mt5linux.sh"
    MT5_SHOW_PATH = Path(PROJECT_ROOT) / "scripts" / "mt5_show.sh"

    def test_start_mt5linux_supports_background_mode(self):
        """start_mt5linux.sh --background deve iniciar MT5 invisível (Xvfb)."""
        src = self.SCRIPT_PATH.read_text()
        # Deve aceitar flag --background
        self.assertIn(
            "--background",
            src,
            "start_mt5linux.sh deve aceitar flag --background (default)"
        )
        # Deve usar Xvfb (já faz isso)
        self.assertIn("Xvfb", src, "Deve usar Xvfb display :99")

    def test_mt5_show_script_exists(self):
        """scripts/mt5_show.sh deve existir (visualizador on-demand)."""
        self.assertTrue(
            self.MT5_SHOW_PATH.exists(),
            f"scripts/mt5_show.sh deve existir (criado Wave 10)"
        )

    def test_mt5_show_starts_vnc_server(self):
        """mt5_show.sh deve iniciar x11vnc para expor display :99."""
        if not self.MT5_SHOW_PATH.exists():
            self.skipTest("mt5_show.sh não existe ainda")
        src = self.MT5_SHOW_PATH.read_text()
        self.assertIn(
            "x11vnc",
            src,
            "mt5_show.sh deve usar x11vnc para expor display :99"
        )

    def test_pre_flight_can_start_mt5_if_not_running(self):
        """vt_pre_flight.py deve poder iniciar MT5 se não estiver rodando."""
        pf_path = Path(PROJECT_ROOT) / "monitoring" / "vt_pre_flight.py"
        src = pf_path.read_text()
        # Deve chamar start_mt5linux.sh se MT5 não conectado
        # (texto pode estar em qualquer ponto do arquivo)
        self.assertTrue(
            "start_mt5linux" in src or "start_mt5" in src,
            "vt_pre_flight.py deve poder iniciar MT5 (start_mt5linux.sh)"
        )


if __name__ == "__main__":
    unittest.main()
