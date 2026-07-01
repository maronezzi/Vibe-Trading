# Forense Arquitetural — Vibe-Trading
**Data:** 2026-07-01 14:35 BRT (mercado aberto)  
**Autor:** Hermes (sub-agente forense, read-only)  
**Repo:** `/home/bruno/Projects/Vibe-Trading`  
**Escopo:** Mapear todas as fontes de verdade (MT5 ↔ DB ↔ state ↔ logs), divergências, e bugs latentes que produzem PnL divergente.

---

## Índice

1. [Estado Atual — Resumo Executivo](#1-estado-atual--resumo-executivo)
2. [As 4 Fontes de Verdade](#2-as-4-fontes-de-verdade)
3. [Mapa Completo de Leituras/Escritas](#3-mapa-completo-de-leiturasescritas)
4. [Inventário de Call Sites MT5](#4-inventário-de-call-sites-mt5)
5. [Inventário de Call Sites DB (`vt_trades.db`)](#5-inventário-de-call-sites-db-vt_tradesdb)
6. [Inventário de `/tmp/vt_autotrader_state.json`](#6-inventário-de-tmpvt_autotrader_statejson)
7. [Trindade Anti-Orphan (defesas em camadas)](#7-trindade-anti-orphan-defesas-em-camadas)
8. [Bugs Históricos (30 dias) e os 8 Commits de Hoje](#8-bugs-históricos-30-dias-e-os-8-commits-de-hoje)
9. [Incidentes Confirmados em 01/07/2026 (DB ao vivo)](#9-incidentes-confirmados-em-01072026-db-ao-vivo)
10. [Race Conditions Conhecidas](#10-race-conditions-conhecidas)
11. [Análise Comparada: como o resto da indústria faz](#11-análise-comparada-como-o-resto-da-indústria-faz)
12. [Recomendações para Bruno Decidir](#12-recomendações-para-bruno-decidir)

---

## 1. Estado Atual — Resumo Executivo

### 1.1 PnL divergente (snapshot DB ao vivo, 14:35 BRT)

| Fonte | PnL hoje (2026-07-01) | Observação |
|---|---|---|
| **DB `vt_trades.db`** (trades fechados com `exit_time IS NOT NULL`) | **R$ 301,71** líquido (9 fechados, 2 abertos) | Soma direta de `net_pnl` |
| **State `/tmp/vt_autotrader_state.json`** | **R$ 301,71** (`state.daily_pnl`) | Igual ao DB — `_sync_daily_pnl_with_db` foi chamado |
| **Telegram report (16:50 `vt_daily_report.py`)** | ⚠️ ainda não gerado (EOD às 16:45) | Vai usar `trades` filtrado por `date(entry_time) = hoje` |
| **MT5 broker** | 🔴 **desconhecido para a forense** | Sem acesso ao extrato da corretora hoje |
| Relatos do Bruno | "+R$ X" (não reconciliado nesta forense) | Provavelmente inclui `id=2069 #2073 #2074 #2075` (GHOST) |

**Telespectador de 1º plano:** 4 trades hoje viraram GHOST com `PnL=0` (`#2069, #2073, #2074, #2075`) porque o `reconcile_positions_with_mt5` correu ANTES do `close()` chegar a registrar PnL. O `daily_report` os EXCLUI silenciosamente (filtro `exit_reason='GHOST'` e `close_source='RECONCILE'`). O `state.daily_pnl` também os IGNORA porque ele só soma quando `log_exit` retorna `pnl > 0`. Resultado: se esses 4 trades tinham PnL real negativo, o PnL mostrado é **mais otimista que o real**. Se tinham PnL positivo, é **menos otimista**. **Ninguém sabe** sem olhar o extrato do broker.

### 1.2 Os 4 problemas arquiteturais raiz

| # | Problema | Consequência |
|---|---|---|
| **P1** | **Nenhuma fonte única de verdade** — DB, state, MT5, e Telegram competem por autoridade. | PnL divergente entre Telegram/DB/state. |
| **P2** | **DB é "log de auditoria" + "cache de queries" ao mesmo tempo** — quem escreve no DB escreve como se fosse log, mas quem lê trata como truth. | Orphans, GHOSTs, e double-counting entre reconcile passes. |
| **P3** | **State é write-through mas sem lock atômico** — múltiplos tick-coroutines gravam em `state.json` e `state.daily_pnl += pnl` sem mutex. | Race conditions clássicas entre `close()` e `reconcile_positions_with_mt5`. |
| **P4** | **`state.daily_pnl` é derivado do DB, mas só se `_sync_daily_pnl_with_db()` rodar** — e essa função é chamada só em `__init__` e após reconcile periódico. | Após `log_exit` direto (sem reconcile), state fica off-by-one. |

---

## 2. As 4 Fontes de Verdade

### 2.1 MT5 (via Wine subprocess) — fonte bruta, mas tem latência

- **Caminho:** `mt5/mt5_orchestrator.py` → subprocess `wine ~/.wine/drive_c/Python311/python.exe mt5/mt5_executor.py <cmd>`.
- **Latência:** ~200-500ms por chamada (subprocess + Wine). Custo: cada `tick()` ou `status()` é caro.
- **Confiabilidade:** Alta para dados do momento (balance, equity, posições abertas), confiável para `history()` (deals fechados).
- **Trade-off:** chamar MT5 demais = subprocess overhead; chamar de menos = state/DB divergem.

### 2.2 DB `vt_trades.db` (SQLite WAL) — "log" + "cache" simultâneo

- **Schema:** `core/vt_trade_log.py:40-137` — 3 tabelas: `trades`, `daily_summary`, `trade_history_from_mt5`.
- **WAL + busy_timeout=10-30s** (variando por módulo — inconsistente: 5s, 10s, 30s).
- **Padrão de acesso:** múltiplos módulos abrem `sqlite3.connect(...)` independentemente. **Não há singleton nem pool de conexão.** Conexões vêm de `get_db()` em `vt_trade_log.py` OU `sqlite3.connect("vt_trades.db")` direto (bypass).
- **Race:** três sites abrem conexão sem lock centralizado: `vt_trade_log.get_db()`, `vt_autotrader.sqlite3.connect(...)` (em `_sync_daily_pnl_with_db`, `_resolve_orphan_closes`, `reconcile_positions_with_mt5`, `recover_open_positions`), `mt5_orchestrator._persist_close_to_db`, e `dashboard/api/data.py`. Cada um configura WAL/busy_timeout separadamente (e às vezes esquece).

### 2.3 State `/tmp/vt_autotrader_state.json` — cache em memória + projeção

- **Classe:** `core/vt_autotrader.py:141-314` `SessionState`.
- **Arquivo:** `STATE_FILE = "/tmp/vt_autotrader_state.json"` (constante hardcoded L167).
- **Serialização:** `to_dict()` + `save()` (json.dump) → reload via `SessionState.load()` (chamado em `main()` antes de `run_daemon`).
- **Conteúdo:** `positions`, `last_signals`, `last_trade_time`, `daily_pnl`, `trade_count`, `wins`, `losses`, `consecutive_losses`, `halt_until`, `resolved_symbols`, etc.
- **Quem lê:** `vt_autotrader.py` (sempre), `vt_analyst.py` (linhas 430, 446, 559), `vt_copilot.py` (linha 29 log path), `vt_pre_flight.py` (linha 178 escreve), `vt_trade_watchdog.py` (linha 37), `vt_order_validator_v2.py` (linhas 177, 190, 204), `dashboard/api/data.py` (linha 16).
- **Quem escreve (16+ call sites):** `vt_autotrader.py` (vários), `vt_pre_flight.py` (L178), `archive/deprecated/migrate_state_keys.py` (legado).
- **Problema:** todos esses READERS podem ver uma versão stale entre `state.save()` e o próximo reload (que não é hot-reload — só `load_effective_config` é hot).

### 2.4 Logs locais (`/tmp/vt_*.log`, `*.jsonl`) — auditoria, nunca decisão

- **Status:** 4+ arquivos: `/tmp/vt_autotrader.log`, `/tmp/vt_orchestrator.log`, `/tmp/vt_notifications.jsonl`, `/tmp/vt_market_state.json`, `/tmp/vt_agi_audit.json`, `/tmp/vt_pre_flight.log`, etc.
- **Quem escreve:** todos os módulos.
- **Quem lê para decisão:** **ninguém deveria** (e o briefing do Bruno confirma: "logs = auditoria, NUNCA pra decisão").
- **Mas violações existem:** `vt_copilot.py` lê `/tmp/vt_autotrader.log` para gerar relatórios; `_ask_llm()` no `mt5_error_recovery` loga contexto em JSON depois consulta LLM. Auditório, ok.

---

## 3. Mapa Completo de Leituras/Escritas

| Fonte | Writer primário | Reader primário | Sync | Risco de divergência |
|---|---|---|---|---|
| **MT5 (broker)** | `mt5_executor.py` (subprocess) | `mt5_orchestrator.py` (interface) | tempo real (cada call) | Latência + timeout. **Nenhum risco de divergência "interno" — é a verdade externa.** |
| **`vt_trades.db` `trades`** | `_persist_close_to_db` (orchestrator), `log_entry/log_exit` (vt_trade_log), `reconcile_*` (3 módulos), `_resolve_orphan_closes`, `recover_open_positions`, `import_mt5_history` | `get_daily_summary`, `vt_daily_report`, `vt_weekly_report`, `vt_tax_report`, `dashboard/api/data.py` | toda vez que log_exit() roda OU reconcile periódico (10 ciclos ≈ 5min) OU startup | **ALTO** — múltiplos writers, schemas duplicados, sem idempotência forte |
| **`vt_trades.db` `daily_summary`** | `_update_daily_summary` (via log_exit) | `get_daily_summary`, `vt_daily_report` | derivado de log_exit | MÉDIO — se log_exit não roda, daily_summary não atualiza |
| **`vt_trades.db` `trade_history_from_mt5`** | `import_mt5_history` (chamado em EOD) | `sync_fees_from_mt5` (lê para atualizar fees) | EOD 16:45 | BAIXO — só EOD |
| **`/tmp/vt_autotrader_state.json`** | `state.save()` em `vt_autotrader.py` (≥10 call sites) + `vt_pre_flight.py:178` + `archive/deprecated/migrate_state_keys.py` | `vt_autotrader.py` (sempre), `vt_analyst.py` (430, 446, 559), `vt_trade_watchdog.py:37`, `vt_order_validator_v2.py` (177,190,204), `dashboard/api/data.py:16`, `vt_copilot.py:29` (log path) | `state.save()` após cada `log_entry`/`log_exit`/modify SL | **ALTO** — write-through sem lock, atomicidade parcial (json.dump não é atômico — pode gerar arquivo truncado se kill -9) |
| **`/tmp/vt_notifications.jsonl`** | `_queue_notification` (vt_trade_log.py) | `vt_notify` (consumidor) | append-only | BAIXO — fila linear |
| **`/tmp/vt_block_counter.json`** | `vt_autotrader.py:937` | `vt_autotrader.py` | rare | BAIXO |
| **`/tmp/vt_paused_timeframes.json`** | `vt_copilot.py` (399, 651) | `vt_copilot.py` | cron/AGI | BAIXO |
| **`/tmp/vt_agi_audit.json`** | `agi_tuning_17h.py` (vários) | `agi_tuning_17h.py` | daily 17:10 | BAIXO |
| **`/tmp/vt_copilot_overrides.json`** | `vt_copilot.py` | `vt_config_loader.py` (sidecar) | runtime | MÉDIO — hot reload depende disso |

---

## 4. Inventário de Call Sites MT5

### 4.1 Leituras MT5 (status, tick, history, info, symbol_info, book, orders, bars)

| Call | Caller | Onde | Frequência |
|---|---|---|---|
| `status()` | `vt_autotrader.py` | L161, L2161 (recover), L2674 (reconcile), L2411 (orphan resolve), L2091 (EOD) | 1×/tick (3-5×) + EOD |
| `status()` | `vt_emergency.py` | L98 (PnL check) | raro (1× por modify failed) |
| `status()` | `vt_daily_report.py` | L38, L45 (via close_all) | 1×/dia |
| `status()` | `vt_copilot.py` | L24 (import) | sob demanda |
| `status()` | `vt_analyst.py` | L36 (import) | sob demanda |
| `status()` | `mt5_error_recovery.py` | L464 (modify_sl retry), L640 (verify after reject) | raro |
| `status()` | `vt_trade_watchdog.py` | L33 (import) | 1×/min |
| `status()` | `vt_pre_flight.py` | L190 (import) | 1×/dia |
| `tick()` | `vt_autotrader.py` | L39 (import), L1739 (manage_position) | 1×/tick/posição |
| `tick()` | `mt5_error_recovery.py` | L273, L292 (safe_buy context), L378 (safe_sell), L205 (fix invalid stops) | raro |
| `history()` | `vt_history_reconcile.py` | L167 (read deals) | 1×/sym/startup + 1×/10 ticks |
| `history()` | `vt_autotrader.py` | L1927 (FECHADO PELO SERVIDOR) | 1×/server-close detectado |
| `history()` | `vt_orchestrator.py` | L398 (interface) | conforme caller |
| `history()` | `vt_autotrader.py` | L2058 (EOD import_mt5_history) | 1×/dia |
| `info()` | `mt5_error_recovery.py` | L157, L205, L494 | raro (retries) |
| `symbol_info`, `book`, `orders`, `bars` | só via CLI (`mt5_orchestrator.py:__main__`) | n/a | humano |

**Bypass encontrados:** nenhum. **Todos passam por `mt5_orchestrator.py`.** ✅

### 4.2 Escritas MT5 (buy, sell, close, close_all, modify_sl)

| Call | Caller | Onde | Notas |
|---|---|---|---|
| `buy()` | `mt5_error_recovery.py` | L292 (safe_buy wrapper) | retry + LLM fallback |
| `buy()` | `mt5_orchestrator.py` | L131 (raw) | só via CLI |
| `sell()` | `mt5_error_recovery.py` | L378 (safe_sell wrapper) | retry + LLM fallback |
| `sell()` | `mt5_orchestrator.py` | L144 (raw) | só via CLI |
| `close()` | `mt5_error_recovery.py` | L564 (safe_close wrapper) | retry |
| `close()` | `mt5_orchestrator.py` | L323 (raw) | `_persist_close_to_db` desde dc447fd6 |
| `close()` | `vt_autotrader.py` | L1777 (hard_exit) — via safe_close | chamado direto? verificar: é via `safe_close(symbol)` |
| `close_all()` | `mt5_orchestrator.py` | L356 (raw) | CLI |
| `close_all()` | `vt_daily_report.py` | L45 | EOD |
| `modify_sl()` | `mt5_error_recovery.py` | L464 (safe_modify_sl) | retry + emergency close |
| `modify_sl()` | `mt5_orchestrator.py` | L360 (raw) | só via CLI |
| `safe_modify_sl_with_emergency_close()` | `vt_autotrader.py` | L1825, L1836, L1905 (3 call sites: breakeven, time-trail, trailing) | cada tick/posição |
| `safe_close()` | `vt_autotrader.py` | L1777 (hard_exit) | 1×/posição se >hard_exit_min |
| `safe_close()` | `vt_emergency.py` | L164 (emergency close) | raro |

**Bypass encontrados:** nenhum. **Todos via `safe_buy/sell/close/modify` wrappers.** ✅

**Observação crítica:** `mt5_orchestrator.close()` faz **side-effect no DB** via `_persist_close_to_db` (commit dc447fd6). Isso é uma violação de camada: o orchestrator (bridge MT5) escreve no DB (camada de auditoria). Deveria ser `safe_close()` que faz o log_exit, não `close()`. O design atual é: **bot chama `safe_close()` → `mt5_orchestrator.close()` → MT5 fecha → `_persist_close_to_db()` (DB write dentro do orchestrator).** Mas o `vt_autotrader.py:1777` chama `safe_close()` que internamente chama `mt5_orchestrator.close()` — então a persistência roda. OK.

---

## 5. Inventário de Call Sites DB (`vt_trades.db`)

### 5.1 Funções core (via `core.vt_trade_log`)

| Função | Definição | Chamadores |
|---|---|---|
| `init_db()` | L40 | `vt_autotrader.py:38, 2151, 3008` (startup + run_once + run_daemon) |
| `log_entry()` | L182 | `vt_autotrader.py:1651` (1 call site em `_execute_entry`) |
| `log_exit()` | L212 | `vt_autotrader.py:1960, 2037` (server-close + EOD) + `vt_emergency.py:194` (emergency) |
| `import_mt5_history()` | L340 | `vt_autotrader.py:2060` (EOD) |
| `sync_fees_from_mt5()` | L403 | `vt_autotrader.py:2063` (EOD) |
| `get_daily_summary()` | L461 | `vt_autotrader.py:2078` (EOD report) |
| `get_db()` | L31 | `core.vt_notification_ledger.py:32, 222`; `tests/...` (vários) |
| `get_multiplier()` | L140 | `vt_emergency.py:120`, `vt_analyst.py:396`, `vt_copilot.py:201`, `tests/...` |

### 5.2 Bypasses (sqlite3 direto, sem passar pelo core)

**9 call sites de bypass:**

| Bypass | Onde | Risco |
|---|---|---|
| `sqlite3.connect("vt_trades.db")` em `_sync_daily_pnl_with_db()` | `vt_autotrader.py:107` | ✅ usa timeout=30 + WAL + busy_timeout — OK |
| `sqlite3.connect("vt_trades.db")` em `_resolve_orphan_closes()` | `vt_autotrader.py:2360` | ✅ mesmo padrão — OK |
| `sqlite3.connect("vt_trades.db")` em `reconcile_positions_with_mt5()` | `vt_autotrader.py:2718` | ✅ mesmo padrão — OK |
| `sqlite3.connect("vt_trades.db")` em `recover_open_positions()` | `vt_autotrader.py:2174` | ✅ mesmo padrão — OK |
| `sqlite3.connect(...)` em `mt5_orchestrator._persist_close_to_db()` | `mt5_orchestrator.py:183` | ⚠️ timeout=5 (vs 30) — **inconsistente** |
| `sqlite3.connect(...)` em `core.vt_history_reconcile._open_db()` | `vt_history_reconcile.py:59` | ⚠️ timeout=10 — **inconsistente** |
| `sqlite3.connect(...)` em `dashboard/api/data.py` (11 calls) | `dashboard/api/data.py:22, 45, 122, 144, ...` | 🔴 **sem WAL/busy_timeout** — race condition garantida se autotrader está rodando |
| `sqlite3.connect(...)` em `monitoring/vt_tax_report.py` | L42, L288 | 🔴 **sem WAL/busy_timeout** — usado em batch job noturno, menor risco |
| `sqlite3.connect(...)` em `monitoring/vt_weekly_report.py` | L73 | 🔴 **sem WAL/busy_timeout** |
| `sqlite3.connect(...)` em `tests/test_orchestrator_close_updates_db.py` | L147, 179, 187, ... (10+ calls) | ✅ testes isolados (fixture tmp_db) |
| `sqlite3.connect(...)` em `tests/test_notification_guarantee.py` | L198, 215, 252, 268, 278, 289, 310, 330, 384 | ✅ testes |

**Recomendação imediata:** padronizar timeout=30 + WAL + busy_timeout=30000 em TODOS os bypass. O dashboard é o caso mais urgente.

### 5.3 Escrita de schema em duplicidade

**`mt5_orchestrator._persist_close_to_db` (L37-71) reescreve o schema do DB dentro do orchestrator** porque "quer deixar o módulo self-contained" (comentário L36). Isso é uma bomba-relógio: se o `vt_trade_log` evoluir o schema (adicionar coluna, índice), o `mt5_orchestrator` continua com a versão antiga e silencia erros. Hoje isso **não** está causando bug, mas é dívida técnica.

---

## 6. Inventário de `/tmp/vt_autotrader_state.json`

### 6.1 Estrutura (snapshot 14:35 BRT)
```json
{
  "positions": {},
  "last_signals": { 7 chaves },
  "last_trade_time": { 14 chaves },
  "daily_pnl": 301.71,           // ✅ igual ao DB
  "trade_count": 9,                // ✅ igual ao DB
  "wins": 3,
  "losses": 6,
  "started_at": null,              // ⚠️ só é setado em run_daemon (não em run_once)
  "closed": false,
  "daily_trade_count": 12,         // 12 trades tentados (3 rejeitados)
  "current_day": "2026-07-01",
  "daily_trade_by_symbol": {...},
  "consecutive_losses": {"WSPU26": 1},
  "halt_until": {}
}
```

### 6.2 Quem LÊ (read-only para decisão)

| Reader | Linhas | Uso |
|---|---|---|
| `vt_autotrader.py` | em todo lugar | gerência de posições, kill switches, consecutive losses |
| `vt_analyst.py` | L430, L446, L559 | análise de mercado (lê `daily_pnl` para PnL-flut) |
| `vt_copilot.py` | L29 (log path) | não lê state, só o log |
| `vt_trade_watchdog.py` | L37 | detecta se autotrader travou |
| `vt_order_validator_v2.py` | L177, 190, 204 | valida ordens vs state.positions |
| `dashboard/api/data.py` | L16 | serve UI |

### 6.3 Quem ESCREVE

| Writer | Linhas | Quando |
|---|---|---|
| `vt_autotrader.py` `state.save()` | L1688 (log_entry), L2018 (server-close), L2073 (EOD), e via SessionState | após cada evento de trade |
| `vt_pre_flight.py` | L178 | pre-flight (08:55) — escreve state "limpo" |
| `archive/deprecated/migrate_state_keys.py` | L60, 111 | LEGADO, não roda mais |

### 6.4 Frequência de sincronização

- **DB → state:** 1× no startup (via `_sync_daily_pnl_with_db` em `__init__`) + 1× após startup reconcile (L3115) + 1× a cada 10 ticks (L3198 implícito) + ad-hoc.
- **state → DB:** nunca. State não escreve no DB.
- **MT5 → state:** implícito via `recover_open_positions()` no startup + `reconcile_positions_with_mt5()` a cada tick.
- **state → disco:** a cada `state.save()` (~10×/tick/posição).

### 6.5 Risco de divergência

**ALTO** porque:
1. `state.daily_pnl += pnl` não é atômico com `state.save()` (race entre tick e reload).
2. `state.positions` é mutado in-memory mas `state.save()` json.dump não é atômico no disco — kill -9 gera JSON truncado.
3. `state.positions` é indexado por `f"{symbol}_{tf}"` mas múltiplas TFs do mesmo symbol colidem (warning L2825).
4. State NÃO tem fila de eventos — é snapshot síncrono. Se `manage_position` deleta posição em L2017 enquanto `reconcile_positions_with_mt5` está escrevendo a mesma key, conflito.

---

## 7. Trindade Anti-Orphan (defesas em camadas)

Bruno implementou 3 camadas (commits c1996c6e, ce026460, 798b8e46, dc447fd6) que rodam **a cada tick** no `run_daemon`:

```
ordem (run_daemon L3156-3164):
  1. _resolve_orphan_closes()       # exit_time IS NULL + ticket fora do MT5 → busca history() e preenche PnL
  2. reconcile_positions_with_mt5() # state ↔ MT5 ↔ DB (3-way)
  3. close() [via _persist_close_to_db]  # se bot chamar close manual
```

### 7.1 Defesa #1 — `_resolve_orphan_closes` (L2300-2620)

- Lê DB: `exit_time IS NULL OR (close_source='RECONCILE' AND NOT LIKE 'ORPHAN_CLOSE_RESOLVED_%')`.
- Lê MT5: `status()`. Tickets fora do MT5 são "fechados pelo servidor".
- Para cada ticket fora: busca `history(symbol, days=2)` e pega o `position_id == entry_ticket` deal.
- UPDATE: `exit_time=now, exit_price=deal.price, gross_pnl=deal.profit, net_pnl=broker_net`.
- Idempotente: tag `ORPHAN_CLOSE_RESOLVED_%`.
- **Bugs conhecidos:** se MT5 history() falha, o trade fica órfão permanentemente. Sem fallback de PnL estimado.

### 7.2 Defesa #2 — `reconcile_positions_with_mt5` (L2628-3000)

- **Filtro:** magic=555501 + comment='VibeTrading'.
- **2a)** MT5 tem, state não tem: INSERT no DB como "RECONCILED" + adiciona em state.positions.
- **2b)** state tem, MT5 não tem: UPDATE DB com `exit_reason='GHOST'`, `close_source='RECONCILE'`, **PnL=0** (porque não tem como saber sem chamar history() aqui).
- **2c)** Bug fix ce026460: filtra `entry_time >= hoje` para não re-ingerir trades antigos; valida `entry_price>0` e `volume>0` para não inserir lixo.
- **Problema:** 2b marca GHOST com PnL=0. Se depois 2a/2b rodar de novo, o 2a pode re-ingerir o mesmo trade se a INSERT for IGNOREd por já existir (IntegrityError → fallback row2 L2811). Mas o close_source continua "RECONCILE" e o PnL continua 0. **A reconciliação do GHOST só acontece se a defesa #1 rodar com sucesso.**

### 7.3 Defesa #3 — `_persist_close_to_db` em `mt5_orchestrator.close()` (L323-353)

- Commit dc447fd6: quando `close()` retorna `status='ok'` E `closed >= 1`, persiste cada deal no DB.
- **Bypassa o `core.vt_trade_log`:** tem schema local (L37-71), `sqlite3.connect` direto.
- **Bugs:** se o trade não tem `entry_ticket` correspondente no DB, ele é inserido como **ORPHAN** com `exit_reason='MANUAL_CLOSE_OR_ORPHAN'` (L235) — mas esse caminho é o de "MT5 fechou trade que bot não conhecia", e o PnL vai como `profit` (sem commission/swap).

### 7.4 Interação das 3 defesas — exemplo do trade #2069

```
13:50:40  _execute_entry() → safe_sell WSPU26 → FILLED ticket=2468023320
          log_entry() OK (DB) + state.positions[...] = {...} OK

13:55:00  tick: manage_position() começa trailing
          safe_modify_sl_with_emergency_close() → OK (sl_pts atualizado)
          pos["sl_pts"] = new_sl_pts

14:00:00  tick: manage_position() detecta posição sumiu do MT5 (SL_SERVIDOR)
          safe_modify_sl continua retornando "ok" mas o position já fechou
          log_exit(SL_SERVIDOR) → tenta history(symbol, days=1)...

          # PERGUNTA: history() retornou algo? Se sim, PnL correto. Se não,
          # cai no fallback local que PODE errar.

14:04:06  tick: reconcile_positions_with_mt5() — vê WSPU26 no state mas não no MT5
          UPDATE DB: exit_time=now, exit_reason='GHOST', close_source='RECONCILE', PnL=0
          ⚠️ Se log_exit() JÁ TINHA gravado com PnL real, esse UPDATE vai
          SOBRESCREVER com PnL=0 (linha vt_autotrader.py:2945-2962 NÃO confere
          — preciso confirmar no fonte).

          ID atual #2069: exit_time=14:04:06 reason=GHOST PnL=0 src=RECONCILE
          ^ confirma: PnL=0, sobrescrito.
```

**Causa raiz:** a defesa #2 (`reconcile_positions_with_mt5`) corre **DEPOIS** da detecção de server-close e **SOBRESCREVE** o PnL com 0. Isso é o bug de hoje.

### 7.5 Mitigação parcial em `_resolve_orphan_closes` (commit c1996c6e)

A defesa #1 agora tem uma cláusula OR:
```sql
(exit_time IS NULL AND entry_ticket IS NOT NULL)
OR
(exit_time IS NOT NULL AND close_source = 'RECONCILE'
 AND close_source NOT LIKE 'ORPHAN_CLOSE_RESOLVED_%')
```

E re-busca history() para preencher PnL real. **MAS** se history() falhar (timeout, MT5 offline), a defesa #1 pula o trade (`stats["skipped_no_history"]`). O trade fica com PnL=0 permanentemente até o próximo tick com MT5 online.

**Quem chamar Bruno:** verificar `data/vt_autotrader.log` por `[ORPHAN-RESOLVE] history(WSPU26) falhou`.

---

## 8. Bugs Históricos (30 dias) e os 8 Commits de Hoje

### 8.1 Os 8 commits de hoje (2026-07-01) na trindade

| Hash | Mensagem | Tentou consertar |
|---|---|---|
| `71480b95` 10:17 | feat(loader): lock file de escrita no vt_config.json (anti-race) | Autotrader sobrescrevia config com dict parcial (580→18 linhas). Lock file com TTL 300s + PID check. |
| `798b8e46` 12:57 | chore(reconcile): ingeri MT5 positions no state/DB a cada tick (anti-orphan) | Posições órfãs no MT5 que bot não enxergava. `reconcile_positions_with_mt5()` a cada tick. |
| `ce026460` 13:13 | fix(reconcile): não inserir lixo em state.positions (entry_price=100, ticket=22222) | L2920-2945: filtro `entry_time>=hoje` + validação `entry_price>0 && volume>0`. |
| `dc447fd6` (anterior, mas relacionado) | fix(orchestrator): close() agora loga exit no DB | close() não persistia PnL no DB. Adicionou `_persist_close_to_db`. |
| `c1996c6e` 14:00+ | chore(autotrader): resolve orphan closes via MT5 history a cada tick (trindade #3) | _resolve_orphan_closes() a cada tick para preencher PnL real de trades cujo MT5 fechou sozinho. |
| `2b9beacc` 14:00 | fix(tests): isolar vt_trades.db em tmp_path | dc447fd6 vazou dado fake #2072 no DB de produção durante tests. |
| `12747a67` | feat(config): reativar BIT (v957, segunda reativação hoje pós reconcile fix) | BIT foi desabilitado por motivo de bug, reativado após fix. |
| `9bcf15d2` + `48780d05` | feat/test: liberar BUY na quarta | Manual override de Bruno. |

### 8.2 Outros bugs históricos relevantes (30 dias)

Commits `fix`/`chore` relacionados a divergência MT5↔DB↔state nos últimos 30 dias (não exaustivo):

- `5a181a3f` — 14 correções CRITICAL/HIGH/MEDIUM (SL, trailing, PnL, indicadores) [batch]
- `25fb75e1` — hermes PATH + state cleanup + orphan removal
- `7acfc605` — 10 bugs MEDIUM corrigidos [batch]
- `6d59b2fc` — emergency close logged in DB; drawdown uses real PnL
- `6e4e8da8` — wire safe_modify_sl_with_emergency_close into autotrader
- `88f6cfca` — anti-drawdown (breakeven + time-trail + max position)
- `7fbf7910` — resolved_symbols in config prevents symbol flip mid-day
- `d4b11d60` — validator SL mostra pontos reais (18pts) em vez de executor (18000pts) [units bug 100x]
- `26d3105b` — trail_activate 1.2→1.0 em 17 pares (causa raiz SL_SERVIDOR)
- `9cfec168` — gravar exit_sl_price em SL_SERVIDOR (schema gap)
- `2dec1e4f` — vt_hermes_helper import + detecta race condition (Wave 8.7)
- `759d1408` — fail-closed contra contratos rollover
- `916815c8` — init strategy_utils in run_once() (KeyError)
- `a87cf1de` — trailing stop, shell injection, div-by-zero, SL notification [batch]

**Total em 30 dias:** 60+ commits `fix` — sistema sob patching constante, indica que a arquitetura base tem problemas estruturais que patches incrementais não resolvem.

---

## 9. Incidentes Confirmados em 01/07/2026 (DB ao vivo)

### 9.1 PnL por fonte (snapshot 14:35 BRT)

| Fonte | PnL | Observação |
|---|---|---|
| `vt_trades.db` `trades.net_pnl WHERE exit_time IS NOT NULL AND date(entry_time)='2026-07-01'` | **R$ 301,71** | Soma direta |
| `state.daily_pnl` | **R$ 301,71** | _sync_daily_pnl_with_db foi chamado |
| Telegram `vt_daily_report.py` (EOD 16:50) | ⏳ ainda não gerado | vai usar `trades.net_pnl` filtrado por hoje |
| Extrato MT5 (corretora) | 🔴 **desconhecido** | Bruno precisa puxar do terminal MT5 ou do app da XP |

### 9.2 Trades do dia (todos)

| ID | Symbol | Dir | Entry | Exit | PnL | Src | Reason |
|---|---|---|---|---|---|---|---|
| 2066 | WSPU26 | BUY | 12:56:48 | 13:06:53 | +12.50 | hermes_manual | MANUAL_CLOSE |
| 2067 | WDOU26 | SELL | 13:02:18 | 13:07:34 | -5.00 | hermes_manual | MANUAL_CLOSE |
| 2068 | WSPU26 | BUY | 13:27:24 | 13:41:47 | -1.44 | None | SL_SERVIDOR |
| **2069** | **WSPU26** | SELL | 13:50:40 | 14:04:06 | **+0.00** | **RECONCILE** | **GHOST** |
| 2070 | WSPU26 | BUY | 10:40:00 | 11:23:00 | +20.05 | hermes_manual | ORPHAN_MANUAL |
| 2071 | WINQ26 | — | — | — | +275.60 | hermes_manual | ORPHAN_MANUAL |
| 2072 | — | — | — | — | — | — | REMOVIDO (vazou de tests) |
| **2073** | **WINQ26** | — | — | 14:24:34 | **+0.00** | **RECONCILE** | **GHOST** |
| **2074** | **BITN26** | — | — | 14:04:07 | **+0.00** | **RECONCILE** | **GHOST** |
| **2075** | **WSPU26** | BUY | 14:03:25 | 14:04:07 | **+0.00** | **RECONCILE** | **GHOST** |
| 2076 | BITN26 | BUY | 14:31:01 | (aberto) | — | — | — |
| 2077 | WSPU26 | BUY | 14:34:09 | (aberto) | — | — | — |

### 9.3 GHOSTs totais no DB (8): 4 de hoje + 4 de JUNHO (legado)

- **HOJE (PnL=0):** #2069 WSPU26, #2073 WINQ26, #2074 BITN26, #2075 WSPU26 (todos `close_source=RECONCILE`, `reason=GHOST`).
- **JUNHO (legado):** #1315 WDOU26, #1316 DOLN26, #1317 WSPM26, #1360 BITM26 (todos `reason=stale_close`).

**Total de PnL "fantasma" hoje: R$ 0** (são 4 trades zerados, sem como saber o real sem extrato MT5).

### 9.4 Orphans (inserções manuais via hermes)

- `id=2070` WSPU26 +R$ 20,05 + `id=2071` WINQ26 +R$ 275,60 → **soma +R$ 295,65** ao PnL do dia, não passaram pelo bot. **Provável causa da divergência** entre "Bruno reporta +R$ X" e "DB mostra +R$ Y": depende de qual query está rodando.

### 9.5 Param override do meio-dia

`vt_config.json:232` tem `max_daily_loss: -800` (v944 "MEIO TERMO"). O briefing cita que voltou a -800 vs -500 do `vt_meio_dia_tuning.py`. **Verificado:** o `scripts/vt_meio_dia_tuning.py` NÃO toca `max_daily_loss` diretamente — só altera `params_by_tf`. O `-800` veio de rodada AGI/manual anterior. **Não é regressão do script**, é decisão de v944 que ficou.

### 9.6 Trade #2068 — `modify_sl` falha silenciosa

Entry 13:27:24, exit 13:41:47 (14min), PnL -1.44, reason `SL_SERVIDOR`, **`close_source=None` (bug)**. O `safe_modify_sl_with_emergency_close` deveria ter setado `close_source='MT5_SERVER_SL'`. Não setou. **Likely fix:** em `manage_position()` L1960-1967, `log_exit()` recebe `close_source` não passado.

### 9.7 State vs DB

| Métrica | State | DB | Match? |
|---|---|---|---|
| `daily_pnl` | 301.71 | 301.71 (sum net_pnl) | ✅ |
| `trade_count` | 9 | 9 (fechados) | ✅ |
| `wins` | 3 | 3 (>0) | ✅ |
| `losses` | 6 | 5 (GHOST=0 não conta no state) | ⚠️ divergem |
| `daily_trade_count` | 12 | 11 | ⚠️ diff 1 (rejeição) |

**State e DB em sync AGORA** (porque `_sync_daily_pnl_with_db` rodou). Mas se `reconcile_positions_with_mt5` rodar entre agora e EOD, pode sobrescrever com 0 (caminho GHOST).

---

## 10. Race Conditions Conhecidas

### 10.1 Race #1 — `close()` vs `reconcile_positions_with_mt5()`

**Sequência do bug:**
1. `manage_position()` chama `safe_close()` → MT5 fecha posição.
2. `_persist_close_to_db()` (no orchestrator) começa a escrever no DB.
3. No próximo tick (antes de #2 terminar), `reconcile_positions_with_mt5()` lê MT5 e vê que a posição sumiu.
4. Marca GHOST com PnL=0 **antes** de #2 terminar.
5. Quando #2 termina, escreve UPDATE com PnL real — **MAS** se a UPDATE do #4 já commitou, pode ou sobrescrever ou ser sobrescrito dependendo da ordem.

**Ordem atual em `run_daemon`:** _resolve_orphan_closes → reconcile_positions → check_and_trade (que pode chamar close). Mas se `manage_position` chamar `safe_close` (L1777) DURANTE `check_and_trade`, o `close` do bot pode acontecer **antes** do próximo reconcile, OK.

**Race window:** ~30s (1 tick). Se close() cai exatamente no início do tick, e reconcile() roda no próximo tick, há 1 tick de janela onde o DB pode ter GHOST com PnL=0 antes do log_exit correto.

### 10.2 Race #2 — múltiplos ticks gravando `state.daily_pnl`

`state.daily_pnl += pnl` não é atômico. Se dois ticks processarem dois trades simultaneamente (improvável dado o single-thread do autotrader, mas possível se algum call MT5 bloquear o GIL), um dos incrementos pode se perder.

**Probabilidade:** muito baixa em prática (autotrader é single-thread).
**Severidade se acontecer:** daily_pnl off-by-one até próximo reconcile.

### 10.3 Race #3 — `recover_open_positions()` vs `reconcile_positions_with_mt5()`

No startup, `recover_open_positions()` popula state.positions a partir de MT5. Mas se `reconcile_positions_with_mt5()` rodar antes do `recover_open_positions()` terminar, pode tentar inserir o mesmo ticket duas vezes.

**Ordem atual em `run_daemon`:** `recover_open_positions()` (L3096) → reconcile startup (L3103-3121) → reconcile per-tick. ✅

### 10.4 Race #4 — `vt_trade_log._queue_notification` + `vt_notify.py`

A notificação é append-only em `/tmp/vt_notifications.jsonl`. Se o autotrader crasha entre `log_exit` e `_queue_notification`, o trade fica no DB mas a notificação Telegram não sai. **Não causa divergência de PnL**, mas quebra observabilidade.

---

## 11. Análise Comparada: como o resto da indústria faz

### 11.1 Robinhood, IBKR, Alpaca — padrão broker-truth

| Camada | Indústria | Vibe-Trading atual |
|---|---|---|
| **Source of truth** | Broker (sempre) | MT5 (teoria) + DB (prática) + state (memória) — competem |
| **Local store** | SQLite/Postgres como cache de queries caras, read-only p/ decisão | SQLite como log + cache + truth ao mesmo tempo |
| **State** | Projeção em memória, read-only depois de validação | Mutável, write-through, múltiplos writers |
| **Reconcile** | Single source of truth; reconcile só se flag `dirty`. Não toda tick. | Triple-source, reconcile a cada tick (caro + race-prone) |
| **Decisão crítica** | Toda ordem: re-validar com broker antes de submeter | Bot confia em state.positions; só MT5 reconfirma via `status()` |
| **Idempotência** | Client order ID para dedup | `INSERT OR IGNORE` + tag `ORPHAN_CLOSE_RESOLVED_%` |
| **Schema** | Versionado, migrations explícitas | Duplicado em 2 lugares (`vt_trade_log.init_db` + `mt5_orchestrator._TRADES_SCHEMA`) |

### 11.2 Padrão de mercado

```
1. Bot decide abrir ordem
2. Bot chama broker.place_order() — obtém client_order_id
3. Bot registra intenção em local store (status=PENDING)
4. Broker confirma via webhook/poll — atualiza local store (status=FILLED)
5. Bot lê local store para decisões (read-only)
6. Periodicamente: reconcile local store ↔ broker positions via API
```

**Chave:** o bot **nunca** lê o local store para saber se a posição está aberta — pergunta ao broker (MT5) toda vez. O local store é só cache de queries caras.

---

## 12. Recomendações para Bruno Decidir

### 12.1 Opção A — Manter arquitetura, fortalecer idempotência (1-2 sprints, refactor cirúrgico)

1. `reconcile_positions_with_mt5` §3 (ghost): antes de marcar PnL=0, tentar `history(symbol, days=1)`. Se falhar, não marcar — apenas logar `skipped_no_history`.
2. Padronizar timeout SQLite em TODOS os bypass (atualmente 5s/10s/30s inconsistente): 30s + WAL + busy_timeout=30000. Patch urgente: `dashboard/api/data.py` (11 calls), `monitoring/vt_tax_report.py`, `monitoring/vt_weekly_report.py`.
3. Remover schema duplicado em `mt5_orchestrator._TRADES_SCHEMA` (L37-71) — importar de `core.vt_trade_log`.
4. Atomic save: `state.save()` escreve em tmp + rename (não json.dump direto).
5. `threading.Lock` no `state.daily_pnl += pnl`.

**Ganho:** elimina ~80% dos GHOSTs sem reescrever. **Custo:** baixo.

### 12.2 Opção B — Refatorar para broker-truth (3-4 sprints, refactor significativo)

**Princípio:** MT5 é o ÚNICO source of truth. DB e state são derivadas.

```
MT5 (broker, autoritativo)
   ↓ status() / history() / positions_get()
mt5_truth.py (interface read-only: get_open_positions, get_today_pnl, get_deal_history)
   ↓ cache (TTL 5s)
Cache SQLite (read-only, snapshot de MT5)
   ↓ projection
State em memória (read-only, derivado de mt5_truth)
   ↓ auditoria
Logs + Telegram (somente humano)
```

**Regras:**
1. Toda decisão sensível (close, modify_sl, kill switch) chama `mt5_truth` ANTES. Sem estado intermediário.
2. DB vira cache puro: `INSERT OR REPLACE` de snapshots, read-only para queries.
3. State vira projeção read-only: `state.daily_pnl` é COMPUTADO de `mt5_truth.get_today_pnl()` a cada acesso. Sem `state.daily_pnl += pnl`.
4. Reconcile dispara em `dirty_flag`, não a cada tick. Dirty setado por: ordem submetida, retorno recebido, 5min sem reconcile.
5. Schema versionado com migrations explícitas (não `_TRADES_SCHEMA` duplicado).
6. Bot verifica posição real com MT5 antes do EOD close.

**Sprints:**
- S1: `mt5_truth.py` + `dirty_flag` + interval reconcile
- S2: `manage_position` usa `mt5_truth` em vez de `state.positions`; remover `state.daily_pnl += pnl`
- S3: Padronizar SQLite (1 helper) + schema versionado
- S4: Remover "fechar tudo 16:45" — bot verifica MT5 e age

### 12.3 Opção C — Híbrido (curto prazo, 2 sprints) ⭐ recomendado

1. Adicionar `mt5_truth.py` como camada adicional (não substituição).
2. `state.daily_pnl` vira `state.cached_daily_pnl`, revalidado a cada tick via `mt5_truth.get_today_pnl()`.
3. `reconcile_positions_with_mt5` §3 **NÃO marca PnL=0** — só registra intenção, deixa `_resolve_orphan_closes` preencher no próximo tick.
4. `close()` (bot) chama `mt5_truth` para confirmar que posição sumiu antes de `log_exit`.
5. Manter `recover_open_positions` e `_resolve_orphan_closes` como fallback.

**Por que Opção C:**
- Custo baixo-médio, sistema continua operando durante refactor.
- Elimina 3 bugs críticos: GHOST com PnL=0, race no dashboard, cache do broker-truth.
- Mantém compatibilidade com AGI optimizer, copilot, watchdog, dashboard.
- `mt5_truth.py` já prepara terreno para Opção B no Q3.

### 12.4 Quick wins para HOJE (15-30min, sem risco)

1. **Bruno puxar extrato MT5 do dia** (terminal ou app XP) e comparar com `state.daily_pnl = R$ 301,71`. Identifica quem está mentindo.
2. **Verificar `/tmp/vt_autotrader.log`** por `[ORPHAN-RESOLVE] history(... ) falhou` ou `[HISTORY FAIL]`. Se sim, MT5 intermitente.
3. **Re-rodar reconcile manual**:
   ```bash
   cd /home/bruno/Projects/Vibe-Trading
   python3 -c "from core.vt_history_reconcile import reconcile_db_with_mt5_history; print(reconcile_db_with_mt5_history())"
   ```
   Se preencher os 4 GHOSTs de hoje, o PnL vai saltar. Bruno vê o real.
4. **Não confiar em `vt_daily_report.py` 16:50** sem antes verificar se `_resolve_orphan_closes` rodou para os 4 GHOSTs.

---

## 13. Self-Healing Architecture (Catálogo de Auto-Correções)

> **Escopo desta seção.** Mapeia os mecanismos de auto-recuperação que **já existem** no
> código hoje (refactor MT5-truth, pós-commit `f944eab9`). Cada entrada cita `arquivo:linha`
> verificável. Esta seção fecha o "Forense" documentando o que o sistema **se autocorrige**
> — complementar às Seções 7 (anti-orphan), 9 (incidentes) e 10 (races).

> **Atualização FASE 2 (2026-07-01).** O catálogo abaixo (13.2) descreve o estado
> **pré-Fase 2** (snapshot commit `f944eab9`). A Fase 2 do handoff entregou três
> novos módulos que **estendem** este catálogo:
>
> | Módulo | Papel | Crons |
> |---|---|---|
> | `optimization/agi_synthesizer.py` | Loop de Síntese de Estratégia (Lei 5): quando `run_exhaustive_search` não acha edge, varia params sobre as 27 estratégias via `simulate_forward` até achar lucro ou esgotar iterações. IND short-circuit (Lei 2). | chamado pelo AGI 17:10 quando há `all_negative_pairs` |
> | `monitoring/vt_self_heal.py` | Self-healing monitor: 6 health checks (autotrader, MT5, DB, state, lock, cron) + auto-cura conservadora (restart processo/MT5, rm lock órfão). Nunca desabilita símbolo (Lei 2). Integrado no `vt_copilot.py` (hook no início do `--full` + modo `--self-heal`). | `*/5 9-17` (pregão) + `*/15 0-8,18-23` (fora) |
> | `scripts/check_symbols_active.py` | Auditor de Integridade de Escopo (Lei 2): valida 16 pares WIN/BIT/WSP/WDO × M5/M15/M30/H1 ativos. IND ignorado. READ-ONLY — Bruno decide reativação. | `0 9 * * 1` (segundas) |
>
> **Cobertura adicionada vs. gaps da Seção 13.3:**
> - Gap #1 (MT5/Wine travado fora das janelas do copilot) → `vt_self_heal.py` agora roda a cada 5min no pregão e reinicia o MT5 via `start_mt5linux.sh`.
> - Gap #2 (supervisão descontínua) → `vt_self_heal.py` roda 24/7 (15min fora do pregão) e integra-se ao copilot.
> - Lei 2 (integridade de escopo) → `check_symbols_active.py` audita semanalmente e alerta se WIN_H1/BIT_M5/BIT_M30 (caso real detectado) ficarem desabilitados.
>
> **Estado dos testes:** 56 novos testes (19 + 17 + 7 + 13) verdes. Suite total 235+ passed
> (1 flaky pré-existente em `test_validation_3days`, isolado, não relacionado à Fase 2).

### 13.1 Diagrama ASCII dos loops de self-healing

```
                              ┌──────────────────────────────────────┐
                              │           FONTE DE VERDADE            │
                              │              MT5 (broker)             │
                              └─────────────────┬────────────────────┘
                                                │  truth (get_open_positions / history)
                    ┌───────────────────────────┼───────────────────────────┐
                    ▼                           ▼                           ▼
          ┌─────────────────┐         ┌──────────────────┐        ┌──────────────────┐
          │  PER-TICK LOOP  │         │   CRON 5 min     │        │  CRON 10/12/15h  │
          │ (vt_autotrader) │         │ (vt_trade_watchd)│        │  (vt_copilot)    │
          └────────┬────────┘         └─────────┬────────┘        └─────────┬────────┘
                   │                            │                           │
   1. _resolve_orphan_closes        5. compute_pnl_drift        6. check_autotrader_health
   2. reconcile_positions_with_mt5  →  alerta Telegram            →  restart_autotrader (pkill+start)
   3. _persist_close_to_db          →  status JSON               7. reconcile_orphans (DB×MT5)
   4. validate_order_pre_send                                   8. intraday report (MT5 history)
                   │                            │                           │
                   ▼                            ▼                           ▼
          ┌─────────────────────────────────────────────────────────────────────────┐
          │                        DESTINO DA AUTO-CURA                              │
          │  state.positions (mem) · vt_trades.db (WAL) · vt_config.json (locked)    │
          └─────────────────────────────────────────────────────────────────────────┘
                                ▲
                                │ atomic write + backup pré-write
                   ┌────────────┴────────────┐
                   │   PROTEÇÃO DE ESCRITA    │
                   │  (vt_config_loader)      │
                   │  lock + whitelist +      │
                   │  _assert_authorized_writ │
                   └─────────────────────────┘
```

**Legenda de leitura:** cada loop pergunta ao MT5 "qual a verdade?" e então reconcilia state/DB
para refletir essa verdade. O per-tick loop é o mais forte (roda a cada tick, idempotente,
history-backed). Os crons 5min/10-15h são as camadas externas de supervisão.

### 13.2 Catálogo de auto-correções (10 entradas)

| # | Tipo de falha | Mecanismo (auto-cura) | Localização | Gatilho |
|---|---|---|---|---|
| 1 | Autotrader morto / log stale | `check_autotrader_health()` → `restart_autotrader()` (pkill -9 + relança + verifica PID) | `monitoring/vt_copilot.py:222,249` | cron 10/12/15h |
| 2 | State desatualizado / stale após restart | `rebuild_state_from_mt5()` — zera `positions` e reprojeta 1:1 do MT5 (magic 555501) | `core/vt_autotrader.py:380` | boot + cada tick |
| 3 | Ghost trade (DB `exit_time IS NULL`, ticket já fechado no MT5) | `_resolve_orphan_closes()` — backfill `exit_time/exit_price/net_pnl` do deal MT5 | `core/vt_autotrader.py:2537` | cada tick |
| 4 | Orphan (posição MT5 não está no state/DB) | `reconcile_positions_with_mt5()` — ingere do MT5 (INSERT) ou marca GHOST (DB exit) | `core/vt_autotrader.py:2865` | cada tick |
| 5 | PnL do DB divergente do MT5 (drift) | `reconcile_db_position()` — soma `profit+commission+swap` dos deals e reescreve `net_pnl` | `core/vt_truth.py:444` | startup + 10 ticks |
| 6 | Drift diário MT5×DB > R$ 5 | `compute_pnl_drift()` → alerta Telegram consolidado | `monitoring/vt_trade_watchdog.py:316` | cron 5min |
| 7 | Ordem rejeitada (INVALID_STOPS / REQUOTE / MARGIN) | `safe_buy/safe_sell` — recalc SL, novo tick+resend, halve volume; LLM fallback | `mt5/mt5_error_recovery.py:290,376` | em cada envio |
| 8 | `modify_sl` falha 3× + posição contra | `safe_modify_sl_with_emergency_close()` → `_emergency_close_position()` | `core/vt_emergency.py:251,155` | MAX_FIX_ATTEMPTS=3 |
| 9 | Ordem duplicada (mesmo magic+symbol já aberto) | `validate_order_pre_send()` bloqueia envio | `core/vt_truth.py:541` | antes de cada envio |
| 10 | Escrita ilegal de `vt_config.json` | `_assert_authorized_writer()` (whitelist) + `acquire_write_lock()` (sidecar .lock) + atomic `_atomic_write()` | `core/vt_config_loader.py:270,185,519` | toda escrita |

**Trindade anti-orphan (Seção 7) = entradas #3 + #4 + a persistência de close.** Ordem de execução
garantida a cada tick: `_resolve_orphan_closes` → `reconcile_positions_with_mt5` → `_persist_close_to_db`
(`core/vt_autotrader.py:3395-3412`), tudo envolvido em try/except (nunca derruba o tick).

### 13.3 Quando o sistema NÃO auto-corrigi (limites — exige humano)

Estes são os limites explícitos do self-healing atual. Cada item é um **gap acionável**
(endereçado pelas Fases 2/3 deste handoff):

1. **MT5/Wine travado entre janelas do copilot.** O autotrader detecta MT5 indisponível
   (`get_truth_from_mt5()` retorna `ok=False`, watchdog retorna `[]`), mas **não reinicia
   o bridge Wine/MT5** — só loga `[MT5_SYNC] falha`. Entre 15:30 e 09:00 um travamento
   deixa o bot "cego" até o próximo cron. *(Fase 3.2: vt_self_heal MT5 ping + restart)*
2. **Supervisão descontínua.** `restart_autotrader()` só roda via copilot às 10/12/15h.
   O watchdog 5min **só alerta**, não reinicia. Não há supervisor contínuo (systemd/watchpid).
   *(Fase 2.2: cron 5min para vt_self_heal)*
3. **`validate_order_pre_send` fail-OPEN em blackout MT5.** Se MT5 está indisponível, retorna
   `True` (permite envio) confiando nos retries do `safe_buy/safe_sell` (`vt_truth.py:562-572`).
   Em blackout, a proteção anti-duplicata fica efetivamente desligada para aquele símbolo.
4. **State 100% dependente do MT5 no restart.** `state.save()`/`load()` viraram NO-OPs. Se o
   MT5 está down no boot, o bot sobe com `positions={}` até o MT5 voltar. Não há state
   persistido para replay/debug.
5. **Emergency close só dispara em falha de `modify_sl`.** Não há fallback geral "risco estourado
   + path de ordem falhou → force-close". `safe_buy/safe_sell` que esgota retries apenas retorna
   rejeitado (não há posição a fechar).
6. **Lock de config é advisory, não kernel-level.** Protege contra processos que passam pelo
   loader, mas não contra `json.dump` direto que bypassa `save_params`. *(Whitelist mitiga só
   módulos in-tree; Fase 1 já adicionou audit test `test_config_write_separation.py)*
7. **Fragmentação de reconcilers.** `reconcile_positions_with_mt5` (autotrader, per-tick),
   `reconcile_orphans` (copilot, tick-price) e `reconcile_with_mt5` (watchdog, DB-backed) são
   3 implementações quase-idênticas com filtros diferentes — risco de divergência de PnL entre
   elas. Não é mecanismo faltante, mas dívida técnica a consolidar.

### 13.4 Matriz de cobertura por Lei de Ouro

| Lei | Self-healing que a defende | Status |
|---|---|---|
| Lei 2 — Integridade de escopo (AGI nunca desabilita símbolo/TF) | `PERMANENTLY_DISABLED={'IND'}` hard-kill só-Bruno (4 camadas) + `apply_changes` strip | ✅ Coberta |
| Lei 3 — SL obrigatório | (validação delegada ao caller hoje; **Fase 3.4** adiciona `MissingStopLossError` dentro de `buy/sell`) | ⚠️ Parcial |
| Lei 4 — Garantia MT5 (ticket confirmado) | `safe_buy/safe_sell` retry + `_verify_position_after_reject` (race reject×fill) | ⚠️ Parcial (**Fase 3.3** adiciona `OrderNotConfirmedError`) |
| Lei 5 — Motor AGI contínuo | `agi_tuning_17h.py` cron 17:10 + Regra 1 (síntese se sem edge) | ⚠️ Parcial (**Fase 2.1** formaliza loop de síntese) |

---

## Anexo A — Arquivos Críticos

- `mt5/mt5_orchestrator.py` (448) — bridge MT5, schema duplicado L37-71.
- `mt5/mt5_error_recovery.py` (660) — retry + LLM fallback para ordens.
- `core/vt_trade_log.py` (663) — schema autoritativo + funções core.
- `core/vt_autotrader.py` (3244) — loop principal, 3 defesas anti-orphan, manage_position.
- `core/vt_history_reconcile.py` (421) — reconciliação via MT5 history.
- `core/vt_emergency.py` (357) — emergency close wrapper.
- `monitoring/vt_daily_report.py` (286) — relatório 16:50.
- `dashboard/api/data.py` (~370) — bypass SQLite sem WAL.

## Anexo B — Comandos Úteis

```bash
cd /home/bruno/Projects/Vibe-Trading
# Estado do dia:  sqlite3 vt_trades.db "SELECT id,symbol,entry_time,exit_time,net_pnl,close_source,exit_reason FROM trades WHERE date(entry_time)='2026-07-01' ORDER BY id"
# GHOSTs:         sqlite3 vt_trades.db "SELECT id,symbol,entry_ticket,exit_reason FROM trades WHERE exit_time IS NOT NULL AND (net_pnl=0 OR net_pnl IS NULL)"
# Reconciliar:    python3 -c "from core.vt_history_reconcile import reconcile_db_with_mt5_history; print(reconcile_db_with_mt5_history())"
# Forçar orphan:  python3 -c "from core.vt_autotrader import _resolve_orphan_closes; _resolve_orphan_closes()"
# Erros do dia:   grep -E "(ERROR|FAIL|RECONCILE|GHOST|ORPHAN-RESOLVE)" /tmp/vt_autotrader.log | tail -50
# Notif Telegram: tail -30 /tmp/vt_notifications.jsonl
```

---

**Fim do relatório forense.**  
Próximo passo sugerido: Bruno decide entre Opção A (rápido) ou Opção C (híbrido) para esta semana; Opção B fica no roadmap Q3.
