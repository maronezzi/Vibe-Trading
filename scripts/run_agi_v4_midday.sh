#!/bin/bash
# run_agi_v4_midday.sh — Otimização meio-dia com proteção de CPU
# Roda com nice 19 + ionice idle + VT_MAX_WORKERS=2
# para não saturar o i5-10210U durante pregão ativo.

export VT_MAX_WORKERS=2
export PYTHONPATH="/home/bruno/Projects/Vibe-Trading:${PYTHONPATH}"

cd /home/bruno/Projects/Vibe-Trading

exec nice -n 19 ionice -c 3 \
    python3 optimization/agi_v4/runner.py --days 7 --max-iterations 3 \
    2>>/tmp/vt_agi_v4_midday.log
