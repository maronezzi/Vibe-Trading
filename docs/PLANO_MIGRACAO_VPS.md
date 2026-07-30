# 🚀 Plano de Migração Vibe-Trading → VPS

**Data:** 2026-07-22
**Autor:** Hermes Agent + Bruno Maronezzi
**Status:** PLANO — aguardando aprovação

---

## 1. Motivação

| Problema atual (notebook) | Solução VPS |
|--------------------------|-------------|
| Reboot/queda de energia mata tudo | Uptime 99.9%+, UPS do datacenter |
| `/tmp` limpa no reboot → profit lock perdido | Path persistente `~/.local/state/vt/` |
| CPU compartilhada com desktop (Firefox, Gradle, etc.) | CPU dedicada pro bot |
| Otimizador satura 8 threads → thermal throttle | Recursos isolados |
| Latência doméstica ~20-40ms até B3 | Datacenter SP: ~5-15ms |
| Wine/MT5 instável com suspensão de tela | Headless 24/7 sem X real |

---

## 2. Requisitos da VPS

### Especificações mínimas

| Recurso | Mínimo | Recomendado |
|---------|--------|-------------|
| vCPU | 4 | 6-8 |
| RAM | 8 GB | 16 GB |
| SSD | 80 GB | 120 GB |
| OS | Ubuntu 22.04/24.04 LTS | Ubuntu 24.04 |
| Rede | 1 Gbps | 1 Gbps |
| Região | São Paulo (menor latência B3) | SP |

### Por que esses números

- Wine + MT5: ~2 GB RAM
- Python autotrader + monitoring: ~1 GB
- AGI v4 otimizador (2 workers): ~2 GB pico
- Xvfb + x11vnc: ~200 MB
- Hermes Agent: ~500 MB
- Sistema: ~1 GB
- **Total: ~7 GB → 8 GB mínimo, 16 GB confortável**

### Provedores com datacenter em SP

| Provedor | Plano | vCPU | RAM | SSD | Preço/mês | Latência B3 |
|----------|-------|------|-----|-----|-----------|-------------|
| **Oracle Cloud** | VM.Standard.A1.Flex (free) | 4 | 24 GB | 200 GB | **R$ 0** (free tier) | ~10ms |
| **DigitalOcean** | Regular Droplet (SAO) | 4 | 8 GB | 160 GB | ~US$ 48 (R$ 260) | ~8ms |
| **Hetzner** | CX42 (sem SP, Falkenstein) | 4 | 16 GB | 160 GB | €14 (R$ 85) | ~120ms |
| **Contabo** | VPS M (sem SP) | 6 | 16 GB | 400 GB | €10 (R$ 60) | ~150ms |
| **Vultr** | Cloud Compute (SAO) | 4 | 8 GB | 160 GB | US$ 48 (R$ 260) | ~8ms |

**Recomendação:** Oracle Cloud free tier (ARM A1, 4 vCPU, 24 GB) se disponível em SP.
Alternativa paga: Hetzner CX42 (melhor custo-benefício, latência 120ms é aceitável pra swing/M5+).

> ⚠️ **Nota sobre Oracle ARM:** Wine x86_64 roda via FEX-Emu ou box64 em ARM.
> Testar antes. Se não funcionar, usar Hetzner/DO (x86_64 nativo).

---

## 3. Arquitetura na VPS

```
┌─────────────────────────────────────────────────────────┐
│  VPS Ubuntu 24.04 (headless)                            │
│                                                         │
│  ┌─────────────┐    ┌──────────────────────────────┐   │
│  │  Xvfb :99   │◄───│  Wine64 (.wine64)            │   │
│  │  (virtual X)│    │  ├─ MT5 terminal64.exe       │   │
│  └─────────────┘    │  ├─ Python311 (Windows)      │   │
│                     │  └─ RPyC server :5001        │   │
│                     └──────────────────────────────┘   │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │  Python 3.12 (Linux)                            │   │
│  │  ├─ core/vt_autotrader.py (daemon)              │   │
│  │  ├─ monitoring/ (copilot, watchdog, self-heal)  │   │
│  │  ├─ optimization/agi_v4/ (cron 12:30 + 17:10)  │   │
│  │  └─ strategies/ (plugins)                       │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │  Hermes Agent (gateway → Telegram)              │   │
│  │  ├─ Cron: watchdog 3min, copilot 20min          │   │
│  │  └─ Otimização meio-dia (nice/ionice)           │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │  x11vnc :5999 (acesso visual Remmina)           │   │
│  │  systemd user service, Restart=always           │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  systemd user services (Linger=yes):                    │
│    xvfb.service → mt5-bridge.service → x11vnc-mt5      │
│    hermes-gateway.service                               │
│    autotrader.service (09:00-17:00 via timer)           │
└─────────────────────────────────────────────────────────┘
```

---

## 4. Checklist de Migração

### Fase 1: Provisionamento (30 min)

- [ ] Contratar VPS (Ubuntu 24.04, x86_64, SP ou mais próximo)
- [ ] Acesso SSH com chave (não senha)
- [ ] `sudo apt update && sudo apt upgrade -y`
- [ ] Criar usuário `bruno` com sudo
- [ ] `loginctl enable-linger bruno` (serviços user sem login)
- [ ] Firewall: liberar SSH (22) + VNC (5999) — restringir por IP se possível
- [ ] Swap: `sudo fallocate -l 4G /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`
- [ ] Timezone: `sudo timedatectl set-timezone America/Sao_Paulo`

### Fase 2: Wine + MT5 (45 min)

- [ ] Instalar Wine:
  ```bash
  sudo dpkg --add-architecture i386
  sudo apt install -y wine64 wine32 xvfb x11vnc
  # Ou Wine staging pra melhor compatibilidade:
  # sudo apt install -y winehq-staging
  ```
- [ ] Criar prefixo 64-bit:
  ```bash
  export WINEPREFIX=~/.wine64
  export WINEARCH=win64
  wineboot --init
  ```
- [ ] Instalar Python 3.11 Windows no Wine:
  ```bash
  # Baixar python-3.11.x-amd64.exe e instalar via wine
  wine python-3.11.9-amd64.exe /quiet InstallAllUsers=1
  ```
- [ ] Instalar MetaTrader5 no Wine:
  ```bash
  wine mt5setup.exe /auto
  ```
- [ ] Instalar pacote Python `MetaTrader5` no Python Windows:
  ```bash
  wine ~/.wine64/drive_c/Program\ Files/Python311/python.exe -m pip install MetaTrader5
  ```
- [ ] Configurar MT5: login, senha, servidor (via GUI ou config.ini)
- [ ] Testar conexão: `wine python.exe -c "import MetaTrader5; ..."`

### Fase 3: Python Linux + Projeto (20 min)

- [ ] Instalar Python 3.12:
  ```bash
  sudo apt install -y python3.12 python3.12-venv python3-pip
  ```
- [ ] Clonar/copiar projeto:
  ```bash
  # Opção A: git clone (se repo privado)
  git clone <repo-url> ~/Projects/Vibe-Trading

  # Opção B: rsync do notebook (mais rápido pra 3 GB)
  rsync -avz --exclude='.git' --exclude='__pycache__' \
    bruno@NOTEBOOK_IP:~/Projects/Vibe-Trading/ ~/Projects/Vibe-Trading/
  ```
- [ ] Criar venv e instalar deps:
  ```bash
  cd ~/Projects/Vibe-Trading
  python3.12 -m venv .venv
  source .venv/bin/activate
  pip install -e ".[dev]"
  ```
- [ ] Copiar DB:
  ```bash
  scp bruno@NOTEBOOK:~/Projects/Vibe-Trading/vt_trades.db ~/Projects/Vibe-Trading/
  ```
- [ ] Copiar `vt_config.json` atual
- [ ] **FIX: Profit Lock path** → mudar de `/tmp/vt_profit_lock.json` para
  `~/.local/state/vt/profit_lock.json` (persistente entre reboots)

### Fase 4: Serviços systemd (15 min)

- [ ] `xvfb.service`:
  ```ini
  [Unit]
  Description=Xvfb virtual display :99
  After=network.target

  [Service]
  ExecStart=/usr/bin/Xvfb :99 -screen 0 1920x1080x24 -ac
  Restart=always
  RestartSec=3

  [Install]
  WantedBy=default.target
  ```

- [ ] `mt5-bridge.service`:
  ```ini
  [Unit]
  Description=MT5 Wine bridge (RPyC :5001)
  After=xvfb.service
  Requires=xvfb.service

  [Service]
  Environment=WINEPREFIX=%h/.wine64
  Environment=WINEARCH=win64
  Environment=DISPLAY=:99
  Environment=WINEDEBUG=-all
  ExecStart=/bin/bash %h/Projects/Vibe-Trading/scripts/start_mt5linux.sh --background
  Restart=on-failure
  RestartSec=10

  [Install]
  WantedBy=default.target
  ```

- [ ] `x11vnc-mt5.service` (igual ao atual do notebook)

- [ ] `autotrader.service` + `autotrader.timer`:
  ```ini
  # autotrader.timer
  [Timer]
  OnCalendar=Mon..Fri 09:00
  Persistent=true

  [Install]
  WantedBy=timers.target
  ```
  ```ini
  # autotrader.service
  [Service]
  Type=oneshot
  WorkingDirectory=%h/Projects/Vibe-Trading
  ExecStart=/bin/bash scripts/start_autotrader.sh
  ```

- [ ] Habilitar tudo:
  ```bash
  systemctl --user daemon-reload
  systemctl --user enable --now xvfb mt5-bridge x11vnc-mt5
  systemctl --user enable autotrader.timer
  ```

### Fase 5: Cron do sistema (10 min)

- [ ] Instalar crontab:
  ```bash
  # Ajustar paths de /home/bruno/Projects → ~/Projects (mesmo user)
  crontab ~/Projects/Vibe-Trading/crontab.txt
  ```
- [ ] Verificar: `crontab -l`

### Fase 6: Hermes Agent (15 min)

- [ ] Instalar Hermes na VPS:
  ```bash
  # Conforme docs: https://hermes-agent.nousresearch.com/docs
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
  ```
- [ ] Configurar Telegram (mesmo bot token ou novo)
- [ ] Configurar provider LLM (Alibaba Token Plan)
- [ ] Recriar cron jobs Hermes:
  - Watchdog 3min
  - Copilot 20min (se não usar cron do sistema)
  - Otimização meio-dia 12:30
- [ ] Testar: `hermes send -t telegram "VPS online ✅"`

### Fase 7: Validação (30 min)

- [ ] MT5 conectado ao broker (verificar no VNC)
- [ ] Autotrader inicia e detecta símbolos
- [ ] Copilot reporta PnL correto
- [ ] Watchdog sem drift (MT5 = DB)
- [ ] VNC acessível via Remmina: `VPS_IP:5999`
- [ ] Profit lock persiste após `sudo reboot`
- [ ] Serviços voltam após reboot (Linger)
- [ ] Latência MT5: `wine python.exe -c "import MetaTrader5 as mt5; mt5.initialize(); print(mt5.terminal_info().ping_last)"`

### Fase 8: Cutover (dia de pregão, 08:30)

1. **Notebook (08:30):**
   - [ ] Parar autotrader: `pkill -f vt_autotrader.py`
   - [ ] Parar cron: `crontab -r` (ou comentar entradas)
   - [ ] Pausar Hermes cron jobs (watchdog, copilot)
   - [ ] Fechar posições abertas se houver (ou deixar VPS assumir)

2. **VPS (08:40):**
   - [ ] Confirmar MT5 conectado
   - [ ] Confirmar autotrader timer ativo
   - [ ] Confirmar Hermes cron ativo
   - [ ] Monitorar primeiro trade

3. **Notebook (pós-pregão):**
   - [ ] Desabilitar serviços de trading (manter como backup)
   - [ ] Manter VNC pra emergência

### Rollback (se algo der errado)

- Notebook continua com tudo instalado
- Reativar: `crontab ~/Projects/Vibe-Trading/crontab.txt` + `systemctl --user start xvfb mt5-bridge`
- DB na VPS pode ser copiada de volta: `scp VPS:~/Projects/Vibe-Trading/vt_trades.db .`

---

## 5. Mudanças de código necessárias

### 5.1 Profit Lock persistente (CRÍTICO)

**Arquivo:** `core/vt_profit_lock.py`

```python
# ANTES:
STATE_FILE = "/tmp/vt_profit_lock.json"

# DEPOIS:
import os
STATE_DIR = os.path.expanduser("~/.local/state/vt")
os.makedirs(STATE_DIR, exist_ok=True)
STATE_FILE = os.path.join(STATE_DIR, "profit_lock.json")
```

### 5.2 Paths hardcoded

Verificar e parametrizar:
- `mt5/mt5_orchestrator.py`: hardcoded `/home/bruno/Projects/Vibe-Trading`
- `scripts/start_autotrader.sh`: `PROJECT="/home/bruno/Projects/Vibe-Trading"`
- `crontab.txt`: paths absolutos

**Solução:** usar `$HOME/Projects/Vibe-Trading` ou variável `VT_PROJECT_ROOT`.

### 5.3 Wine prefix

- Notebook usa `.wine` (execução direta) + `.wine64` (bridge)
- VPS: unificar em `.wine64` (só o bridge RPyC é necessário)
- Remover dependência do prefixo `.wine` se possível

---

## 6. Segurança

| Item | Ação |
|------|------|
| SSH | Chave ed25519, sem senha, porta custom (opcional) |
| VNC 5999 | Restringir por IP (ufw) ou túnel SSH |
| MT5 credenciais | `config.ini` com permissão 600 |
| Telegram bot token | Hermes auth pool (não em texto plano) |
| Firewall | `ufw allow 22/tcp && ufw allow from SEU_IP to any port 5999` |
| Backups DB | Cron diário: `scp vt_trades.db backup@...` ou rclone |

---

## 7. Custos estimados

| Item | Custo |
|------|-------|
| VPS (Hetzner CX42) | ~R$ 85/mês |
| VPS (Oracle free) | R$ 0/mês |
| VPS (DO/Vultr SAO) | ~R$ 260/mês |
| Domínio (opcional, pra túnel) | ~R$ 40/ano |
| **Total mínimo** | **R$ 0-85/mês** |

---

## 8. Timeline

| Dia | Ação |
|-----|------|
| D+0 | Contratar VPS, provisionar (Fases 1-2) |
| D+1 | Instalar projeto + serviços (Fases 3-5) |
| D+2 | Hermes + validação (Fases 6-7) |
| D+3 | Cutover em pregão real (Fase 8) |
| D+4 a D+7 | Monitoramento intensivo |
| D+8 | Desativar notebook como primário |

---

## 9. Riscos e mitigação

| Risco | Probabilidade | Mitigação |
|-------|--------------|-----------|
| Wine não roda MT5 na VPS | Baixa | Testar antes do cutover; fallback: VPS Windows |
| Latência pior que notebook | Baixa (SP) | Escolher datacenter SP; medir ping antes |
| Broker bloqueia IP de datacenter | Muito baixa | B3 não bloqueia VPS (comum pra algotrading) |
| VPS cai durante pregão | Muito baixa | Self-heal + watchdog reiniciam; notebook como backup |
| Perda de dados no cutover | Baixa | DB copiada antes; notebook intacto |

---

## 10. Decisões pendentes (Bruno)

1. **Qual provedor?** Oracle free (ARM, testar Wine) vs Hetzner (x86, garantido) vs DO (SP, caro)
2. **Manter notebook como backup ativo?** (recomendo sim, pelo menos 1 mês)
3. **VNC com senha ou túnel SSH?** (túnel é mais seguro)
4. **Migrar Hermes inteiro ou só cron jobs?** (recomendo inteiro — Telegram + watchdog)

---

*Documento gerado em 2026-07-22. Atualizar conforme decisões.*
