#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -f bot.env ]; then
    echo "File bot.env belum ada. Salin .env.example menjadi bot.env lalu isi token dan ID Telegram." >&2
    exit 2
fi
chmod 600 bot.env
set -a
. ./bot.env
set +a
exec python3 ./bot.py
