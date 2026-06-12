#!/bin/bash
cd /home/bruno/Projects/Vibe-Trading
PYTHONUNBUFFERED=1 exec python3 vt_autotrader.py >> /tmp/vt_autotrader.log 2>&1
