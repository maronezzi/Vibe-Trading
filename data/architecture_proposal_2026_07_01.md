# Proposta de Arquitetura — Vibe-Trading com MT5 como Fonte de Verdade

**Data:** 2026-07-01
**Autor:** Hermes (Wave 12 — design proposto)
**Mandato:** Bruno Maronezzi
**Status:** Proposta para revisão e aprovação

---

## 1. Sumário Executivo

### 1.1 Problema arquitetural atual

O Vibe-Trading hoje trata **três fontes de estado como se fossem autoritativas** ao mesmo tempo: o DB SQLite (`vt_trades.db`), o arquivo de state em `/tmp/vt_autotrader_state.json` e o resultado de `mt5.status()` / `mt5.history()` lidos sob demanda. Isso produz divergência contínua por desenho, não por bug pontual. O fluxo real é: o autotrader envia uma ordem, o MT5 fecha sozinho via SL server-side (sem o bot saber), o `log_exit()` em `vt_trade_log.py` perde a janela (DB lock, restart, race com `manage_position`) e o trade fica com `exit_time IS NULL` + `net_pnl = 0` no DB. O próximo ciclo de `reconcile_positions_with_mt5()` (commit `ce026460`) detecta o drift e marca o trade como `GHOST` com PnL zerado — o `vt_daily_report` exclui `GHOST`, então o trade some do relatório. Por isso o intraday do Telegram não bate com a verdade do broker: o `daily_pnl` no state é a soma de `net_pnl` do DB, mas o DB está mentindo.

A “trindade anti-orphan” montada nas últimas 24h (`_resolve_orphan_closes` + `reconcile_positions_with_mt5` + `_persist_close_to_db` em `close()`) cobre 95% dos casos, mas é **reativa e tem ordem frágil** — se `_resolve_orphan_closes` não rodar antes de `reconcile_positions_with_mt5` no tick, voltamos ao GHOST com PnL=0. São 8+ commits só hoje tapando buraco por buraco porque o modelo é “toda fonte é autoritativa até provar o contrário”, e provar o contrário exige N rodadas de reconcile contra N fontes.

### 1.2 Solução proposta (alto nível)

Inverter a hierarquia: **MT5 é a única fonte autoritativa**, DB vira cache local com TTL explícito, state vira projeção em memória reconstruída do MT5 a cada restart, e os logs locais servem só para auditoria/decisão de longo prazo (não para decisão de tick). Toda decisão de trading passa por um único contrato `core/vt_truth.py` que centraliza `get_open_positions()`, `get_daily_pnl()`, `get_position_history()` e `reconcile_db_from_mt5()`. Toda ação de escrita (BUY/SELL/MODIFY_SL/EMERGENCY_CLOSE) passa por um padrão `pre-condition → action → post-condition → diff` que re-consulta o MT5 antes de confiar no retorno do executor. O DB deixa de ser consultado para coisas que o MT5 já sabe — vira write-through cache que aceita ser descartado.

### 1.3 Benefícios esperados

- **PnL confiavelmente igual ao do broker.** `get_daily_pnl()` lê `mt5.history_deals_get()` direto, somando `profit + commission + swap` dos deals do dia. Telegram intraday bate com extrato da corretora no centavo.
- **Zero orphans por construção.** Antes de cada BUY, `validate_order_pre_send()` consulta MT5 positions; se já existe posição aberta no mesmo símbolo+direção com mesmo magic, bloqueia. O bug que gerou trades #2069/#2073/#2074/#2075 vira impossível por design.
- **Recuperação automática sem código defensivo.** Restart do autotrader = chamar `rebuild_state_from_mt5()` na inicialização, que reconstrói `state.positions` lendo `status()`. Não precisa de `recover_open_positions()` ad-hoc nem de `reconcile_positions_with_mt5()` defensivo a cada tick.
- **Trindade anti-orphan vira desnecessária.** Os três reconciliadores atuais (`_resolve_orphan_closes`, `reconcile_positions_with_mt5`, `_persist_close_to_db`) colapsam em um: `reconcile_db_from_mt5()` chamado a cada N segundos, idempotente, write-through.
- **DB lock deixa de ser incidente.** DB vira cache: se locked por >5s, autotrader não trava — segue operando com truth do MT5, e o reconcile do próximo tick grava o cache de novo.

---

## 2. Arquitetura Atual

### 2.1 Diagrama — quem lê/escreve cada fonte

```
┌──────────────────────────────────────────────────────────────────────────┐
│                       ARQUITETURA ATUAL (2026-07-01)                     │
└──────────────────────────────────────────────────────────────────────────┘

  ┌──────────────────┐                            ┌────────────────────┐
  │   /tmp/...       │   ← escrita por save()     │  vt_trades.db      │
  │   vt_autotrader  │   ← leitura em load()      │  (SQLite)          │
  │   _state.json    │   ← trada como autoritativa│  ← trada como      │
  │                  │      para positions/       │     autoritativa   │
  │   • positions    │      daily_pnl/trade_count │     para PnL/      │
  │   • last_signals │                            │     exit_time/     │
  │   • daily_pnl    │   ★ VIRA TRASH APÓS 5min ★ │     strategy       │
  │   • halt_until   │     (off-by-one conhecido)  │                    │
  └────────┬─────────┘                            └─────────┬──────────┘
           │                                                │
           │ lê                                              │ lê
           │                                                │
           ▼                                                ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  core/vt_autotrader.py — run_daemon loop (30s tick)                 │
  │                                                                      │
  │   1. load state (positions)            ← STATE autoritativo (?)     │
  │   2. check strategies → sinal de BUY                                │
  │   3. safe_buy(symbol)                  →  MT5 (autoritativo)        │
  │   4. log_entry()                       →  DB (aut. para registro)   │
  │   5. state.positions[key] = {...}      →  STATE (aut. para gestão)  │
  └──────────────────────────────────────────────────────────────────────┘
           │                                                │
           │                                                │
           ▼                                                ▼
  ┌──────────────────────┐                        ┌────────────────────┐
  │ MT5 (via Wine)       │                        │ DB writes também   │
  │ • status()           │ ← consultado para      │ disparados por:    │
  │ • history()          │   reconcile mas NÃO     │ • close() (manual) │
  │ • buy/sell/modify/   │   para decisão de tick  │ • _persist_close   │
  │   close              │                        │   _to_db()         │
  │                      │                        │ • import_mt5_      │
  │ ★ FECHAMENTO         │                        │   history()        │
  │   SERVER-SIDE É      │                        │                    │
  │   INVISÍVEL ★        │                        │ ★ TODOS REATIVOS ★ │
  └──────────────────────┘                        └────────────────────┘
           ▲                                                ▲
           │                                                │
           │   Trindade reativa a cada tick (~30s):         │
           │   ┌───────────────────────────────────────┐    │
           └───┤ 1. _resolve_orphan_closes()           │────┘
               │    → se MT5 fechou e DB está NULL,     │
               │      busca deal no history() e UPDATE  │
               │                                        │
               │ 2. reconcile_positions_with_mt5()     │
               │    → se MT5 tem e state não, ingere    │
               │    → se state tem e MT5 não, GHOST     │
               │                                        │
               │ 3. _persist_close_to_db() [em close()]│
               │    → se close() manual rodou, UPDATE   │
               └────────────────────────────────────────┘
                          ORDEM FRÁGIL:
              se _resolve_orphan_closes roda DEPOIS de
              reconcile, gera GHOST com PnL=0 (bug de hoje)
```

### 2.2 Caminhos de close (3 atualmente)

Hoje uma posição pode ser fechada por **três caminhos diferentes**, cada um persistindo o resultado de forma diferente no DB:

| # | Caminho | Quem detecta | Quem persiste | Persiste o quê | Bug conhecido |
|---|---------|--------------|---------------|----------------|---------------|
| 1 | **Bot chama `close(symbol)`** | `mt5_orchestrator.close()` | `_persist_close_to_db()` (commit `dc447fd6`) | UPDATE trade com `profit+commission+swap` reais do broker | OK quando DB não está locked |
| 2 | **MT5 fecha sozinho (SL/TP server-side)** | `_resolve_orphan_closes()` (commit Wave 12) | UPDATE direto na tabela `trades` | PnL real do deal via `mt5.history()` | Pula se roda depois de `reconcile_positions_with_mt5` |
| 3 | **MT5 fecha sozinho (SL/TP) — caminho B** | `reconcile_positions_with_mt5()` (commit `ce026460`) | INSERT/UPDATE com `close_source='RECONCILE'` ou marca `GHOST` | PnL=0 se DB estava NULL e history ainda não chegou | Marca GHOST com PnL=0 → some do intraday report |

O **caminho 3** é o que mais dói: `reconcile_positions_with_mt5` corre a cada tick e, se o MT5 fechou sozinho mas o `mt5.history()` ainda não indexou o deal, o bot marca o trade como `GHOST` no DB. O `_resolve_orphan_closes` da próxima tick deveria corrigir, mas como `reconcile` já setou `close_source='RECONCILE'` e o intraday report filtra por `strategy NOT LIKE '%GHOST%'`, a janela de dano é real.

### 2.3 Pontos de divergência (trindade)

```
                    ┌──────────────────────────────────┐
                    │ 1. CONFIG LOCK (vt_config.json)  │
                    │    acquire_write_lock()          │
                    │    sidecar .lock file            │
                    │    8h55: pre_flight resolve      │
                    │    17h10: agi_tuning             │
                    │    QUALQUER escritor: 17h17 race  │
                    │    → 2x em 1 dia, config         │
                    │    reduzido de 580 → 18 linhas   │
                    └──────────────────────────────────┘
                                      │
                                      ▼
                    ┌──────────────────────────────────┐
                    │ 2. RECONCILE POSITIONS ↔ MT5     │
                    │    (a cada tick)                  │
                    │    Decide quem é "source of       │
                    │    truth" para state.positions   │
                    │    e para o INSERT/UPDATE no DB  │
                    │    Estado ambíguo: pode marcar   │
                    │    GHOST com PnL=0               │
                    └──────────────────────────────────┘
                                      │
                                      ▼
                    ┌──────────────────────────────────┐
                    │ 3. RESOLVE ORPHAN CLOSES         │
                    │    (a cada tick, ANTES do recon.) │
                    │    Único path que preenche PnL    │
                    │    real em GHOST já marcados,    │
                    │    mas só funciona se history()   │
                    │    já tem o deal indexado.        │
                    │    Janela típica: 5-30s após      │
                    │    close server-side.             │
                    └──────────────────────────────────┘
                                      │
                                      ▼
                          Intraday report usa DB
                          (que está mentindo durante
                           essa janela de 5-30s)
```

---

## 3. Arquitetura Proposta

### 3.1 Diagrama — MT5 autoritativo, DB cache, state mirror

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    ARQUITETURA PROPOSTA (Wave 12)                        │
└──────────────────────────────────────────────────────────────────────────┘

  ┌────────────────────┐
  │ MT5 (via Wine)     │   ★ ÚNICA FONTE AUTORITATIVA ★
  │                    │   • positions abertas (status)
  │  • status()        │   • deals fechados (history)
  │  • history()       │   • balance / equity / margin
  │  • buy/sell/modify │   • tick prices
  │  • close           │
  │                    │   TODA decisão lê daqui.
  │  ★ fecha sozinho   │   DB só é consultado se MT5
  │    (server-side)   │   falhar (fallback raro).
  └────────┬───────────┘
           │
           │  toda escrita
           │  passa por
           │  pre→action→post→diff
           ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  core/vt_truth.py  (NOVA — fonte central de leitura)            │
  │                                                                  │
  │  • get_open_positions() → list[Position]  (cache 1s em RAM)     │
  │  • get_position_history(symbol, since, until) → list[Deal]      │
  │  • get_daily_pnl(date) → Decimal (centavos, MT5 history deals) │
  │  • get_account_snapshot() → dict (balance/equity/margin)        │
  │  • reconcile_db_from_mt5() → dict (stats)                       │
  │  • validate_order_pre_send(symbol, direction) → bool            │
  │  • rebuild_state_from_mt5() → None (startup)                    │
  │                                                                  │
  │  TODOS os módulos chamam essas funções. Ninguém                 │
  │  importa mt5_orchestrator.status() direto.                      │
  └──────────────────────────────────────────────────────────────────┘
           │                          │
           │ lê                       │ escreve
           ▼                          ▼
  ┌─────────────────────┐    ┌──────────────────────────────┐
  │ In-memory CACHE     │    │ vt_trades.db (SQLite)         │
  │ (RAM, TTL 1-2s)     │    │                              │
  │                     │    │ ★ CACHE WRITE-THROUGH ★      │
  │  • _open_pos_cache  │    │   TTL 5min para trades com   │
  │    expires 1s       │    │   exit_time NOT NULL          │
  │  • _history_cache   │    │   (pode ser reconstruído     │
  │    expires 30s      │    │    do MT5)                   │
  │  • _account_cache   │    │                              │
  │    expires 2s       │    │ Trades > 30 dias:            │
  │                     │    │   mantidos aqui (MT5 history │
  │ state.positions =   │    │   tem limite de 30 dias)     │
  │  projeção em RAM,   │    │                              │
  │  reconstruída de    │    │ Diário:                      │
  │  MT5 a cada         │    │   SELECT direto de MT5       │
  │  restart (sem       │    │   history_deals — DB só pra  │
  │  persistir em       │    │   detalhe IR/long-term.      │
  │  disco).            │    │                              │
  └─────────────────────┘    └──────────────────────────────┘
                                      │
                                      │  usado por
                                      ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Logs locais (/tmp/vt_*.log, /tmp/vt_*.jsonl)                   │
  │                                                                  │
  │  ★ APPEND-ONLY ★ — NUNCA consultados para decisão.              │
  │  Servem para:                                                    │
  │   • auditoria (o que o bot fez e por quê)                        │
  │   • decisão de longo prazo (AGI otimiza olhando 30+ dias)       │
  │   • forense pós-incidente (ex.: 30/06 orphan hunt)              │
  └──────────────────────────────────────────────────────────────────┘
```

### 3.2 Função central `core/vt_truth.py`

Novo módulo (não existe hoje) que centraliza **toda** leitura de estado. Se há um único ponto de acesso ao MT5, todo o resto do código fica livre para ser reativo e pequeno.

```python
# core/vt_truth.py  (NÃO EXISTE — A CRIAR)

# Estado interno (in-memory, volátil, reconstruído do MT5)
_open_pos_cache: list | None = None      # TTL 1s
_account_cache: dict | None = None       # TTL 2s
_history_cache: dict[tuple, list] = {}   # TTL 30s, key=(symbol, since, until)

CACHE_TTL_POS = 1.0      # posições mudam a cada tick
CACHE_TTL_ACC = 2.0      # balance/equity mexem a cada posição
CACHE_TTL_HIST = 30.0    # history não muda dentro de 30s

def get_open_positions() -> list[Position]:
    """Snapshot atual de posições abertas no MT5 (com cache 1s).
    FONTE AUTORITATIVA — DB e state NUNCA são consultados para isto.
    """

def get_daily_pnl(date_iso: str | None = None) -> Decimal:
    """PnL líquido do dia em centavos, somando profit+commission+swap
    de TODOS os deals MT5 de hoje. NUNCA consulta DB.
    """

def get_position_history(symbol, since_iso, until_iso) -> list[Deal]:
    """Deals de um símbolo no intervalo. Cache 30s."""

def get_account_snapshot() -> dict:
    """{balance, equity, margin_free, margin_level} via MT5 (cache 2s)."""

def validate_order_pre_send(symbol: str, direction: str) -> bool:
    """Bloqueia nova ordem se já existe posição aberta mesmo symbol+
    direction no MT5. Chamado ANTES de enviar BUY/SELL. Falha-segura:
    se MT5 indisponível, retorna False (= bloqueia, conservador)."""

def rebuild_state_from_mt5() -> None:
    """Reconstroi state.positions in-memory a partir de MT5.status().
    Chamado no startup do autotrader. Substitui recover_open_positions()
    + load() do /tmp/vt_autotrader_state.json."""

def reconcile_db_from_mt5() -> dict:
    """Sincroniza DB (cache) com MT5 (truth). Idempotente. A cada N
    segundos, NÃO a cada tick (custo de history() é ~200ms por
    símbolo). Retorna {checked, updated, inserted_orphans, errors}.

    Estratégia:
      1. Para cada posição aberta no MT5:
         - se ticket não existe no DB → INSERT (cache, com flag
           _synced_from_mt5=TRUE)
         - se ticket existe e exit_time é NULL → ok
         - se ticket existe e exit_time é NOT NULL → deveria estar
           fechado; verifica se ainda aparece em MT5 (não deveria)
      2. Para cada deal no MT5 history do dia:
         - se deal.entry_ticket existe no DB com exit_time NULL →
           UPDATE com profit real
         - se deal.entry_ticket NÃO existe no DB → INSERT como orphan
           (genuíno: o bot abriu mas a persistência falhou)
      3. Para cada trade no DB com exit_time NOT NULL há mais de 5min:
         - verifica se ainda bate com MT5 history. Se divergir, marca
         - drift_audited=TRUE (sem sobrescrever — auditoria futura)."""
```

### 3.3 Padrão de escrita: pre-condition → action → post-condition → diff

**Toda** ação de escrita (BUY, SELL, MODIFY_SL, EMERGENCY_CLOSE) passa por este contrato. É a mudança mais importante do design.

```python
# Pseudo-código do padrão (não está no código — é design)

def execute_with_truth_contract(action_fn, *, symbol, direction, **kwargs):
    """
    Padrão canônico para TODA escrita que vai pro MT5.

    action_fn: callable que faz a escrita (safe_buy, safe_sell,
                safe_modify_sl, safe_close).

    Retorna dict com {status, ticket, pre_state, post_state, diff,
    pnl_real_if_closed}.
    """

    # 1) PRE-CONDITION
    pre_state = truth.get_open_positions()
    if not truth.validate_order_pre_send(symbol, direction):
        return {"status": "BLOCKED", "reason": "duplicate_or_no_mt5"}

    # 2) ACTION (chama MT5 via Wine — ~200ms)
    action_result = action_fn(symbol, **kwargs)

    # 3) POST-CONDITION
    time.sleep(0.3)   # MT5 precisa propagar o fill
    post_state = truth.get_open_positions()

    # 4) DIFF
    diff = compute_position_diff(pre_state, post_state, action_result)

    # 5) PERSIST (cache write-through, NUNCA bloqueia)
    if diff.opened:
        db_write_queue.put(("INSERT", _build_trade_row(diff, action_result)))
    elif diff.closed:
        db_write_queue.put(("UPDATE", diff.ticket, _build_exit_row(diff)))

    return diff
```

Vantagens concretas:
- Se o bot reabrir ordem duplicada por bug de signal, o `validate_order_pre_send()` no passo 1 bloqueia antes de gastar 200ms no Wine.
- Se o MT5 retornar `FILLED` mas o ticket não aparecer em `post_state`, sabemos que o fill foi rejeitado server-side sem re-try cego.
- O DB write é fire-and-forget: se o DB travar, o bot continua operando (truth está no MT5). Próximo reconcile do `reconcile_db_from_mt5()` corrige o cache.

### 3.4 In-memory cache

Cache em RAM dentro de `core/vt_truth.py` (módulo-level, não global) com TTL explícito:

| Função | TTL | Justificativa |
|--------|-----|---------------|
| `get_open_positions()` | 1.0s | Posições mudam a cada tick (~30s); 1s evita re-query dentro de um mesmo tick se chamado N vezes. |
| `get_account_snapshot()` | 2.0s | Balance/equity mexem só em eventos de posição; 2s é folgado. |
| `get_position_history()` | 30s | History não muda dentro de 30s. Reconciliador só roda a cada 30-60s. |
| `get_daily_pnl()` | 5.0s | PnL diário cresce monotonicamente; 5s é OK. Mas o intraday report SEMPRE invalida o cache antes de calcular. |

```python
# Padrão de invalidação
_truth_cache = SimpleNamespace()  # placeholder
_truth_cache._open_pos_ts = 0
_truth_cache._open_pos_data = None

def _cached_open_positions():
    now = time.monotonic()
    if (now - _truth_cache._open_pos_ts) < CACHE_TTL_POS and _truth_cache._open_pos_data is not None:
        return _truth_cache._open_pos_data
    data = _mt5_status_uncached()
    _truth_cache._open_pos_ts = now
    _truth_cache._open_pos_data = data
    return data
```

---

## 4. Migração por Fases

### 4.1 FASE 1 — Quick wins (1-2 dias)

**Objetivo:** parar o sangramento de PnL errado no intraday SEM refatorar arquitetura.

| Task | Esforço | Risco |
|------|---------|-------|
| Adicionar `get_daily_pnl_truth(date_iso)` em `core/vt_truth.py` (stub inicial) que chama `mt5_orchestrator.history(symbol=None, days=1)` e soma `profit+commission+swap` por deal | 2h | Baixo — função nova, sem mudar código existente |
| Modificar `vt_daily_report.get_trades_report()` para usar `get_daily_pnl_truth()` no campo `total_pnl` e `get_open_positions()` no campo `trades` abertos (em vez de DB) | 1h | Baixo — só impacta o relatório, intraday continua igual |
| Estender `monitoring/vt_trade_watchdog.py` para também chamar `history_deals_today()` e comparar com soma do DB | 1h | Baixo — watchdog é read-only |
| Adicionar teste `tests/test_intraday_uses_mt5_truth.py` que valida: se DB tem PnL=0 mas MT5 history tem PnL=+50, intraday reporta +50 | 1h | — |

**Resultado Fase 1:** Telegram intraday bate com broker-truth no dia seguinte. Não muda nada no loop do autotrader.

### 4.2 FASE 2 — Refactor do truth layer (3-5 dias)

**Objetivo:** introduzir `core/vt_truth.py` como single point of truth; DB vira cache com TTL.

| Task | Esforço | Risco |
|------|---------|-------|
| Criar `core/vt_truth.py` com `get_open_positions()`, `get_daily_pnl()`, `get_position_history()`, `get_account_snapshot()`, todos com cache in-memory TTL | 4h | Médio — bug em cache invalidation pode estagnar dados |
| Criar `validate_order_pre_send()` em `vt_truth.py` (consulta MT5 positions) | 2h | Médio — falso positivo bloqueia trade legítimo |
| Refatorar `core/vt_autotrader._execute_entry()` para chamar `validate_order_pre_send()` ANTES de `safe_buy/sell` | 2h | Baixo — defesa em profundidade, não substitui a checagem existente |
| Refatorar `reconcile_positions_with_mt5()` para chamar `vt_truth.get_open_positions()` em vez de `status()` direto | 1h | Baixo |
| Refatorar `_resolve_orphan_closes()` para usar `vt_truth.get_position_history()` | 1h | Baixo |
| Refatorar `vt_trade_watchdog.py` para usar `vt_truth.*` em vez de `mt5_orchestrator.status()` direto | 1h | Baixo |
| Adicionar coluna `synced_from_mt5_at TIMESTAMP` em `trades` para auditoria de cache vs truth | 1h | Baixo — migration aditiva |
| Testes: `test_truth_layer.py`, `test_validate_pre_send.py`, `test_truth_cache_ttl.py` | 4h | — |

**Resultado Fase 2:** todos os módulos leem de `vt_truth`. DB ainda é consultado, mas é fallback (não primário). Caches TTL funcionam.

### 4.3 FASE 3 — Consolidação (5-7 dias)

**Objetivo:** state vira projeção em RAM; intraday report vai direto ao MT5; DB vira cache de longo prazo.

| Task | Esforço | Risco |
|------|---------|-------|
| Substituir `/tmp/vt_autotrader_state.json` por `rebuild_state_from_mt5()` no startup. Remover `SessionState.save()` e `SessionState.load()`. | 3h | Alto — restart mid-day pode perder `halt_until`, `consecutive_losses`. Mitigação: ler do DB e migrar pra in-memory. |
| Mover `daily_pnl`, `trade_count`, `wins`, `losses` de `state` para `vt_truth` (calculados a cada chamada de `get_daily_pnl()`) | 2h | Médio |
| Refatorar `vt_daily_report.get_trades_report()` para buscar deals do MT5 history direto (não DB), usando `vt_truth.get_position_history()` | 3h | Médio — PnL do dia no Telegram vai mudar (passa a ser broker-truth, não DB-truth) |
| Adicionar TTL de 5min em queries do DB para `trades` com `exit_time NOT NULL`: se entry_time > 5min, sempre re-busca do MT5 history (ou seja, DB vira "shadow" só pra trades > 5min) | 4h | Médio — mudanças no schema mental dos callers |
| Descontinuar `import_mt5_history()` em `vt_trade_log.py` (não é mais necessário — `vt_truth` lê MT5 direto) | 1h | Baixo |
| Remover `sync_fees_from_mt5()` (idem — fees vêm do deal MT5 direto) | 0.5h | Baixo |
| Manter `trades` no DB apenas para: (a) trades > 30 dias (MT5 history não vê), (b) tax report / IR | 2h | Baixo |
| Testes: `test_state_rebuild_from_mt5.py`, `test_intraday_uses_history_direct.py`, `test_db_ttl_5min.py` | 4h | — |

**Resultado Fase 3:** restart do autotrader é instantâneo (`rebuild_state_from_mt5()` lê MT5 em ~200ms, não precisa carregar JSON do disco). PnL intraday é broker-truth, sempre.

### 4.4 FASE 4 — Observabilidade (3-5 dias, pode ser paralelo a Fase 3)

**Objetivo:** detectar drift entre DB cache e MT5 truth automaticamente; alertas quantitativos.

| Task | Esforço | Risco |
|------|---------|-------|
| Adicionar snapshot diário de MT5 → DB (cron 16:50, após EOD): para cada trade do dia, UPDATE com PnL real do broker, gravar `mt5_snapshot_at` | 2h | Baixo |
| Estender `vt_trade_watchdog.py` (cron 2min) com check de drift: para cada trade do DB com `exit_time NOT NULL` e `entry_time >= today`, calcular PnL via `vt_truth.get_position_history()` e comparar com `net_pnl` no DB | 3h | Médio — performance (history() por trade é caro) |
| Alertar via Telegram se drift absoluto > R$5/dia OU drift relativo > 1% | 1h | Baixo |
| Métrica de "drift cents" no `vt_watchdog_status.json`: campo `drift_cents_total`, `drift_cents_max_single_trade` | 1h | Baixo |
| Painel `dashboard/` lê `vt_watchdog_status.json` e mostra histórico de drift (últimos 7 dias) | 4h | Baixo |
| Testes: `test_watchdog_drift_detection.py`, `test_snapshot_job.py` | 2h | — |

**Resultado Fase 4:** se DB cache divergir do MT5 por qualquer motivo (bug, race, restart mid-write), watchdog detecta em ≤2min e alerta.

---

## 5. Contrato `get_*` (python pseudo-code)

Estes são os contratos que `core/vt_truth.py` deve cumprir. Todos os módulos passam a consumir só esta interface.

```python
# core/vt_truth.py — contrato público

from decimal import Decimal
from datetime import datetime
from typing import Optional, List, NamedTuple


class Position(NamedTuple):
    """Posição aberta no MT5 (autoritativa)."""
    ticket: str            # string para evitar int64 overflow no JSON
    symbol: str            # "WDOQ26", "WINQ26", etc.
    direction: str         # "BUY" | "SELL"
    volume: float          # contratos
    entry_price: float     # preço de abertura no broker
    current_price: float   # último tick
    sl: float              # stop loss atual (0 se não tiver)
    tp: float              # take profit atual (0 se não tiver)
    profit_pts: float      # (current - entry) com sinal
    profit_brl: float      # profit em R$ com multiplicador aplicado
    time_open: datetime    # timestamp do open
    magic: int             # magic number (555501 = nosso)
    comment: str           # "VibeTrading" se nosso
    source: str = "MT5"    # sempre "MT5" — truth


class Deal(NamedTuple):
    """Deal MT5 (entrada ou saída)."""
    ticket: str
    position_id: str       # = entry_ticket do trade
    symbol: str
    type: str              # "BUY" | "SELL" (direção do deal, não da posição)
    volume: float
    price: float
    profit: float          # PnL bruto (R$, antes de fees)
    commission: float      # comissão (negativa)
    swap: float            # swap (pode ser + ou -)
    net_pnl: float         # profit + commission + swap (broker-truth)
    time: datetime
    magic: int
    reason: str            # "SL", "TP", "MANUAL", etc.
    entry_or_exit: str     # "IN" | "OUT"


# ============== READS ==============

def get_open_positions(
    *,
    symbol: Optional[str] = None,
    magic: Optional[int] = 555501,
    use_cache: bool = True,
) -> List[Position]:
    """
    FONTE AUTORITATIVA para posições abertas.
    Lê mt5.status() (com cache 1s).
    Filtra por magic (default 555501 = nosso) e symbol opcional.
    Falha: retorna [] (NUNCA levanta). Loga erro.
    """


def get_position_history(
    symbol: str,
    *,
    since_iso: Optional[str] = None,   # default: 7 dias atrás
    until_iso: Optional[str] = None,   # default: agora
    use_cache: bool = True,
) -> List[Deal]:
    """
    Deals de um símbolo no intervalo. Lê mt5.history() (cache 30s).
    Inclui deals IN e OUT — caller filtra se precisar.
    """


def get_daily_pnl(
    date_iso: Optional[str] = None,    # default: hoje
    *,
    use_cache: bool = False,           # relatórios SEMPRE invalidam
) -> Decimal:
    """
    PnL líquido do dia em centavos, somando net_pnl de todos os
    deals OUT de hoje. Lê MT5 history direto (NÃO consulta DB).
    Retorna Decimal('123.45') — usar Decimal para evitar float drift.
    Custo: 1 chamada history(symbol=None, days=1) ≈ 200ms.
    """


def get_account_snapshot(use_cache: bool = True) -> dict:
    """
    {balance, equity, margin_free, margin_level, currency}.
    Lê mt5.status()["account"] (cache 2s).
    """


# ============== PRE-CONDITION ==============

def validate_order_pre_send(
    symbol: str,
    direction: str,
) -> bool:
    """
    True se PODE enviar nova ordem; False se deve bloquear.
    Bloqueia se:
      - MT5 indisponível (conservador: melhor não abrir do que perder controle)
      - Já existe posição aberta no mesmo symbol+direction com magic nosso
    NÃO verifica:
      - daily_pnl, max_daily_trades, etc. (responsabilidade do autotrader)
    Custo: 1 chamada get_open_positions() (cache hit = 0ms).
    """


# ============== WRITE-THROUGH CACHE (DB) ==============

def reconcile_db_from_mt5(
    *,
    symbols: Optional[List[str]] = None,  # default: lido do CONFIG
    days: int = 2,                        # janela de history
) -> dict:
    """
    Sincroniza vt_trades.db (cache) com MT5 (truth).
    Idempotente: rodar N vezes = rodar 1 vez.
    Failure-safe: try/except em toda chamada externa.

    Returns:
        {
          "checked": N,             # trades do DB analisados
          "synced": M,              # trades com PnL atualizado
          "orphans_inserted": K,    # deals MT5 sem row no DB → INSERT
          "drift_pnl_cents": X,     # soma de |MT5_pnl - DB_pnl| em centavos
          "drift_count": Y,         # nº trades com drift > 0
          "errors": [...],
        }

    Estratégia (3 passos):
      1. Buscar deals do MT5 history (1 chamada por símbolo)
      2. Para cada deal OUT: se position_id não está no DB, INSERT;
         se está com exit_time NULL, UPDATE com PnL real
      3. Para cada trade do DB com exit_time NOT NULL: comparar
         net_pnl com deal MT5; se drift > R$0.01, log
    """


# ============== STARTUP ==============

def rebuild_state_from_mt5(state_obj) -> None:
    """
    Reconstroi state.positions in-memory a partir de MT5.
    Substitui SessionState.load() do /tmp/vt_autotrader_state.json.
    Mantém halt_until e consecutive_losses lendo do DB (se DB
    também migrou pra cá na Fase 3).

    state_obj: instância de SessionState (mutada in-place).
    """
```

---

## 6. Risco e Mitigação

| Risco | Probabilidade | Impacto | Mitigação |
|-------|---------------|---------|-----------|
| **Latência de MT5 (Wine subprocess ~200ms por chamada)** | Alta | Médio | Cache in-memory TTL 1-2s para `get_open_positions()` e `get_account_snapshot()`. `get_daily_pnl()` cache 5s com invalidação explícita em relatórios. Pattern: `pre→action→post` (3 calls) = ~600ms, aceitável dentro de tick de 30s. |
| **MT5 cair / Wine travar** | Média | Alto | Se `get_open_positions()` falhar 3x seguidas, autotrader entra em modo `HALT_MT5_DOWN` (já existe `halt_*` no config). Telegram alerta. Não envia novas ordens. Reconciliador tenta reconectar a cada 60s. |
| **`mt5.history()` só vê últimos 30 dias** | Certeza (limitação MT5) | Médio | DB retém trades > 30 dias (já retém). `reconcile_db_from_mt5(days=2)` cobre o intraday; relatório histórico (tax report, weekly report) lê do DB. Snapshot diário 16:50 grava `mt5_snapshot_at` em cada trade → permite auditoria retroativa. |
| **Cache invalidation bug estagna dados** | Média | Alto | TTL curto (1-2s) limita janela. Função `truth.invalidate_cache()` chamada explicitamente em eventos críticos (BUY, SELL, CLOSE, RECONCILE_END). Testes: `test_truth_cache_ttl.py` valida que `time.sleep(1.1)` força refresh. |
| **DB locked durante reconcile** | Alta (pytest/scripts paralelos) | Baixo | `reconcile_db_from_mt5()` failure-safe: `try/except OperationalError` em cada UPDATE, loga e segue. Próximo tick tenta de novo. Bot NÃO trava por DB. |
| **MT5 retorna `FILLED` mas ticket não aparece em `post_state`** | Baixa | Alto | Pattern `pre→action→post` detecta. Se `post_state` não tem o ticket esperado, `diff` falha, alerta via Telegram. Reconciliador investiga. |
| **Race entre `_execute_entry` e `reconcile_db_from_mt5`** | Média | Médio | FASE 3: `reconcile_db_from_mt5` lê `get_open_positions()` (truth) e só escreve no DB para tickets JÁ presentes em MT5. Se o bot inseriu um ticket há 100ms, o reconcile NÃO insere de novo. Verificado por `_synced_from_mt5_at` column. |
| **`/tmp/vt_autotrader_state.json` deixar de existir quebra restart** | Certeza (Fase 3) | Baixo | `rebuild_state_from_mt5()` substitui. `halt_until` e `consecutive_losses` migrados para DB na Fase 3. |
| **Wave 9-style reativação de símbolo (IND)** | Baixa (já tem `PERMANENTLY_DISABLED`) | Alto | FASE 2: `validate_order_pre_send()` rejeita ordens de símbolos em `PERMANENTLY_DISABLED` ANTES de chamar MT5. Defesa em profundidade. |
| **AGI/optimizer escrever config corrompido** | Média (2x em 1 dia) | Médio | `acquire_write_lock()` + whitelist em `vt_config_loader.py` (já existe). FASE 2: truth layer só LÊ config, nunca escreve. (Já respeita o mandato Bruno 2026-07-01.) |

---

## 7. Work Breakdown

### 7.1 Tasks, horas estimadas, marcos

| ID | Task | Fase | Horas | Marco |
|----|------|------|-------|-------|
| T01 | Criar `core/vt_truth.py` stub com `get_daily_pnl_truth()` | 1 | 2h | F1.1 |
| T02 | Refatorar `vt_daily_report` para usar `get_daily_pnl_truth()` | 1 | 1h | F1.2 |
| T03 | Estender `vt_trade_watchdog` com `history_deals_today()` | 1 | 1h | F1.3 |
| T04 | Testes Fase 1 (`test_intraday_uses_mt5_truth.py`) | 1 | 1h | F1.4 |
| T05 | Implementar `core/vt_truth.py` completo (5 funções) | 2 | 6h | F2.1 |
| T06 | Implementar `validate_order_pre_send()` | 2 | 2h | F2.2 |
| T07 | Refatorar `_execute_entry` para chamar `validate_order_pre_send()` | 2 | 2h | F2.3 |
| T08 | Refatorar `reconcile_positions_with_mt5` para usar `vt_truth.*` | 2 | 1h | F2.4 |
| T09 | Refatorar `_resolve_orphan_closes` para usar `vt_truth.*` | 2 | 1h | F2.5 |
| T10 | Refatorar `vt_trade_watchdog` para usar `vt_truth.*` | 2 | 1h | F2.6 |
| T11 | Migration: adicionar coluna `synced_from_mt5_at` em `trades` | 2 | 1h | F2.7 |
| T12 | Testes Fase 2 (truth layer, pre-send, cache TTL) | 2 | 4h | F2.8 |
| T13 | Implementar `rebuild_state_from_mt5()` | 3 | 3h | F3.1 |
| T14 | Remover `SessionState.save()` / `load()` (state vira in-memory) | 3 | 3h | F3.2 |
| T15 | Mover `daily_pnl/trade_count/wins/losses` para `vt_truth` | 3 | 2h | F3.3 |
| T16 | Refatorar `vt_daily_report` para `get_position_history()` direto | 3 | 3h | F3.4 |
| T17 | TTL 5min em queries do DB para trades fechados | 3 | 4h | F3.5 |
| T18 | Descontinuar `import_mt5_history()` e `sync_fees_from_mt5()` | 3 | 1.5h | F3.6 |
| T19 | Testes Fase 3 (state rebuild, intraday direto, DB TTL) | 3 | 4h | F3.7 |
| T20 | Snapshot diário MT5 → DB (cron 16:50) | 4 | 2h | F4.1 |
| T21 | Drift detection no watchdog (a cada 2min) | 4 | 3h | F4.2 |
| T22 | Alertas Telegram drift > R$5/dia | 4 | 1h | F4.3 |
| T23 | Métrica `drift_cents_*` no `vt_watchdog_status.json` | 4 | 1h | F4.4 |
| T24 | Painel dashboard com histórico de drift | 4 | 4h | F4.5 |
| T25 | Testes Fase 4 (drift detection, snapshot job) | 4 | 2h | F4.6 |
| **TOTAL** | | | **50.5h** | |

### 7.2 Marcos (milestones)

| Marco | Data alvo (desde 2026-07-01) | Critério de aceitação |
|-------|------------------------------|------------------------|
| **M1: PnL intraday confiável** | D+2 (2026-07-03) | Telegram intraday bate com extrato da corretora no centavo. Watchdog detecta drift. |
| **M2: Truth layer ativo** | D+7 (2026-07-08) | Todos os módulos leem de `vt_truth.*`. Zero chamadas diretas a `mt5_orchestrator.status()` / `history()` fora de `vt_truth`. |
| **M3: State 100% in-memory** | D+14 (2026-07-15) | Restart do autotrader = `rebuild_state_from_mt5()` em <500ms. `/tmp/vt_autotrader_state.json` removido. |
| **M4: Drift detection automatizado** | D+18 (2026-07-19) | Watchdog detecta drift DB↔MT5 em ≤2min. Alertas Telegram quando > R$5/dia. |

### 7.3 Riscos por task

| Task | Risco principal | Mitigação |
|------|-----------------|-----------|
| T05 (truth layer) | Cache TTL errado → dados estagnados | TTL conservador (1-2s); função `invalidate_cache()` exposta; testes de TTL |
| T13/T14 (state rebuild) | Perder `halt_until` no restart | Ler halt_until do DB na Fase 3 (já é persistido lá em `trades.notes` indiretamente) |
| T16 (intraday direto) | MT5 history() lento (~500ms) | Cache 5s; chamar 1x por relatório (não por trade) |
| T21 (drift detection) | history() por trade no watchdog = O(N×200ms) = invável | Agregar: 1 history() por símbolo, comparar todos os deals do símbolo com DB |
| T24 (dashboard) | Escopo cresce (UI bonita, etc.) | Limitar a 1 gráfico de drift + 1 tabela de últimos alertas. Resto fica para depois. |

---

## 8. Exemplos de Código

### 8.1 Abertura de trade — before / after

#### BEFORE (atual — `core/vt_autotrader.py:1463`)

```python
# Atual: chama safe_buy/sell direto, confia no retorno, persiste no DB depois
if direction == "BUY":
    result = safe_buy(symbol, _vol, sl_pts=sl_pts, strategy=strategy)
else:
    result = safe_sell(symbol, _vol, sl_pts=sl_pts, strategy=strategy)

if result.get("status") == "FILLED":
    ticket = result.get("ticket", "?")
    # ... validação LLM ...
    # ... monta state.positions[key] = {...} ...
    # Se QUALQUER passo acima falhar (DB locked, exception, JSON),
    # MT5 está aberto mas state+DB não sabem. → orphan.
    log_entry(...)  # pode falhar silenciosamente
```

**Problema:** se `log_entry()` falhar (DB locked, exception), MT5 tem a posição aberta mas o bot não tem registro. Próximo tick → bot pode abrir OUTRA posição se vier sinal da mesma direção (orphan #2069-style).

#### AFTER (proposto)

```python
# Proposto: pre→action→post→diff via truth layer
from core import vt_truth

def execute_entry_truth(symbol, direction, vol, sl_pts, **kwargs):
    # 1) PRE-CONDITION: MT5 tem posição aberta no mesmo symbol+direction?
    if not vt_truth.validate_order_pre_send(symbol, direction):
        log(f"🚫 [TRUTH] {symbol} {direction} bloqueado: já existe posição aberta no MT5")
        return {"status": "BLOCKED", "reason": "DUPLICATE_MT5"}

    # 2) ACTION
    action_fn = safe_buy if direction == "BUY" else safe_sell
    pre_pos = vt_truth.get_open_positions()
    result = action_fn(symbol, vol, sl_pts=sl_pts, strategy=kwargs.get("strategy"))

    if result.get("status") != "FILLED":
        return result  # MT5 rejeitou — sem divergência

    ticket = str(result.get("ticket", ""))

    # 3) POST-CONDITION
    time.sleep(0.3)  # MT5 propaga o fill
    post_pos = vt_truth.get_open_positions()
    mt5_tickets = {p.ticket for p in post_pos}
    if ticket not in mt5_tickets:
        log(f"⚠️ [TRUTH] MT5 disse FILLED mas ticket {ticket} não aparece em status()")
        return {"status": "DIVERGENT", "ticket": ticket, "pre": pre_pos, "post": post_pos}

    # 4) DIFF + CACHE WRITE-THROUGH
    diff = compute_position_diff(pre_pos, post_pos, result)
    enqueue_db_write(("INSERT", _build_trade_row(diff, result)))  # fire-and-forget

    # 5) STATE MIRROR (in-memory, reconstruído de MT5 a cada restart)
    update_state_mirror(post_pos)

    return {"status": "OK", "ticket": ticket, "diff": diff}
```

**Ganho:** se a chamada `safe_buy` retornar `FILLED` mas o ticket não aparecer no `get_open_positions()` da pós-condição, detectamos em ≤500ms. DB write é fire-and-forget: se falhar, próximo `reconcile_db_from_mt5()` corrige. Zero janela de orphan.

### 8.2 Intraday report — before / after

#### BEFORE (atual — `monitoring/vt_daily_report.py:64-77`)

```python
# Atual: tudo vem do DB
db = sqlite3.connect(str(DB_PATH))
trades = db.execute('''
    SELECT symbol, direction, ... net_pnl, exit_time ...
    FROM trades
    WHERE date(entry_time) = ?
    ORDER BY entry_time
''', (target_date,)).fetchall()

# Problema: net_pnl pode estar 0 porque log_exit falhou
# Problema: trade GHOST (com PnL=0 marcado por reconcile) é EXCLUÍDO do report
# Problema: se DB locked, db.execute levanta e trava o cron
```

#### AFTER (proposto)

```python
# Proposto: deals do MT5 history são a verdade; DB é só pra detalhe
from core import vt_truth
from decimal import Decimal

def get_daily_report_mt5_truth(target_date=None) -> dict:
    if target_date is None:
        target_date = date.today().isoformat()

    # 1) PnL: MT5 history (broker-truth, NUNCA DB)
    vt_truth.invalidate_cache()  # relatório sempre vê o fresh
    daily_pnl = vt_truth.get_daily_pnl(target_date)  # Decimal

    # 2) Trades: MT5 history deals do dia (com join com DB pra IR detail)
    symbols = CONFIG.get("symbols_resolved", [])
    all_deals = []
    for sym in symbols:
        deals = vt_truth.get_position_history(sym, since_iso=f"{target_date}T00:00:00",
                                                until_iso=f"{target_date}T23:59:59")
        all_deals.extend(deals)

    # 3) Stats: agregados em memória (Decimal-safe)
    n_trades = len([d for d in all_deals if d.entry_or_exit == "OUT"])
    wins = len([d for d in all_deals if d.entry_or_exit == "OUT" and d.net_pnl > 0])
    losses = n_trades - wins
    win_rate = (wins / n_trades * 100) if n_trades > 0 else 0

    # 4) DB é fallback SÓ pra detalhe IR (multiplier, strategy, fees_synced)
    #    se DB indisponível, report sai sem detalhe (não trava)
    details = _fetch_db_details(all_deals)  # try/except interno

    return {
        "date": target_date,
        "total_pnl_brl": daily_pnl,
        "n_trades": n_trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate,
        "deals": all_deals,
        "details": details,
        "source": "MT5_HISTORY",  # explícito: veio do broker
    }
```

**Ganho:** se `log_exit()` falhou hoje e o DB está com `net_pnl=0`, o intraday report vai mostrar o PnL real (vem do MT5). DB locked → report sai sem detalhe de IR, mas PnL está correto.

### 8.3 Reconciler — before / after

#### BEFORE (atual — três funções: `reconcile_positions_with_mt5` + `_resolve_orphan_closes` + `_persist_close_to_db`)

```python
# reconcile_positions_with_mt5() — chamado a cada tick (~30s)
# Problema 1: marca GHOST com PnL=0 se history() ainda não tem o deal
# Problema 2: usa status() direto, não cache
def reconcile_positions_with_mt5():
    mt5_status = status()  # 1 call ~200ms
    # ... 200 linhas de INSERT/UPDATE com PnL=0 quando não tem deal ...
    # ... state.positions[f"{symbol}_{tf}"] = {...} (state vira autoritativo de novo) ...

# _resolve_orphan_closes() — chamado a cada tick ANTES do reconcile
# Problema 1: depende de ordem (se rodar DEPOIS do reconcile, não recupera o PnL)
# Problema 2: faz 1 history(symbol) por symbol (N calls × ~200ms = até 2s por tick)
def _resolve_orphan_closes():
    # ... lê DB, lê MT5 status, lê history por symbol, faz UPDATE cirúrgico ...

# _persist_close_to_db() — em mt5_orchestrator.close() (manual)
# Problema 1: se DB locked, raises (mas é wrap em try/except no caller)
# Problema 2: só roda se bot chamou close() (não cobre server-side)
def _persist_close_to_db(symbol, details):
    # ... 100 linhas de INSERT orphan ou UPDATE existing ...
```

**Custo atual por tick:** 1× `status()` + N× `history(symbol)` = até 2-3s de chamadas Wine. Roda a cada 30s = até 10% do tick em I/O bloqueante.

#### AFTER (proposto — uma função: `reconcile_db_from_mt5`)

```python
# vt_truth.reconcile_db_from_mt5() — chamado a cada 60s (não 30s)
# Idempotente, write-through, failure-safe, NUNCA crasha o bot
def reconcile_db_from_mt5(*, symbols=None, days=2) -> dict:
    if symbols is None:
        symbols = CONFIG.get("symbols_resolved", [])

    stats = {"checked": 0, "synced": 0, "orphans_inserted": 0,
             "drift_pnl_cents": 0, "drift_count": 0, "errors": []}

    # 1) OPEN POSITIONS (cache 1s — 1 call ao MT5)
    try:
        open_pos = get_open_positions()  # usa cache
    except Exception as e:
        stats["errors"].append(f"open_pos:{e}")
        return stats

    # 2) DEALS POR SÍMBOLO (cache 30s — N calls agrupadas)
    deals_by_position = {}  # {position_id: deal}
    for sym in symbols:
        try:
            deals = get_position_history(sym, days=days)  # cache hit = 0ms
            for d in deals:
                if d.entry_or_exit == "OUT":
                    deals_by_position[d.position_id] = d
        except Exception as e:
            stats["errors"].append(f"history({sym}):{e}")
            continue

    # 3) SYNC DB (try/except por trade, NUNCA crasha)
    try:
        conn = _open_db_wal()
        open_trades = conn.execute("""
            SELECT id, entry_ticket, symbol, direction, net_pnl, exit_time
            FROM trades
            WHERE exit_time IS NULL
        """).fetchall()
    except Exception as e:
        stats["errors"].append(f"db_select:{e}")
        return stats

    stats["checked"] = len(open_trades)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for trade in open_trades:
        ticket = str(trade["entry_ticket"] or "")
        if not ticket:
            continue
        deal = deals_by_position.get(ticket)
        if deal is None:
            continue  # ainda aberto no MT5 (legítimo)

        # Drift detectado: MT5 fechou mas DB tem exit_time NULL
        try:
            conn.execute("""
                UPDATE trades SET
                    exit_time = ?,
                    exit_price = COALESCE(NULLIF(?, 0), entry_price),
                    net_pnl = ?,
                    gross_pnl = ?,
                    swap = ?,
                    exit_reason = ?,
                    close_source = 'MT5_TRUTH_RECONCILE',
                    synced_from_mt5_at = ?,
                    updated_at = datetime('now', 'localtime')
                WHERE id = ? AND exit_time IS NULL
            """, (now_str, deal.price, deal.net_pnl, deal.profit, deal.swap,
                  _infer_exit_reason(deal), now_str, trade["id"]))
            stats["synced"] += 1
        except sqlite3.OperationalError:
            stats["errors"].append(f"db_locked_trade_{trade['id']}")
            continue  # próximo tick
        except Exception as e:
            stats["errors"].append(f"update_{trade['id']}:{e}")
            continue

    # 4) ORPHAN INGESTION (deals MT5 sem trade no DB)
    open_tickets = {t["entry_ticket"] for t in open_trades if t["entry_ticket"]}
    for pos_id, deal in deals_by_position.items():
        if pos_id in open_tickets:
            continue
        # Deal órfão: MT5 fechou, mas não havia trade no DB
        try:
            conn.execute("""
                INSERT OR IGNORE INTO trades
                    (entry_ticket, symbol, direction, volume, entry_time,
                     entry_price, exit_time, exit_price, gross_pnl, swap,
                     net_pnl, exit_reason, close_source, synced_from_mt5_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (deal.position_id, deal.symbol, deal.type, deal.volume,
                  deal.time.isoformat(), deal.price, deal.time.isoformat(),
                  deal.price, deal.profit, deal.swap, deal.net_pnl,
                  _infer_exit_reason(deal), "MT5_TRUTH_ORPHAN", now_str))
            stats["orphans_inserted"] += 1
        except Exception as e:
            stats["errors"].append(f"orphan_insert_{pos_id}:{e}")

    conn.commit()
    conn.close()
    return stats
```

**Ganho:**
- **1 call `get_open_positions()`** (cache 1s) + **N× `get_position_history()`** (cache 30s) = 0-2 calls reais ao MT5 por reconcile, não 2-10.
- **Idempotente:** rodar 2× seguidas = mesmo resultado. Padrão `close_source='MT5_TRUTH_RECONCILE'` permite auditoria.
- **Failure-safe:** cada UPDATE é try/except, DB locked = skip + log, próximo tick tenta de novo.
- **Drift detection:** se `synced_from_mt5_at` for antigo (> 5min) E trade ainda tem `exit_time IS NULL` E MT5 não tem posição aberta = drift real → alerta Fase 4.

---

## 9. Resumo das Mudanças por Arquivo

| Arquivo | Mudança | Fase |
|---------|---------|------|
| `core/vt_truth.py` | **CRIAR** (NÃO EXISTE) | 1, 2, 3 |
| `core/vt_autotrader.py` | Refatorar `_execute_entry`, `reconcile_positions_with_mt5`, `_resolve_orphan_closes`, `run_daemon`, `recover_open_positions`, `SessionState` | 2, 3 |
| `core/vt_trade_log.py` | Descontinuar `import_mt5_history`, `sync_fees_from_mt5`; manter `log_entry`/`log_exit` como write-through helpers | 3 |
| `core/vt_history_reconcile.py` | Manter para Fase 1/2 (compat), marcar deprecated na Fase 3 | 3 |
| `core/vt_config_loader.py` | Sem mudança (whitelist já existe desde 2026-07-01) | — |
| `mt5/mt5_orchestrator.py` | Sem mudança (continua sendo a bridge Wine) | — |
| `mt5/mt5_executor.py` | Sem mudança | — |
| `monitoring/vt_daily_report.py` | Refatorar `get_trades_report` para usar `vt_truth.get_daily_pnl` e `get_position_history` | 1, 3 |
| `monitoring/vt_trade_watchdog.py` | Refatorar para `vt_truth.*`; adicionar drift detection | 2, 4 |
| `monitoring/vt_copilot.py` | Refatorar para `vt_truth.get_open_positions()` e `get_daily_pnl()` | 2 |
| `dashboard/` | Adicionar painel de drift (Fase 4) | 4 |
| `tests/` | Adicionar `test_truth_layer.py`, `test_intraday_uses_mt5_truth.py`, `test_rebuild_state.py`, `test_drift_detection.py` | 1-4 |
| `vt_trades.db` | Migration: adicionar coluna `synced_from_mt5_at TIMESTAMP` | 2 |
| `/tmp/vt_autotrader_state.json` | **REMOVER** (state vira in-memory, reconstruído de MT5) | 3 |

---

## 10. Apêndice — Decisões Abertas

Perguntas que precisam de resposta do Bruno antes de prosseguir:

1. **`daily_pnl` no Telegram intraday deve vir de MT5 history ou de soma de `trades.net_pnl`?**
   Trade-off: MT5 history é broker-truth mas só vê últimos 30 dias. Soma de `trades.net_pnl` é o que temos no DB (mas está mentindo). **Recomendação:** MT5 history (Fase 1).

2. **`/tmp/vt_autotrader_state.json` deve ser removido de vez (Fase 3) ou mantido como fallback?**
   Trade-off: remover é mais limpo mas perde `halt_until` se DB cair. Manter duplica informação. **Recomendação:** remover, mas ler `halt_until` do DB (`trades.notes` ou nova coluna).

3. **Watchdog drift threshold: R$5/dia fixo ou % do `daily_pnl`?**
   Trade-off: fixo é simples, % escala. **Recomendação:** R$5 OU 5% do `daily_pnl` (o que for maior).

4. **`validate_order_pre_send` deve falhar-fechado (bloqueia se MT5 indisponível) ou falhar-aberto (deixa passar)?**
   Trade-off: fechado é conservador (não abre se não tem certeza), aberto é permissivo (continua operando se MT5 tem hiccup). **Recomendação:** falhar-fechado por padrão, com override no config (`truth_strict: false`) para emergências.

5. **Quantos minutos de janela de cache no DB?**
   Trade-off: 5min (curto, mais queries) vs 30min (longo, mais drift). **Recomendação:** 5min para `trades.exit_time IS NOT NULL`, sem cache para `IS NULL` (sempre MT5).

---

## 11. Diagrama Visual do Fluxo (referência rápida)

> **Esta seção é o "one-pager" visual da arquitetura implementada.** Complementa o texto das
> Seções 2 (atual) e 3 (proposta) com um diagrama renderizável, a tabela de componentes críticos
> e o mapa de dependências real entre módulos (extraído dos `import` do código, não inventado).
> O diagrama completo interativo está em `data/diagrams/flow_end_to_end.html` (dark theme, SVG).

### 11.1 Diagrama — visão de 7 camadas + loop AGI

Renderização ASCII compacta (o HTML tem a versão completa com cores e setas direcionadas):

```
        ┌─────────────────────── 1. MT5 / WINE ───────────────────────┐
        │  MT5 Terminal · ~/.wine64 · Xvfb :99 · RPyC bridge :5001     │
        │  mt5/mt5_executor.py (Windows-side: buy/sell/modify/close)   │
        └────────────────────────────┬─────────────────────────────────┘
                                     │  JSON I/O via _run_wine()
        ┌────────────────────────────▼─────────────────────────────────┐
        │                     2. BRIDGE (mt5/)                          │
        │  mt5_orchestrator.py ──── mt5_error_recovery.py               │
        │  _persist_close_to_db()      safe_buy/safe_sell (retry×3)     │
        └────────────────────────────┬─────────────────────────────────┘
                                     │  broker-truth
        ┌────────────────────────────▼───────────┐   ┌──────────────────┐
        │        3. TRUTH LAYER (core/)           │   │  4. CONFIG        │
        │  core/vt_truth.py (583 linhas)          │◄──┤  vt_config.json   │
        │  get_open_positions / get_position_     │   │  (canônico,       │
        │  history / get_daily_pnl /              │   │   locked, whitelist│
        │  reconcile_db_position / validate_      │   │   writers)        │
        │  order_pre_send                         │   │  vt_config_loader │
        └────────────────────────────┬───────────┘   └─────────┬────────┘
                                     │ truth                    ▲ apply_changes
        ┌────────────────────────────▼───────────┐             │ (17h10)
        │       5. AUTOTRADER (core/)              │   ┌─────────┴────────┐
        │  vt_autotrader.py                        │   │  LOOP AGI 17:10   │
        │  _execute_entry · validate_order_pre_send│   │  agi_tuning_17h   │
        │  reconcile_positions_with_mt5            │◄──┤  regime→discover  │
        │  _resolve_orphan_closes                  │   │  →LLM gate→apply  │
        │  PERMANENTLY_DISABLED={'IND'}            │   └──────────────────┘
        │  vt_strategy_loader (plugins strategies/)│
        └──────────┬──────────────────┬────────────┘
                   │ write-through    │ projection
        ┌──────────▼─────────┐  ┌─────▼──────────────┐
        │ 6a. vt_trades.db   │  │ 6b. state.json      │
        │ SQLite WAL (cache) │  │ /tmp (projection-   │
        │ _persist_close     │  │  only, rebuild cada │
        └──────────┬─────────┘  │  restart)           │
                   │            └─────────────────────┘
        ┌──────────▼──────────────────────────────────────────────────┐
        │                  7. MONITORAMENTO (monitoring/)              │
        │ vt_copilot (10/12/15h) · vt_trade_watchdog (5min)            │
        │ vt_daily_report (16:50) · vt_pre_flight (08:55)              │
        └──────────────────────────────────────────────────────────────┘
```

Versão interativa: **`data/diagrams/flow_end_to_end.html`** — abre em qualquer navegador,
inclui legenda de cores por camada e tabela dos 7 crons com horários reais do `crontab.txt`.

### 11.2 Componentes críticos (SLA, frequência, owner)

| Componente | Arquivo | Frequência / SLA | Owner | Falha se cair |
|---|---|---|---|---|
| MT5 executor | `mt5/mt5_executor.py` | chamada síncrona (~200ms/op) | Bruno + Wine | sem truth nem ordens |
| Bridge orchestrator | `mt5/mt5_orchestrator.py` | por operação (buy/sell/close) | core | ordens não saem |
| Error recovery | `mt5/mt5_error_recovery.py` | retry×3, 500ms entre tentativas | core | ordens rejeitadas sem recovery |
| Truth layer | `core/vt_truth.py` | cache + refresh; chamada por tick (~30s) | core (canônico) | PnL/positions mentem |
| Autotrader loop | `core/vt_autotrader.py` | 1 tick ~30s durante pregão | core | sem trading |
| State projection | `/tmp/vt_autotrader_state.json` | rebuild no restart + projeção por tick | core (NO-OP persist) | state vazio até MT5 voltar |
| DB cache | `vt_trades.db` (WAL) | write-through a cada close | core/mt5 | PnL histórico perdido |
| Config canônico | `vt_config.json` (+ `.lock`) | leitura por tick; escrita só AGI 17h10 | AGI (whitelist) | params inconsistentes |
| Watchdog drift | `monitoring/vt_trade_watchdog.py` | cron 5 min; threshold R$5/dia | monitoring | drift não detectado |
| Copilot supervisor | `monitoring/vt_copilot.py` | cron 10/12/15h; reinicia se morto | monitoring | autotrader morto pós-15h |
| AGI otimizador | `optimization/agi_tuning_17h.py` | cron 17:10 seg-sex | optimization | sem evolução de params |

### 11.3 Mapa de dependências entre módulos (extraído dos `import`)

Seta `A -> B` significa "A importa B". Gerado pela análise estática do código atual
(commit `f944eab9`), não é design aspiracional.

```
optimization.agi_tuning_17h ─┬─> optimization.agi_bayesian_optimizer
                             ├─> optimization.agi_evidence_validator
                             ├─> optimization.agi_regime_classifier
                             ├─> optimization.agi_safety_validator
                             ├─> optimization.exhaustive_strategy_search
                             ├─> optimization.vt_forward_backtest
                             └─> core.vt_config_loader          (apply_changes grava config)

core.vt_truth ─────────────────> mt5.mt5_orchestrator           (única dependência: broker)

core.vt_autotrader ─┬─> core.vt_truth        (via mt5_orchestrator indireto)
                    ├─> core.vt_history_reconcile
                    ├─> core.vt_emergency
                    ├─> core.vt_config_loader
                    ├─> core.vt_strategy_loader
                    ├─> core.vt_trade_log
                    ├─> core.vt_order_validator_v2
                    ├─> mt5.mt5_orchestrator
                    └─> mt5.mt5_error_recovery

core.vt_emergency ──────> mt5.mt5_orchestrator + mt5.mt5_error_recovery
core.vt_history_reconcile ─> mt5.mt5_orchestrator + core.vt_trade_log

monitoring.vt_copilot ──────> core.vt_autotrader + core.vt_config_loader + mt5.mt5_orchestrator
monitoring.vt_trade_watchdog> core.vt_config_loader + mt5.mt5_orchestrator
monitoring.vt_daily_report ─> core.vt_autotrader + mt5.mt5_orchestrator
monitoring.vt_pre_flight ───> mt5 (conectividade)
```

**Conclusões do mapa:**
- `core/vt_truth.py` tem **uma única dependência** (`mt5_orchestrator`) — confirma que é a
  fronteira limpa broker→sistema projetada na Seção 3.2.
- `mt5_orchestrator.py` é o **hub central**: importado por truth, autotrader, emergency,
  history_reconcile, copilot, watchdog e daily_report. Qualquer mudança ali tem blast radius alto.
- `optimization/` é **folha isolada**: só toca `vt_config_loader` para gravar params — nunca
  importa `core/vt_autotrader` nem `mt5/` em runtime. AGI é desacoplada do trading loop.
- Nenhum módulo de `monitoring/` é importado por `core/` ou `mt5/` (dependência unidirecional
  core→monitoring), o que permite reiniciar crons sem afetar o autotrader.

### 11.4 Caminho crítico de uma ordem (trace end-to-end)

```
1. vt_strategy_loader gera sinal (WIN/WDO/BIT/WSP × M5/M15/M30/H1)
2. vt_autotrader._execute_entry()
   ├─ validate_order_pre_send()      [core/vt_truth.py:541]  ← bloqueia duplicata
   └─ mt5_error_recovery.safe_buy()  [retry×3]
3. mt5_orchestrator.buy()
   └─ _run_wine('buy', {...})        [JSON → mt5_executor.py Windows-side]
4. MT5 retorna ticket + retcode      ← LEI 4: só "aberta" se ticket>0
5. _persist_close_to_db()            [no close, write-through no vt_trades.db]
6. state.positions atualizado        [projeção, NÃO persistido em disco]

   ─── a cada tick (~30s) ───
7. _resolve_orphan_closes()          [backfill exit de ghosts]
8. reconcile_positions_with_mt5()    [ingere orphans / marca ghosts]
9. compute_pnl_drift()  [5min]       [alerta se MT5×DB > R$5]
```

---

**Fim do documento.** Aguardando aprovação do Bruno para iniciar Fase 1.
