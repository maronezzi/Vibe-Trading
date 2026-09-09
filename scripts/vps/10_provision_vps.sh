#!/bin/bash
# =====================================================================
# Vibe-Trading — Fase A: provisionamento base da VPS (Hostinger KVM 2)
# Roda NA VPS (Ubuntu 24.04 LTS), como root: sudo bash 10_provision_vps.sh
#
# Instala: pacotes base, WineHQ (i386+x64), Xvfb, swap 16G, usuário bruno,
# firewall (só SSH). IDEMPOTENTE. NUNCA inicia processo de trading.
# O sync de código/MT5/Hermes é feito da máquina local pelos scripts 20/21.
# =====================================================================
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "ERRO: rode como root (sudo bash $0)"; exit 1; }

. /etc/os-release
echo "== Provisionando VPS: ${PRETTY_NAME} =="

# ── 1. Timezone B3 ────────────────────────────────────────────────────
timedatectl set-timezone America/Sao_Paulo 2>/dev/null || true

# ── 2. Pacotes base (cron roda com PATH restrito — tudo via apt/absoluto) ──
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    xvfb x11-utils sqlite3 git rsync jq cron tzdata locales \
    python3-venv python3-pip python3-dev build-essential \
    software-properties-common gnupg2 wget ca-certificates curl

# ── 3. WineHQ (stack usa wine 32-bit ~/.wine e wine64 ~/.wine64) ─────
if ! command -v wine >/dev/null 2>&1; then
    dpkg --add-architecture i386
    mkdir -pm755 /etc/apt/keyrings
    wget -qO /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key
    wget -qNP /etc/apt/sources.list.d/ \
        "https://dl.winehq.org/wine-builds/ubuntu/dists/${VERSION_CODENAME}/winehq-${VERSION_CODENAME}.sources"
    apt-get update -qq
    apt-get install -y -qq --install-recommends winehq-stable winbind
fi
wine --version || { echo "ERRO: wine não instalou"; exit 1; }

# ── 4. Usuário bruno (mesmo uid/caminho do host de origem) ────────────
if ! id bruno >/dev/null 2>&1; then
    adduser --disabled-password --gecos "Bruno Maronezzi" bruno
    usermod -aG sudo bruno
    echo "bruno ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/bruno
fi
install -d -o bruno -g bruno -m 755 /home/bruno/Projects

# ── 5. Swap 16G (a VPS tem 8 GB RAM — AGI/backtest precisa de swap) ───
SWAP_TOTAL=$(free -m | awk '/^Swap:/{print $2}')
if [[ ${SWAP_TOTAL:-0} -lt 14000 ]]; then
    if [[ ! -f /swapfile ]]; then
        fallocate -l 16G /swapfile
        chmod 600 /swapfile
        mkswap /swapfile
    fi
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    swapon -a || true
    # Pressão de swap conservadora: prefere RAM, usa swap sob carga (AGI).
    sysctl -qw vm.swappiness=20
    grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=20' >> /etc/sysctl.conf
fi

# ── 6. Firewall: somente SSH. Nada de portas de trading expostas ─────
#     (MT5/RPyC 5001/hermes são outbound; o bridge RPyC fica em localhost).
if command -v ufw >/dev/null 2>&1; then
    ufw allow OpenSSH >/dev/null 2>&1 || true
    yes | ufw enable >/dev/null 2>&1 || true
fi

# ── 7. SSH: permitir login do bruno por chave (sem senha) ─────────────
install -d -o bruno -g bruno -m 700 /home/bruno/.ssh
touch /home/bruno/.ssh/authorized_keys
chown bruno:bruno /home/bruno/.ssh/authorized_keys
chmod 600 /home/bruno/.ssh/authorized_keys

echo
echo "== Provisionamento OK =="
echo "Próximo passo (da máquina local): scripts/vps/20_sync_code_to_vps.sh --wine"
