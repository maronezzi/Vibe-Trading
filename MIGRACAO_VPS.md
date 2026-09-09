# MIGRACAO_VPS.md — Runbook de migração Vibe-Trading → Hostinger KVM 2

> Autor: kit gerado em 2026-09 (Wave VPS-M1). Target: **Hostinger KVM 2** —
> 2 vCPU / 8 GB RAM / 100 GB NVMe, datacenter São Paulo, Ubuntu 24.04 LTS.
> Regra de ouro do Bruno: **sempre exatamente 1 sistema operando** — o cutover
> é um interruptor, nunca uma sobreposição.

---

## 1. Arquitetura da migração (resumo em 5 linhas)

| Artefato | Onde nasce | Onde roda | Como se move |
|---|---|---|---|
| Código + estratégias | **Local** (dev) | VPS | `20_sync_code_to_vps.sh` (deploy) |
| `vt_trades.db`, `vt_config.json`, relatórios | **VPS** (runtime) | VPS | Volta pelo espelho `50_mirror_pull` |
| Prefixes Wine (`~/.wine`, `~/.wine64`) | Local (1ª sync) | VPS | `20_sync --wine` (caminhos idênticos) |
| Hermes (gateway + jobs + credenciais) | Local hoje | **VPS pós-cutover** | `21_sync_hermes_to_vps.sh` |
| Espelho de segurança | — | Local `~/Backups/vps-mirror/` | Cron 19:40 no `crontab.local_dev.txt` |

Caminho do projeto é **idêntico nas duas máquinas** (`/home/bruno/Projects/Vibe-Trading`,
usuário `bruno`) porque o orquestrador tem esse path hardcoded — zero mudança de código.

## 2. Estudo da duplicidade — por que o Hermes é o ponto crítico

### 2.1 As DUAS camadas que podem duplicar

1. **Crontab do sistema** (`crontab.txt`): copilot 10:05/12:05/15:05, intraday 20min,
   AGI 12:00/17:10, self-heal, relatórios. Se dois hosts tiverem esse crontab ativo,
   o Telegram recebe tudo em dobro.
2. **Gateway Hermes** (`~/.hermes`): NÃO é só o CLI de envio. É um agente residente
   (`hermes gateway run`, PID vivo hoje) que:
   - **Faz polling do Telegram** (plataforma `connected`) — dois gateways com o mesmo
     token brigam pela polling ou processam as mesmas mensagens em dobro;
   - Mantém **scheduler interno próprio** (`~/.hermes/cron/jobs.json`) com **36 jobs**,
     VÁRIOS ativos e sobrepostos ao crontab do sistema.

### 2.2 Inventário dos jobs Hermes relevantes (levantado em 2026-09-02)

| Job | Agenda | Estado hoje | Sobreposição com crontab |
|---|---|---|---|
| Vibe-Trading Autotrader | 09:00 seg–sex | **ATIVO** | Sim (start_autotrader.sh 09:00) — idempotente, mas é 2ª camada |
| Vibe-Trading AGI v4 17h | 17:10 seg–sex | **ATIVO** | Sim (run_agi_v4_cron.sh 17:10) — horário idêntico |
| Vibe-Trading Otimização Meio-Dia | 12:30 seg–sex | **ATIVO** | Parcial (AGI 12:00) |
| vt-trade-watchdog | cada 2 min | **ATIVO** | Parcial (watchdog 5min do crontab) |
| vt-restart-when-flat | */5 9–16h | **ATIVO** | Não (exclusivo do hermes) |
| Inteligência Trader / Relatório Diário / Semanal / Pre-Flight / Intraday 20min | várias | pausados | — (o Intraday já foi pausado por duplicar relatório — precedente!) |
| fw-report-2026MMDD (≈10 jobs), Licenciamento, OLX, etc. | one-off/legado | ativos mas vencidos | Lixo a auditar pós-migração |

### 2.3 Decisão (estratégia adotada)

- **Gateway Hermes ÚNICO no mundo**: mora na **VPS** após o cutover. O cutover
  (40) para o gateway local e sobe o da VPS. Enquanto a VPS não estiver pronta,
  nada de gateway lá (o 30_validate falha se detectar). A conversa do Bruno pelo
  Telegram continua funcionando igual — quem responde passa a ser a VPS.
- **Crontab ÚNICO**: produção só na VPS. Local recebe `crontab.local_dev.txt`
  (espelho + rescan de drift, zero trading).
- **Jobs migram AS-IS** (`cron/jobs.json` inteiro): a migração replica o comportamento
  atual 1:1 — inclusive as camadas duplicadas que já existem hoje (hermes 17:10 +
  cron 17:10 rodam no mesmo host e convivem por idempotência). **Não é o momento de
  "arrumar" nada.** Pós-migração, fazer uma Wave de auditoria dos 36 jobs (pausar
  fw-report vencidos, licenciamento já executado, revisar watchdog 2min vs 5min).
- **`vt_hermes_helper` intocado**: `hermes send` é transport; com um host ativo não
  há como duplicar. Nenhuma mudança no código vivo da ordem/SL/validator.

## 3. Pré-requisitos (uma vez)

1. VPS Hostinger KVM 2, região **Brasil (São Paulo)**, Ubuntu 24.04, IP público.
2. Chave SSH: `ssh-keygen -t ed25519` (se não tiver) e exportar a pública no painel
   da Hostinger / via `ssh-copy-id root@IP`.
3. Alias SSH (a VPS vira `ssh vps` para todo o kit):
   ```ssh-config
   # ~/.ssh/config (local)
   Host vps
       HostName <IP_DA_VPS>
       User bruno
       IdentityFile ~/.ssh/id_ed25519
   ```
   *Nota: durante o provisionamento o user `bruno` ainda não tem chave — copiar a
   pública para `/root/.ssh/authorized_keys` inicialmente e rodar `10_provision_vps.sh`,
   que cria `bruno` e seu `authorized_keys`; depois trocar `User bruno`.*
4. Descobrir o **host do servidor MT5 da corretora** (jurnal do MT5 local / logs do
   orquestrador) e preencher `VT_BROKER_HOST` em `scripts/vps/vps.conf`.

## 4. Fases de execução

### Fase A — Provisionamento (≈20 min, roda na VPS)
```bash
scp -r scripts/vps root@vps:/root/vps-kit      # ou via alias
ssh root@vps 'bash /root/vps-kit/10_provision_vps.sh'
```
Instala pacotes, **WineHQ (i386+x64)**, Xvfb, swap 16 GB, usuário `bruno`, ufw só-SSH,
timezone São Paulo. Não inicia nada de trading.

### Fase B — Sync inicial (≈1ª vez pesada: Wine ~7 GB; hermes-agent ~1–2 GB)
```bash
bash scripts/vps/20_sync_code_to_vps.sh --wine          # código + prefixes Wine
bash scripts/vps/21_sync_hermes_to_vps.sh               # estado do Hermes (sem ligar nada)
ssh vps 'cd ~/.hermes/hermes-agent && python3.12 -m venv venv && venv/bin/pip install -e .'
ssh vps 'mkdir -p ~/.local/bin && printf "#!/usr/bin/env bash\nexec ~/.hermes/hermes-agent/venv/bin/hermes \"\$@\"\n" > ~/.local/bin/hermes && chmod +x ~/.local/bin/hermes'
```
(A VPS NÃO pode ligar o gateway ainda — briga de polling do Telegram com o local.)

### Fase C — Validação (dentro dos 30 dias de reembolso)
```bash
# 1) preencher VT_BROKER_HOST em scripts/vps/vps.conf
bash scripts/vps/30_validate_vps.sh
# 2) Xvfb + MT5 na VPS como unidades systemd (NUNCA nohup via ssh — o
#    logind mata processos da sessão quando a conexão fecha; foi a causa
#    do "MT5 morre em 2,5 s com exit code 0" do dia 2026-09-03):
ssh vps 'sudo systemd-run --unit=xvfb99 --property="Restart=on-failure" \
    /usr/bin/Xvfb :99 -screen 0 1920x1080x24 -ac'
ssh vps 'sudo systemd-run --unit=mt5-order --uid=bruno \
    --setenv=DISPLAY=:99 --setenv=WINEPREFIX=/home/bruno/.wine \
    --setenv=WINEDEBUG=-all --setenv=HOME=/home/bruno \
    /usr/bin/wine "/home/bruno/.wine/drive_c/Program Files/MetaTrader 5 Terminal/terminal64.exe" /portable'
# 3) conferir login/latência no jurnal (locale UTF-16 → iconv):
ssh vps 'iconv -f UTF-16LE -t UTF-8 ~/.wine/drive_c/"Program Files"/"MetaTrader 5 Terminal"/logs/$(date +%Y%m%d).log | grep -i authorized | tail -3'
# 4) semana de sombra: crontab de sombra com SÓ o forward walker
ssh vps 'crontab -' < scripts/vps/crontab.vps_shadow.txt
```
**Resultado real (2026-09-03)**: login XPMT5-DEMO OK, ping **3,28 ms** (access point
ótimo — local fica entre 6,2–7,9 ms), "trading enabled, **netting mode**",
sessões local+VPS coexistindo sem kick, snapshot de saldo gravado pelo import do
walker (R$ 1.000.734,96). Cadeia venv→orchestrator→wine→MT5→XP validada ponta a ponta.

### Fase D — Cutover (o interruptor; fora do pregão, posições zeradas)
```bash
bash scripts/vps/40_cutover_to_vps.sh --dry-run    # ler o plano
bash scripts/vps/40_cutover_to_vps.sh --execute    # aplica (pede MIGRAR)
# opcional: --execute --start-now para subir o daemon na hora (teste noturno)
```
Faz: backup do crontab local → crontab dev local → mata daemon/watcher/walker/MT5/
gateway local → delta final (código+config+DB+hermes) → instala crontab de produção
na VPS (+ @reboot p/ Xvfb, bridge e hermes) → sobe bridge+gateway na VPS → valida →
mensagem de teste no Telegram. **Rollback a qualquer momento:** `45_rollback_to_local.sh
--execute` (para a VPS, traz DB/config de volta, restaura crontab local do backup).

### Fase E — Pós-cutover (primeira semana)
- Conferir pregão inteiro: `/tmp/vt_autotrader.log`, self-heal, intraday reports no Telegram.
- Reboot-test na VPS (deve voltar sozinho via @reboot: Xvfb, bridge, hermes; daemon às 09:00).
- **Auditoria dos 36 jobs hermes** (Wave VPS-M2): pausar fw-report-*, licenciamento,
  decidir watchdog 2min vs 5min, documentar em `AGI_V4_NORMA.md`/AGENTS.md.
- **Achado pré-existente (herdado na cópia fiel)**: `scripts/start_mt5linux.sh` está
  stale — aponta para `~/.wine64/drive_c/Program Files/MetaTrader 5/terminal64.exe`,
  que NÃO existe nem no local (pasta `MetaTrader 5` do wine64 está vazia; o MT5 vivo
  roda no ~/.wine como "MetaTrader 5 Terminal"). Ou seja, a alavanca de recovery do
  pre-flight/self-heal para o bridge RPyC :5001 está quebrada HOJE no local também.
  Wave VPS-M3 deve decidir: reinstalar MT5 no wine64 ou atualizar o lever para o ~/.wine.
- Espelho: conferir `~/Backups/vps-mirror/` preenchendo às 19:40 (crontab dev local).
- Atualizar `AGENTS.md`/`CLAUDE.md` (host prod = VPS; local = dev) e o `crontab.txt`
  comentário de instalação.

## 5. Fluxo permanente depois da migração

1. **Desenvolver sempre no local** (testes, ruff, pytest focado, backtest).
2. **Deploy**: `bash scripts/vps/20_sync_code_to_vps.sh` (fora do pregão; `--with-config`
   só se mudou params de verdade). Código versionado: commit + push GitHub como hoje.
3. **Runtime** (DB, config da AGI, relatórios) pertence à VPS e **volta sozinho**
   19:40 seg–sex pelo espelho → `~/Backups/vps-mirror/` (cópia no computador +
   nuvem = regra atendida; GitHub mantém o espelho de código).
4. **Nunca** instalar o crontab de produção no local de novo sem rollback formal
   (`45`) — é o que garante o "1 sistema só".

## 6. Riscos e mitigadores

| Risco | Mitigação |
|---|---|
| 8 GB RAM apertados com AGI | swap 16 GB provisionado; AGI meio-dia já roda `VT_MAX_WORKERS=1`; monitorar no 30_validate |
| Latência inesperada VPS→corretora | gate no 30_validate (<30 ms) + semana de forward walker antes do cutover; 30 dias de reembolso |
| MT5 pede re-login/2FA na VPS | previsto na Fase C via `mt5_show.sh`; senha/2FA de conta de trading com Bruno |
| Dois gateways Telegram | 21/30/40 travam: gateway só na VPS, e só no passo final do cutover |
| rsync `--prune` apagando runtime da VPS | `--delete` nunca é default no deploy (20); só no espelho (50), onde é semântica correta |
| Divergência vt_config.json (dev vs AGI) | ownership único: VPS manda no config; `--with-config` é exceção consciente |
| Cron PATH restrito na VPS | kit replica PATHs absolutos do crontab.txt; start_autotrader já exporta `~/.local/bin` |
| **logind mata processos de sessão SSH** (nohup não protege de kill de cgroup) | Xvfb/MT5/hermes como unidades `systemd-run` (xvfb99, mt5-order, hermes-gateway); no cutover, @reboot do cron para reboot-safety (filhos do crond sobrevivem) |
| **Conflito de polling do Telegram (409)**: gateway novo sobe antes da sessão stale do gateway antigo expirar → 5 retries/200s e desiste com **exit 0** | Unidade `hermes-gateway.service` permanente com `Restart=always` (RestartSec=30) — reinicia até vencer. Lição 2026-09-03 08:35: cutover derrubou o gateway local e o VPS perdeu a corrida por segundos |
| **Gateway local ressuscita sozinho**: o desktop tinha `hermes-gateway.service` no `systemd --user` (enabled) — trazia o gateway de volta após cada pkill, roubando o polling do Telegram da VPS e deixando o chat mudo (2026-09-03 ~10:30, com 2 sessões de agente presas em draining) | `systemctl --user disable --now hermes-gateway.service` no LOCAL (feito 2026-09-03). Se um dia precisar de gateway no desktop p/ dev, NUNCA simultâneo ao da VPS |
| `start_mt5linux.sh` stale (MT5 wine64 inexistente) | **CORRIGIDO em 2026-09-03 (VPS-M3)**: wrapper systemd-aware (vivo → exit 0 rápido; morto → restart da unidade); legado preservado em `start_mt5linux_legacy.sh`; unidades permanentes em `scripts/vps/systemd/`, troca final pós-pregão com `scripts/vps/60_swap_units_pospregao.sh` |

## 7. Checklist rápido de comandos (cola)

```bash
bash scripts/vps/10_provision_vps.sh        # na VPS, root
bash scripts/vps/20_sync_code_to_vps.sh --wine
bash scripts/vps/21_sync_hermes_to_vps.sh
bash scripts/vps/30_validate_vps.sh
bash scripts/vps/40_cutover_to_vps.sh --dry-run
bash scripts/vps/40_cutover_to_vps.sh --execute
bash scripts/vps/45_rollback_to_local.sh --execute   # emergência
bash scripts/vps/50_mirror_pull_from_vps.sh          # espelho (cron 19:40)
```
