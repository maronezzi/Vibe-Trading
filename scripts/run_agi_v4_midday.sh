#!/bin/bash
# run_agi_v4_midday.sh — Otimização meio-dia com proteção de CPU
# Roda com nice 19 + ionice idle + VT_MAX_WORKERS=2
# para não saturar o i5-10210U durante pregão ativo.
#
# Wave anti-colisão (30/07): alinhado com o wrapper canônico — usa .venv/bin
# (numpy/backtest disponíveis) e SEM --max-iterations (loop roda completo com
# freios naturais: convergência/estagnação/deadline 90min). O lock anti-
# paralelismo agora vive no runner.py (fcntl.flock), então este script não
# colide com o cron 12:00/17:10 mesmo chamando direto — a 2ª run sai graciosamente.

export VT_MAX_WORKERS=2
export PYTHONPATH="/home/bruno/Projects/Vibe-Trading:${PYTHONPATH}"

cd /home/bruno/Projects/Vibe-Trading

exec nice -n 19 ionice -c 3 \
    .venv/bin/python3 optimization/agi_v4/runner.py --days 7 \
    2>>/tmp/vt_agi_v4_midday.log
