# Shellgram — akses root Linux via Telegram

Shellgram menjalankan bot Telegram privat **di mesin Linux yang ingin diakses**. Bot menerima/mengirim file dan menjalankan perintah shell **sebagai root**.

## Keamanan dan batasan

- Shellgram dirancang berjalan sebagai **root**. Siapa pun yang menguasai akun Telegram dengan ID yang diizinkan mendapat **root penuh** di mesin ini. Lindungi token bot dan akun Telegram (aktifkan 2FA); jika bocor, revoke token via BotFather.
- Bot mengaktifkan Linux `NoNewPrivileges` dan tidak membutuhkan `sudo` — perintah `/run` sudah dieksekusi langsung sebagai root.
- Paket ini **tidak menginstal apa pun pada VM secara otomatis**, tidak meminta sudo, dan tidak berisi token rahasia.
- Fitur transfer bot dibatasi hingga 20 MiB per file.

## Instalasi cepat (satu perintah, sebagai root)

```sh
# di mesin Linux yang ingin diakses via Telegram:
unzip shellgram.zip -d ~/shellgram   # atau: git clone <repo> ~/shellgram
cd ~/shellgram
sudo ./install.sh
```

`install.sh` akan memandu semuanya: cek Python 3.10+, meminta token bot
(dari @BotFather) dan ID Telegram Anda (dari @userinfobot) lalu menyimpannya
di `bot.env` (chmod 600), menjalankan self-check, dan memasang service systemd
`shellgram` agar bot otomatis berjalan sebagai root (auto-start + auto-restart
saat gagal). Hanya Python 3.10+ dan pustaka standar yang diperlukan; tidak
perlu pip. Kirim `/status` ke bot untuk verifikasi.

Tanpa systemd, jalankan manual: `sudo ./run.sh` (atau via tmux:
`sudo tmux new -d -s shellgram './run.sh'`).

## Token Telegram dan polling

Bot menggunakan **long polling**. Satu token bot hanya boleh dipolling oleh satu proses pada satu waktu. Token bot yang saat ini dipakai bot Sandbox tidak boleh dijalankan bersamaan di VM ini: Telegram dapat mengembalikan konflik polling. Jika kedua mesin perlu aktif bersamaan, buat bot Telegram baru melalui BotFather dan gunakan token baru hanya di `bot.env` VM ini. Alternatifnya, hentikan proses lama sebelum memakai token yang sama.

## Manual: pasang service systemd sendiri

`install.sh` sudah melakukannya otomatis. Jika ingin manual, buat
`/etc/systemd/system/shellgram.service` berisi:

```ini
[Unit]
Description=Shellgram Telegram root access bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/shellgram
EnvironmentFile=/root/shellgram/bot.env
ExecStart=/usr/bin/python3 /root/shellgram/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

(Sesuaikan path bila folder instalasi bukan `/root/shellgram`.) Lalu:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now shellgram.service
```

Log: `journalctl -u shellgram -f`. Untuk menghentikan:
`sudo systemctl disable --now shellgram.service`.

## Perintah Telegram

- `/help`, `/status`, `/pwd`, `/ls [path]`
- `/upload [nama]`, lalu kirim lampiran; atau langsung kirim lampiran
- `/get <path>` / `/download <path>` — kirim file VM ke Telegram
- `/fetch <URL> [nama]` — unduh URL web publik
- `/run <command>` — jalankan perintah selama maksimal 60 detik sebagai root
- `/runbg <command>`, `/jobs`, `/joblog <id> [baris]`, `/stop <id>` — pekerjaan latar
- `/find <nama> [folder]`, `/info <path>`, `/hash <file>`, `/cat <path>`, `/disk [path]`, `/recent`, `/mkdir <path>`
- `/copy <sumber> <tujuan>`, `/move <sumber> <tujuan>`, `/rename <sumber> <nama_baru>`
- `/trash <file>`, `/restore <nama_trash>`

Gunakan tanda kutip pada path/nama yang mengandung spasi. Lampiran masuk disimpan di subfolder `Pictures`, `Videos`, `Documents`, `Music`, atau `Downloads` pada home akun bot.
