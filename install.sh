#!/bin/bash
# Shellgram installer — pemasangan interaktif dalam satu perintah.
# Dirancang berjalan sebagai root: instalatur langsung mendapat akses root via Telegram.
set -eu

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"

info() { printf '\033[1;34m[i]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# 1. Wajib root — Shellgram memang dirancang berjalan sebagai root.
[ "$(id -u)" -eq 0 ] || die "Jalankan sebagai root: sudo ./install.sh"

# 2. Python 3.10+ (stdlib saja, tanpa pip).
command -v python3 >/dev/null 2>&1 || die "python3 tidak ditemukan."
PYVER="$(python3 -c 'import sys; print(sys.version_info[0]*100 + sys.version_info[1])')"
[ "$PYVER" -ge 310 ] || die "Butuh Python 3.10+ (terdeteksi: $(python3 --version 2>&1))."

# 3. bot.env — dipandu interaktif bila belum ada.
if [ -f "$INSTALL_DIR/bot.env" ]; then
    info "bot.env sudah ada — lewati pembuatan."
else
    info "Belum ada bot.env."
    info "Buat bot via @BotFather di Telegram untuk mendapat token,"
    info "lalu lihat ID Telegram Anda via @userinfobot."
    echo
    read -rsp "  TELEGRAM_BOT_TOKEN: " TOKEN; echo
    read -rp  "  AUTHORIZED_USER_ID (angka): " USERID; echo
    [[ "$TOKEN" =~ ^[0-9]{8,10}:[A-Za-z0-9_-]{35}$ ]] || die "Format token tidak valid (contoh: 123456789:AAH...35 karakter)."
    [[ "$USERID" =~ ^[0-9]{5,15}$ ]] || die "AUTHORIZED_USER_ID harus angka."
    printf 'TELEGRAM_BOT_TOKEN=%s\nAUTHORIZED_USER_ID=%s\n' "$TOKEN" "$USERID" > "$INSTALL_DIR/bot.env"
    chmod 600 "$INSTALL_DIR/bot.env"
    unset TOKEN USERID
    ok "bot.env dibuat (chmod 600)."
fi

# 4. Self-check bawaan bot.
info "Menjalankan self-check..."
set -a; . "$INSTALL_DIR/bot.env"; set +a
python3 "$INSTALL_DIR/bot.py" --self-check || die "Self-check gagal."
set +a

# 5. Pasang sebagai service systemd (atau beri petunjuk tmux bila tanpa systemd).
if [ -d /run/systemd/system ]; then
    info "systemd terdeteksi — memasang service shellgram..."
    cat > /etc/systemd/system/shellgram.service <<EOF
[Unit]
Description=Shellgram Telegram root access bot
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/bot.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 $INSTALL_DIR/bot.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF
    chmod 644 /etc/systemd/system/shellgram.service
    systemctl daemon-reload
    systemctl enable --now shellgram.service
    sleep 3
    if systemctl is-active --quiet shellgram.service; then
        ok "Service shellgram aktif dan auto-start."
    else
        die "Service gagal start — periksa: journalctl -u shellgram -e"
    fi
    info "Lihat log: journalctl -u shellgram -f"
else
    warn "systemd tidak terdeteksi di mesin ini."
    info "Jalankan manual:  cd $INSTALL_DIR && ./run.sh"
    info "Atau via tmux:   tmux new -d -s shellgram '$INSTALL_DIR/run.sh'"
fi

ok "Selesai. Kirim /status ke bot Telegram untuk verifikasi akses root."
