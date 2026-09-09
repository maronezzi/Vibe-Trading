#!/bin/bash
# =====================================================================
# Vibe-Trading — Fase C: validação READ-ONLY da VPS
# Roda NA MÁQUINA LOCAL e inspeciona a VPS via SSH. Nunca envia ordem,
# nunca inicia trading, nunca escreve na VPS (exceto leituras do SO).
#
# Uso: bash 30_validate_vps.sh [--hermes-test]
#   --hermes-test  envia UMA mensagem de teste pelo hermes DA VPS
#                  (só use após o cutover ou com o gateway local parado!)
# =====================================================================
set -uo pipefail
cd "$(dirname "$0")"
. ./vps.conf

HERMES_TEST=0
[[ "${1:-}" == "--hermes-test" ]] && HERMES_TEST=1

FAIL=0
ok()   { echo "  ✅ $1"; }
bad()  { echo "  ❌ $1"; FAIL=1; }
warn() { echo "  ⚠️  $1"; }

section() { echo; echo "── $1 ──────────────────────────────"; }

section "1. Conectividade SSH"
if HOSTINFO=$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$VT_VPS_SSH" 'echo OK; hostname; uname -r' 2>&1); then
    echo "$HOSTINFO" | sed 's/^/  /'
else
    bad "ssh $VT_VPS_SSH falhou: $HOSTINFO"; echo; exit 1
fi

section "2. Recursos (8 GB RAM + 16 GB swap esperados)"
ssh "$VT_VPS_SSH" 'free -h; echo; df -h / | tail -1'

section "3. Wine (i386+x64) e Python do prefixo"
if ssh "$VT_VPS_SSH" 'wine --version' 2>/dev/null; then
    ok "wine instalado"
else
    bad "wine ausente — rode 10_provision_vps.sh"
fi
if ssh "$VT_VPS_SSH" 'test -x ~/.wine/drive_c/Python311/python.exe && echo binario-ok' 2>/dev/null | grep -q ok; then
    ok "~/.wine/drive_c/Python311/python.exe presente"
else
    warn "Python311 do prefixo ~/.wine ainda não sincronizado (rode 20_sync --wine)"
fi
ssh "$VT_VPS_SSH" 'test -d ~/.wine64/drive_c && echo wine64-ok' 2>/dev/null | grep -q wine64 \
    && ok "~/.wine64 presente" || warn "~/.wine64 ausente (bridge MT5 precisa dele)"

section "4. MT5 terminal + bridge"
ssh "$VT_VPS_SSH" 'test -d "$HOME/.wine/drive_c/Program Files/MetaTrader 5 Terminal" && echo mt5-ok' 2>/dev/null | grep -q mt5-ok \
    && ok "MetaTrader 5 Terminal sincronizado" || warn "MT5 ainda não sincronizado (20_sync --wine)"
if ssh "$VT_VPS_SSH" 'ss -ltn 2>/dev/null | grep -q ":5001"'; then
    ok "RPyC 5001 (bridge MT5) escutando — confirme que é 127.0.0.1, não 0.0.0.0"
else
    warn "porta 5001 inativa (normal pré-cutover; o bridge sobe com start_mt5linux.sh)"
fi

section "5. Integridade do DB (vt_trades.db)"
ssh "$VT_VPS_SSH" "sqlite3 \"file:$VT_PROJECT/vt_trades.db?mode=ro\" 'PRAGMA quick_check;' 2>/dev/null" \
    | grep -q '^ok$' && ok "sqlite quick_check: ok" || warn "DB ausente ou íntegro-check falhou (normal antes do 1º sync com DB)"

section "6. Trading na VPS (pré-cutover: deve ser zero; pós-cutover: daemon é esperado)"
if ssh "$VT_VPS_SSH" 'pgrep -af "vt_autotrader.py|vt_trade_event_watcher|forward_walker" | grep -v pgrep' 2>/dev/null | grep -q .; then
    warn "há processos de trading na VPS — correto APÓS o cutover; ANTES dele indica vazamento"
else
    ok "nenhum processo de trading na VPS (estado pré-cutover)"
fi

section "7. Hermes na VPS (binário + config; SEM gateway local duplicado)"
if ssh "$VT_VPS_SSH" 'test -x ~/.local/bin/hermes && echo hermes-bin' 2>/dev/null | grep -q hermes-bin; then
    ok "~/.local/bin/hermes presente"
else
    warn "wrapper hermes ausente (21_sync + build do venv)"
fi
if ssh "$VT_VPS_SSH" 'test -f ~/.hermes/config.yaml && echo cfg' 2>/dev/null | grep -q cfg; then
    ok "~/.hermes/config.yaml presente"
else
    warn "config do hermes ausente (21_sync_hermes_to_vps.sh)"
fi
ssh "$VT_VPS_SSH" 'pgrep -f "hermes_cli.main gateway run" >/dev/null && echo gw-running' 2>/dev/null | grep -q gw-running \
    && warn "gateway hermes rodando na VPS — correto PÓS-cutover; ANTES dele = duplicidade de Telegram" \
    || ok "gateway hermes parado na VPS (correto pré-cutover)"

section "8. Latência até o servidor MT5 da corretora"
if [[ -n "${VT_BROKER_HOST}" ]]; then
    echo "  alvo: ${VT_BROKER_HOST}"
    LOCAL_MS=$(ping -c 5 -W 2 "$VT_BROKER_HOST" 2>/dev/null | tail -1 | sed 's/.*=\([^/]*\)\/\([^/]*\).*/\2/')
    VPS_MS=$(ssh "$VT_VPS_SSH" "ping -c 5 -W 2 $VT_BROKER_HOST 2>/dev/null | tail -1" | sed 's/.*=\([^/]*\)\/\([^/]*\).*/\2/')
    echo "  local: ${LOCAL_MS:-n/d} ms | VPS: ${VPS_MS:-n/d} ms"
    [[ -n "$VPS_MS" ]] && (( $(echo "$VPS_MS < 30" | bc -l 2>/dev/null || echo 0) )) && ok "VPS < 30 ms" || warn "latência da VPS alta ou não medida — investigue antes do cutover"
else
    warn "VT_BROKER_HOST vazio em vps.conf — preencha o host da corretora para medir"
fi

section "9. Teste de envio Telegram (opcional)"
if [[ $HERMES_TEST -eq 1 ]]; then
    if ssh "$VT_VPS_SSH" '~/.local/bin/hermes send -t "telegram:-1004284773048" "🧪 [VPS] teste do kit de migração Vibe-Trading"' 2>&1 | tail -2; then
        ok "mensagem de teste disparada (confira no Telegram)"
    else
        bad "hermes send falhou na VPS — ver ~/.hermes/.env e python-telegram-bot no venv"
    fi
else
    echo "  (pulei — use --hermes-test; só com o gateway local parado)"
fi

echo
[[ $FAIL -eq 0 ]] && echo "== VALIDAÇÃO: PASS ==" || echo "== VALIDAÇÃO: FALHOU (veja ❌ acima) =="
exit $FAIL
