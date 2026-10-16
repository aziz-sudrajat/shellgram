#!/usr/bin/env python3
"""Private Telegram polling bot using the current unprivileged Linux account."""
from __future__ import annotations

import http.client
import errno
import ctypes
import getpass
import hashlib
import ipaddress
import json
import logging
import os
import re
import select
import shlex
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from pathlib import Path

HOME = Path.home()
LINUX_USERNAME = getpass.getuser()
UPLOAD_DIR = HOME / 'telegram_uploads'
DOWNLOAD_DIR = HOME / 'telegram_downloads'
MEDIA_DIRS = {
    'photo': 'Pictures',
    'video': 'Videos',
    'document': 'Documents',
    'audio': 'Music',
    'other': 'Downloads',
}
STATE_DIR = HOME / '.local' / 'state' / 'shellgram'
OFFSET_FILE = STATE_DIR / 'offset'
TRASH_DIR = STATE_DIR / 'trash' / 'files'
TRASH_META_DIR = STATE_DIR / 'trash' / 'metadata'
JOB_LOG_DIR = STATE_DIR / 'jobs'
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_BACKGROUND_JOBS = 3
BACKGROUND_JOB_TIMEOUT = 60 * 60
BACKGROUND_JOB_HISTORY = 20
MAX_JOB_LOG_BYTES = 10 * 1024 * 1024
MAX_COMMAND_BYTES = 4096
MAX_CAPTURE_BYTES = 128 * 1024
MAX_REPLY_CHARS = 3700
MAX_CAT_BYTES = 48 * 1024
MAX_FIND_NODES = 5000
MAX_FIND_SECONDS = 5
POLL_SECONDS = 50
DOWNLOAD_TIMEOUT = 20
DOWNLOAD_DEADLINE = 120

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('telegram-access-bot')


class BotError(Exception):
    """An API or user-facing operation failed without exposing secrets."""


def is_authorized(message: dict, user_id: int) -> bool:
    sender = message.get('from') or {}
    chat = message.get('chat') or {}
    return sender.get('id') == user_id and chat.get('type') == 'private' and chat.get('id') == user_id


def safe_filename(value: str) -> str:
    value = str(value or 'download.bin').replace('\\', '/')
    value = value.rsplit('/', 1)[-1]
    value = ''.join(ch if ch.isprintable() and ch not in '\r\n\x00' else '_' for ch in value)
    value = value.strip(' .')[:160]
    return value or 'download.bin'


def enable_no_new_privs() -> None:
    """Disallow child commands from gaining privilege via setuid/file capabilities."""
    libc = ctypes.CDLL(None, use_errno=True)
    # Linux PR_SET_NO_NEW_PRIVS = 38; irreversible for this process.
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def read_no_new_privs() -> bool:
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('NoNewPrivs:'):
                return line.split(':', 1)[1].strip() == '1'
    except OSError:
        pass
    return False


def load_token() -> tuple[str, int]:
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        raise BotError('missing token')
    try:
        user_id = int(os.environ.get('AUTHORIZED_USER_ID', '0'))
    except ValueError:
        raise BotError('invalid authorized user') from None
    if user_id <= 0:
        raise BotError('invalid authorized user')
    return token, user_id


TOKEN = ''
AUTHORIZED_USER_ID = 0
API_ROOT = ''
FILE_ROOT = ''
UPLOAD_ARM_SECONDS = 300
PENDING_UPLOADS: dict[int, tuple[float, str | None]] = {}
BACKGROUND_JOBS: dict[str, dict] = {}


def parse_response(response) -> dict:
    try:
        payload = json.loads(response.read())
    except Exception:
        raise BotError('invalid Telegram API response') from None
    if not isinstance(payload, dict) or not payload.get('ok'):
        raise BotError('Telegram API rejected the request')
    return payload


def _is_retryable(exc: BaseException) -> bool:
    """Transient network failures worth one silent retry: the server or a
    proxy dropped an otherwise healthy connection mid-request."""
    if isinstance(exc, (http.client.RemoteDisconnected,
                        http.client.IncompleteRead,
                        TimeoutError, socket.timeout,
                        ConnectionError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return _is_retryable(reason) if isinstance(reason, BaseException) else False
    return False


def api_call(method: str, fields: dict | None = None, _retries: int = 2):
    body = urllib.parse.urlencode(fields or {}).encode('utf-8')
    request = urllib.request.Request(
        API_ROOT + method,
        data=body,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=65 if method == 'getUpdates' else 30) as response:
                return parse_response(response)['result']
        except BotError:
            raise
        except Exception as exc:
            if _is_retryable(exc) and attempt < _retries:
                attempt += 1
                log.warning('api_call %s transient failure (%s); retry %d/%d',
                            method, type(exc).__name__, attempt, _retries)
                time.sleep(min(2 ** attempt, 4))
                continue
            raise BotError(f'Telegram API unavailable ({type(exc).__name__})') from None


def send_text(chat_id: int, text: str) -> None:
    api_call('sendMessage', {
        'chat_id': str(chat_id),
        'text': str(text)[:MAX_REPLY_CHARS],
        'disable_web_page_preview': 'true',
    })


def unique_path(directory: Path, filename: str) -> Path:
    clean = safe_filename(filename)
    base = Path(clean)
    for index in range(10000):
        name = clean if index == 0 else f'{base.stem}_{index}{base.suffix}'
        candidate = directory / name
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            return candidate
        except FileExistsError:
            continue
    raise BotError('could not choose a unique filename')


def post_document(chat_id: int, file_path: Path) -> None:
    try:
        if file_path.stat().st_size > MAX_FILE_BYTES:
            raise BotError('file melebihi batas transfer 20 MiB')
        with file_path.open('rb') as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except BotError:
        raise
    except OSError:
        raise BotError('file tidak bisa dibaca') from None
    if len(data) > MAX_FILE_BYTES:
        raise BotError('file melebihi batas transfer 20 MiB')

    boundary = '----Shellgram' + uuid.uuid4().hex
    name = safe_filename(file_path.name).replace('"', '_')
    chunks = [
        (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n').encode(),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode(),
        data,
        f'\r\n--{boundary}--\r\n'.encode(),
    ]
    request = urllib.request.Request(
        API_ROOT + 'sendDocument',
        data=b''.join(chunks),
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            parse_response(response)
    except BotError:
        raise
    except Exception as exc:
        raise BotError(f'unggah ke Telegram gagal ({type(exc).__name__})') from None


def fetch_telegram_file(file_id: str, expected_size: int | None) -> bytes:
    if expected_size is not None and expected_size > MAX_FILE_BYTES:
        raise BotError('file melebihi batas transfer 20 MiB')
    info = api_call('getFile', {'file_id': file_id})
    remote_path = info.get('file_path')
    if not remote_path:
        raise BotError('Telegram tidak memberikan lokasi file')
    if info.get('file_size', 0) > MAX_FILE_BYTES:
        raise BotError('file melebihi batas transfer 20 MiB')
    url = FILE_ROOT + urllib.parse.quote(remote_path, safe='/')
    try:
        with urllib.request.urlopen(url, timeout=45) as response:
            data = response.read(MAX_FILE_BYTES + 1)
    except Exception as exc:
        raise BotError(f'unduhan dari Telegram gagal ({type(exc).__name__})') from None
    if len(data) > MAX_FILE_BYTES:
        raise BotError('file melebihi batas transfer 20 MiB')
    return data


def media_details(message: dict):
    for key in ('document', 'video', 'video_note', 'audio', 'voice', 'animation', 'sticker'):
        item = message.get(key)
        if isinstance(item, dict) and item.get('file_id'):
            message_id = message.get('message_id', 'telegram')
            if key == 'sticker':
                if item.get('is_video'):
                    return item, item.get('file_name') or f'sticker_{message_id}.webm', MEDIA_DIRS['video']
                if item.get('is_animated'):
                    return item, item.get('file_name') or f'sticker_{message_id}.tgs', MEDIA_DIRS['other']
                return item, item.get('file_name') or f'sticker_{message_id}.webp', MEDIA_DIRS['photo']
            fallback_ext = {'video': 'mp4', 'video_note': 'mp4', 'audio': 'bin', 'voice': 'ogg', 'animation': 'mp4'}
            filename = item.get('file_name') or f'{key}_{message_id}.{fallback_ext.get(key, "bin")}'
            mime = (item.get('mime_type') or '').lower()
            suffix = Path(filename).suffix.lower()
            if key in ('video', 'video_note', 'animation') or mime.startswith('video/') or suffix in ('.mp4', '.mkv', '.mov', '.webm', '.avi'):
                folder = MEDIA_DIRS['video']
            elif key in ('audio', 'voice') or mime.startswith('audio/') or suffix in ('.mp3', '.m4a', '.aac', '.wav', '.ogg', '.flac'):
                folder = MEDIA_DIRS['audio']
            elif key == 'document' and (mime.startswith('image/') or suffix in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.heic', '.tif', '.tiff')):
                folder = MEDIA_DIRS['photo']
            elif key == 'document':
                folder = MEDIA_DIRS['document']
            else:
                folder = MEDIA_DIRS['other']
            return item, filename, folder
    photos = message.get('photo')
    if isinstance(photos, list) and photos:
        item = photos[-1]
        if isinstance(item, dict) and item.get('file_id'):
            return item, f'photo_{message.get("message_id", "telegram")}.jpg', MEDIA_DIRS['photo']
    return None


def custom_upload_name(caption: str) -> str:
    try:
        parts = shlex.split(caption or '')
    except ValueError:
        return ''
    if len(parts) >= 2 and parts[0].lower() in ('/name', '/upload'):
        return ' '.join(parts[1:])
    return ''


def resolve_user_path(raw: str) -> Path:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1]
    if not raw:
        raise BotError('path belum diisi')
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = HOME / candidate
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise BotError('path tidak ditemukan atau tidak dapat diakses') from None


def resolve_user_file(raw: str) -> Path:
    path = resolve_user_path(raw)
    if not path.is_file():
        raise BotError('path bukan file biasa')
    return path


def parse_two_paths(raw: str, usage: str) -> tuple[str, str]:
    try:
        parts = shlex.split(raw)
    except ValueError:
        raise BotError(f'format: {usage}') from None
    if len(parts) != 2:
        raise BotError(f'format: {usage}')
    return parts[0], parts[1]


def resolve_destination(raw: str, source: Path, same_dir_for_name: bool = False) -> Path:
    raw = raw.strip()
    if not raw:
        raise BotError('path tujuan belum diisi')
    candidate = Path(raw).expanduser()
    if same_dir_for_name and not candidate.is_absolute() and '/' not in raw and '\\' not in raw:
        return source.parent / safe_filename(raw)
    if not candidate.is_absolute():
        candidate = HOME / candidate
    if candidate.name in ('', '.', '..'):
        raise BotError('nama tujuan tidak valid')
    try:
        if candidate.exists() and candidate.is_dir():
            return candidate.resolve(strict=True) / source.name
        parent = candidate.parent.resolve(strict=True)
        return parent / safe_filename(candidate.name)
    except (OSError, RuntimeError):
        raise BotError('folder tujuan tidak ditemukan atau tidak dapat diakses') from None


def copy_file_exclusive(source: Path, target: Path) -> int:
    if target.exists() or target.is_symlink():
        raise BotError('file tujuan sudah ada; tidak ada file yang ditimpa')
    source_stat = source.stat()
    if not stat.S_ISREG(source_stat.st_mode):
        raise BotError('hanya file biasa yang didukung')
    if shutil.disk_usage(target.parent).free < source_stat.st_size:
        raise BotError('ruang kosong tidak cukup untuk menyalin file')
    try:
        with source.open('rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        os.chmod(target, (stat.S_IMODE(source_stat.st_mode) & 0o700) | 0o600)
        return source_stat.st_size
    except FileExistsError:
        raise BotError('file tujuan sudah ada; tidak ada file yang ditimpa') from None
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def move_file_exclusive(source: Path, target: Path) -> int:
    if source == target:
        raise BotError('sumber dan tujuan sama')
    if target.exists() or target.is_symlink():
        raise BotError('file tujuan sudah ada; tidak ada file yang ditimpa')
    source_stat = source.stat()
    if not stat.S_ISREG(source_stat.st_mode):
        raise BotError('hanya file biasa yang didukung')
    try:
        os.link(source, target)
    except FileExistsError:
        raise BotError('file tujuan sudah ada; tidak ada file yang ditimpa') from None
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        copy_file_exclusive(source, target)
    try:
        source.unlink()
    except OSError:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return source_stat.st_size


def operate_file(raw: str, operation: str) -> str:
    usage = f'/{operation} <path_sumber> <path_tujuan>'
    source_raw, target_raw = parse_two_paths(raw, usage)
    source = resolve_user_file(source_raw)
    target = resolve_destination(target_raw, source, same_dir_for_name=(operation == 'rename'))
    if source == target:
        raise BotError('sumber dan tujuan sama')
    try:
        if operation == 'copy':
            size = copy_file_exclusive(source, target)
        else:
            size = move_file_exclusive(source, target)
    except BotError:
        raise
    except OSError as exc:
        raise BotError(f'operasi {operation} gagal ({exc.strerror or type(exc).__name__})') from None
    return f'{operation} selesai: {source} → {target} ({human_bytes(size)})'


def file_info(raw: str) -> str:
    path = resolve_user_path(raw)
    try:
        info = path.stat()
    except OSError as exc:
        raise BotError(f'gagal membaca metadata ({exc.strerror or type(exc).__name__})') from None
    kind = 'folder' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) else 'lainnya'
    modified = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(info.st_mtime))
    return (f'Path: {path}\nJenis: {kind}\nUkuran: {human_bytes(info.st_size)}\n'
            f'Izin: {stat.filemode(info.st_mode)}\nDiubah: {modified}')


def file_sha256(raw: str) -> str:
    path = resolve_user_file(raw)
    digest = hashlib.sha256()
    try:
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
    except OSError as exc:
        raise BotError(f'gagal menghitung SHA-256 ({exc.strerror or type(exc).__name__})') from None
    return f'SHA-256 {path.name}: {digest.hexdigest()}'


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise BotError('direktori data bot tidak aman')
    os.chmod(path, 0o700)


def unique_trash_target(filename: str) -> tuple[Path, Path]:
    base = Path(safe_filename(filename))
    for index in range(10000):
        name = base.name if index == 0 else f'{base.stem}_{index}{base.suffix}'
        target = TRASH_DIR / name
        metadata = TRASH_META_DIR / f'{name}.json'
        if not target.exists() and not target.is_symlink() and not metadata.exists():
            return target, metadata
    raise BotError('tidak dapat membuat nama unik di trash')


def trash_file(raw: str) -> str:
    source = resolve_user_file(raw)
    ensure_private_directory(TRASH_DIR)
    ensure_private_directory(TRASH_META_DIR)
    try:
        if source.is_relative_to(TRASH_DIR.resolve()):
            raise BotError('file sudah berada di trash bot')
    except OSError:
        raise BotError('path trash tidak dapat diperiksa') from None
    target, metadata_path = unique_trash_target(source.name)
    metadata = {
        'original_path': str(source),
        'original_name': source.name,
        'trash_name': target.name,
        'trashed_at': int(time.time()),
    }
    temp = metadata_path.with_name(f'.{metadata_path.name}.{uuid.uuid4().hex}.tmp')
    metadata_written = False
    moved = False
    try:
        with temp.open('x', encoding='utf-8') as stream:
            json.dump(metadata, stream)
        os.chmod(temp, 0o600)
        os.replace(temp, metadata_path)
        metadata_written = True
        move_file_exclusive(source, target)
        moved = True
    except Exception as exc:
        try:
            temp.unlink(missing_ok=True)
            if metadata_written and not moved:
                metadata_path.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, BotError):
            raise
        raise BotError(f'gagal memindahkan file ke trash ({type(exc).__name__})') from None
    return f'Dipindahkan ke trash (bisa dipulihkan): {target.name}\nPulihkan dengan /restore "{target.name}".'


def restore_file(raw: str) -> str:
    try:
        parts = shlex.split(raw)
    except ValueError:
        raise BotError('format: /restore <nama_file_di_trash>') from None
    if len(parts) != 1:
        raise BotError('format: /restore <nama_file_di_trash>')
    name = safe_filename(parts[0])
    trashed = TRASH_DIR / name
    metadata_path = TRASH_META_DIR / f'{name}.json'
    if not trashed.is_file() or not metadata_path.is_file():
        raise BotError('file atau catatan pemulihan tidak ditemukan di trash bot')
    try:
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        original = Path(metadata['original_path'])
        original_name = safe_filename(metadata['original_name'])
        if metadata.get('trash_name') != name or original.name != original_name:
            raise ValueError('invalid metadata')
        parent = original.parent.resolve(strict=True)
        target = parent / original_name
    except (OSError, ValueError, KeyError, TypeError):
        raise BotError('catatan pemulihan tidak valid atau folder asal tidak tersedia') from None
    if target.exists() or target.is_symlink():
        raise BotError(f'tujuan asal sudah ada; pindahkan dulu, lalu ulangi /restore "{name}"')
    size = move_file_exclusive(trashed, target)
    try:
        metadata_path.unlink()
    except OSError:
        return f'Dipulihkan: {target} ({human_bytes(size)}); catatan trash lama belum dapat dibersihkan.'
    return f'Dipulihkan: {target} ({human_bytes(size)})'


def resolve_user_dir(raw: str = '') -> Path:
    path = resolve_user_path(raw) if raw.strip() else HOME.resolve()
    if not path.is_dir():
        raise BotError('path bukan folder')
    return path


def run_shell(command: str, timeout_seconds: int = 60) -> tuple[int, str, bool]:
    command = command.strip()
    if not command:
        raise BotError('format: /run <command>')
    if len(command.encode('utf-8', 'replace')) > MAX_COMMAND_BYTES:
        raise BotError('command terlalu panjang (maksimum 4096 byte)')

    child_env = os.environ.copy()
    child_env.pop('TELEGRAM_BOT_TOKEN', None)
    child_env.pop('AUTHORIZED_USER_ID', None)
    try:
        proc = subprocess.Popen(
            ['/bin/bash', '-lc', command],
            cwd=str(HOME),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=True,
        )
    except Exception as exc:
        raise BotError(f'command tidak dapat dijalankan ({type(exc).__name__})') from None

    captured = bytearray()
    truncated = False
    timed_out = False
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            ready, _, _ = select.select([proc.stdout], [], [], min(0.25, remaining))
            if ready:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                room = MAX_CAPTURE_BYTES - len(captured)
                if room > 0:
                    captured.extend(chunk[:room])
                if len(chunk) > room:
                    truncated = True
            elif proc.poll() is not None:
                break
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=3)
    finally:
        if proc.stdout:
            proc.stdout.close()

    output = captured.decode('utf-8', errors='replace').strip()
    if truncated:
        output += '\n[output dipotong]'
    return proc.returncode if proc.returncode is not None else -1, output, timed_out


def terminate_process_group(proc: subprocess.Popen, grace_seconds: float = 2.0) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def reap_background_jobs() -> None:
    now = time.monotonic()
    for job in BACKGROUND_JOBS.values():
        proc = job['process']
        if proc.poll() is None and now >= job['deadline']:
            job['timed_out'] = True
            terminate_process_group(proc)
        if proc.poll() is not None and job.get('return_code') is None:
            job['return_code'] = proc.returncode
            job['finished_at'] = time.monotonic()
    finished = sorted(
        ((job_id, job) for job_id, job in BACKGROUND_JOBS.items() if job['process'].poll() is not None),
        key=lambda pair: pair[1].get('finished_at', pair[1]['started_at']),
    )
    for job_id, _job in finished[:-BACKGROUND_JOB_HISTORY]:
        BACKGROUND_JOBS.pop(job_id, None)


def drain_job_output(stream, log_path: Path) -> None:
    written = 0
    truncated = False
    try:
        with log_path.open('ab') as output:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                room = max(0, MAX_JOB_LOG_BYTES - written)
                if room:
                    output.write(chunk[:room])
                    written += min(len(chunk), room)
                if len(chunk) > room:
                    truncated = True
            if truncated:
                output.write(b'\n[log dipotong setelah batas 10 MiB; output berikutnya dibuang]\n')
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        log.warning('background job log write failed (%s)', type(exc).__name__)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def start_background_job(command: str) -> str:
    command = command.strip()
    if not command:
        raise BotError('format: /runbg <command>')
    if len(command.encode('utf-8', 'replace')) > MAX_COMMAND_BYTES:
        raise BotError('command terlalu panjang (maksimum 4096 byte)')
    reap_background_jobs()
    active = sum(job['process'].poll() is None for job in BACKGROUND_JOBS.values())
    if active >= MAX_BACKGROUND_JOBS:
        raise BotError(f'batas job berjalan tercapai ({MAX_BACKGROUND_JOBS}); gunakan /jobs atau /stop <id>')
    ensure_private_directory(JOB_LOG_DIR)
    job_id = uuid.uuid4().hex[:8]
    log_path = JOB_LOG_DIR / f'{job_id}.log'
    env = os.environ.copy()
    env.pop('TELEGRAM_BOT_TOKEN', None)
    env.pop('AUTHORIZED_USER_ID', None)
    env['PYTHONUNBUFFERED'] = '1'
    proc = None
    reader_thread = None
    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        proc = subprocess.Popen(
            ['/bin/bash', '-lc', command],
            cwd=str(HOME),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        if proc.stdout is None:
            raise OSError('stdout pipe unavailable')
        reader_thread = threading.Thread(target=drain_job_output, args=(proc.stdout, log_path), daemon=True)
        reader_thread.start()
    except Exception as exc:
        if proc is not None:
            terminate_process_group(proc)
            if proc.stdout:
                proc.stdout.close()
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise BotError(f'job tidak dapat dimulai ({type(exc).__name__})') from None
    started = time.monotonic()
    BACKGROUND_JOBS[job_id] = {
        'process': proc,
        'reader_thread': reader_thread,
        'log_path': log_path,
        'started_at': started,
        'deadline': started + BACKGROUND_JOB_TIMEOUT,
        'return_code': None,
        'finished_at': None,
        'timed_out': False,
        'stopped': False,
    }
    return f'Job {job_id} dimulai (maks. 60 menit). Log maks. 10 MiB: {log_path}\nPeriksa: /jobs atau /joblog {job_id}; hentikan: /stop {job_id}.'


def list_background_jobs() -> str:
    reap_background_jobs()
    if not BACKGROUND_JOBS:
        return 'Tidak ada job aktif atau tersimpan di sesi bot ini.'
    rows = ['Job latar:']
    for job_id, job in sorted(BACKGROUND_JOBS.items(), key=lambda pair: pair[1]['started_at'], reverse=True):
        proc = job['process']
        if proc.poll() is None:
            state = 'berjalan'
            elapsed = int(time.monotonic() - job['started_at'])
        elif job.get('timed_out'):
            state = 'timeout'
            elapsed = int(job.get('finished_at', time.monotonic()) - job['started_at'])
        elif job.get('stopped'):
            state = 'dihentikan'
            elapsed = int(job.get('finished_at', time.monotonic()) - job['started_at'])
        else:
            state = f'selesai exit={proc.returncode}'
            elapsed = int(job.get('finished_at', time.monotonic()) - job['started_at'])
        rows.append(f'{job_id}  {state}  {elapsed}s  {job["log_path"]}')
    return '\n'.join(rows)[:MAX_REPLY_CHARS]


def valid_job_id(value: str) -> bool:
    return len(value) == 8 and all(char in '0123456789abcdef' for char in value)


def stop_background_job(job_id: str) -> str:
    if not valid_job_id(job_id):
        raise BotError('ID job tidak valid; gunakan /jobs untuk melihat ID')
    job = BACKGROUND_JOBS.get(job_id)
    if not job:
        raise BotError('job tidak ditemukan pada sesi bot ini')
    proc = job['process']
    if proc.poll() is None:
        terminate_process_group(proc)
        job['stopped'] = not job.get('timed_out', False)
        job['return_code'] = proc.poll()
        job['finished_at'] = time.monotonic()
    return f'Job {job_id} dihentikan. Log: {job["log_path"]}'


def read_job_log(raw: str) -> str:
    try:
        parts = shlex.split(raw)
    except ValueError:
        raise BotError('format: /joblog <id> [jumlah_baris]') from None
    if len(parts) not in (1, 2) or not valid_job_id(parts[0]):
        raise BotError('format: /joblog <id> [jumlah_baris]')
    try:
        lines = int(parts[1]) if len(parts) == 2 else 30
    except ValueError:
        raise BotError('jumlah baris harus angka') from None
    if lines < 1 or lines > 50:
        raise BotError('jumlah baris harus 1 sampai 50')
    path = JOB_LOG_DIR / f'{parts[0]}.log'
    if path.is_symlink() or not path.is_file():
        raise BotError('log job tidak ditemukan')
    try:
        with path.open('r', encoding='utf-8', errors='replace') as stream:
            tail = ''.join(deque(stream, maxlen=lines)).strip()
    except OSError as exc:
        raise BotError(f'gagal membaca log ({exc.strerror or type(exc).__name__})') from None
    return (f'Log job {parts[0]} (baris terakhir):\n' + (tail or '(belum ada output)'))[:MAX_REPLY_CHARS]


def format_command_result(return_code: int, output: str, timed_out: bool) -> str:
    header = f'exit code: {return_code}'
    if timed_out:
        header += ' (timeout 60 detik; proses dihentikan)'
    return (header + '\n' + (output or '(tidak ada output)'))[:MAX_REPLY_CHARS]


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}' if unit != 'B' else f'{int(value)} B'
        value /= 1024
    return f'{size} B'


def list_directory(raw: str = '') -> str:
    directory = resolve_user_dir(raw)
    rows = []
    count = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if len(rows) >= 45:
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    kind = 'DIR ' if stat.S_ISDIR(info.st_mode) else 'FILE'
                    size = '-' if kind == 'DIR ' else human_bytes(info.st_size)
                    rows.append(f'{kind}  {size:>9}  {entry.name}')
                except OSError:
                    rows.append(f'????            {entry.name}')
    except OSError:
        raise BotError('folder tidak dapat dibaca') from None
    rows.sort(key=str.casefold)
    heading = f'Isi {directory} ({count} item):'
    if not rows:
        return heading + '\n(folder kosong)'
    suffix = f'\n... menampilkan 45 dari {count} item.' if count > 45 else ''
    return heading + '\n' + '\n'.join(rows) + suffix


def search_files(args: str) -> str:
    try:
        parts = shlex.split(args)
    except ValueError:
        raise BotError('format: /find <nama> [folder] — gunakan tanda kutip untuk path/nama berspasi') from None
    if not parts:
        raise BotError('format: /find <nama> [folder]')
    needle = parts[0].casefold()
    if len(parts) > 2:
        raise BotError('format: /find <nama> [folder] — gunakan tanda kutip untuk path berspasi')
    root = resolve_user_dir(parts[1] if len(parts) == 2 else '')
    started = time.monotonic()
    visited = 0
    matches = []
    stopped_early = False
    def ignore_walk_error(_error):
        return None
    for current, directories, files in os.walk(root, topdown=True, followlinks=False, onerror=ignore_walk_error):
        for name in directories + files:
            visited += 1
            if needle in name.casefold():
                matches.append(str(Path(current) / name))
                if len(matches) >= 30:
                    stopped_early = True
                    break
            if visited >= MAX_FIND_NODES or time.monotonic() - started > MAX_FIND_SECONDS:
                stopped_early = True
                break
        if stopped_early:
            break
    if not matches:
        return f'Tidak ada hasil untuk {needle!r} di {root}.' + ('\nPencarian dibatasi.' if stopped_early else '')
    result = f'Hasil pencarian di {root}:\n' + '\n'.join(matches)
    if stopped_early:
        result += '\n(Pencarian dihentikan pada batas 5.000 item / 5 detik / 30 hasil.)'
    return result


def read_text_file(raw: str) -> str:
    path = resolve_user_file(raw)
    try:
        if path.stat().st_size > MAX_CAT_BYTES:
            raise BotError('file terlalu besar untuk /cat (maksimum 48 KiB)')
        data = path.read_bytes()
    except BotError:
        raise
    except OSError:
        raise BotError('file tidak dapat dibaca') from None
    if b'\x00' in data:
        raise BotError('file tampaknya biner; gunakan /upload untuk mengirim file')
    text = data.decode('utf-8', errors='replace')
    return f'{path}:\n{text or "(file kosong)"}'[:MAX_REPLY_CHARS]


def disk_report(raw: str = '') -> str:
    path = resolve_user_dir(raw)
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        raise BotError('informasi kapasitas tidak dapat dibaca') from None
    return (f'Filesystem untuk {path}\n'
            f'Total: {human_bytes(usage.total)}\n'
            f'Terpakai: {human_bytes(usage.used)}\n'
            f'Tersedia: {human_bytes(usage.free)}')


def recent_files() -> str:
    candidates = []
    directories = (UPLOAD_DIR, DOWNLOAD_DIR, HOME / 'Pictures', HOME / 'Videos', HOME / 'Documents', HOME / 'Music', HOME / 'Downloads')
    for directory in directories:
        try:
            for entry in directory.iterdir():
                try:
                    if entry.is_file():
                        info = entry.stat()
                        candidates.append((info.st_mtime, entry, info.st_size))
                except OSError:
                    continue
        except OSError:
            continue
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return 'Belum ada file di folder upload/download bot.'
    rows = ['File terbaru:']
    for mtime, path, size in candidates[:10]:
        stamp = time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))
        rows.append(f'{stamp}  {human_bytes(size):>9}  {path}')
    return '\n'.join(rows)


def make_directory(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1]
    if not raw:
        raise BotError('format: /mkdir <path>')
    requested = Path(raw).expanduser()
    if not requested.is_absolute():
        requested = HOME / requested
    parent = requested.parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise BotError('folder induk tidak ditemukan atau tidak dapat diakses') from None
    if not resolved_parent.is_dir():
        raise BotError('folder induk bukan folder')
    target = resolved_parent / requested.name
    try:
        target.mkdir(mode=0o700)
    except FileExistsError:
        raise BotError('path sudah ada') from None
    except OSError:
        raise BotError('folder tidak dapat dibuat') from None
    return f'Folder dibuat: {target}'


def resolve_public_addresses(host: str, port: int):
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise BotError('host URL tidak dapat di-resolve') from None
    if not records:
        raise BotError('host URL tidak memiliki alamat IP')
    for family, socktype, proto, _canonname, sockaddr in records:
        try:
            ip = ipaddress.ip_address(sockaddr[0].split('%', 1)[0])
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
        except ValueError:
            raise BotError('alamat IP tujuan tidak valid') from None
        if not ip.is_global:
            raise BotError('URL hanya boleh mengarah ke host dengan IP publik')
    return records


def validate_download_url(url: str):
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise BotError('URL tidak valid') from None
    if scheme not in ('http', 'https') or not host:
        raise BotError('gunakan URL langsung berawalan http:// atau https://')
    if parsed.username is not None or parsed.password is not None:
        raise BotError('URL dengan username/password tidak didukung')
    expected_port = 443 if scheme == 'https' else 80
    port = port or expected_port
    if port != expected_port:
        raise BotError('hanya port web standar 80/443 yang didukung')
    resolve_public_addresses(host, port)
    return parsed


def connect_to_public_host(host: str, port: int, timeout):
    records = resolve_public_addresses(host, port)
    last_error = None
    for family, socktype, proto, _canonname, sockaddr in records:
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not None:
                sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise OSError('could not connect to public host') from last_error


class PublicHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        if self._tunnel_host:
            raise OSError('proxy tunnels are disabled')
        self.sock = connect_to_public_host(self.host, self.port, self.timeout)


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        if self._tunnel_host:
            raise OSError('proxy tunnels are disabled')
        raw_socket = connect_to_public_host(self.host, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


class PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(PublicHTTPConnection, request)


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(
            PublicHTTPSConnection,
            request,
            context=self._context,
        )


class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(request.full_url, newurl)
        old_scheme = urllib.parse.urlsplit(request.full_url).scheme.lower()
        parsed = validate_download_url(target)
        if old_scheme == 'https' and parsed.scheme.lower() != 'https':
            raise urllib.error.HTTPError(target, code, 'HTTPS downgrade blocked', headers, fp)
        return super().redirect_request(request, fp, code, msg, headers, target)


def build_public_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        PublicHTTPHandler(),
        PublicHTTPSHandler(),
        PublicRedirectHandler(),
    )


def download_url(url: str, requested_name: str = '') -> tuple[Path, int]:
    parsed = validate_download_url(url)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if DOWNLOAD_DIR.is_symlink() or not DOWNLOAD_DIR.is_dir():
        raise BotError('folder download tidak aman')
    os.chmod(DOWNLOAD_DIR, 0o700)

    request = urllib.request.Request(url, headers={
        'User-Agent': 'Shellgram/1.0',
        'Accept': '*/*',
    })
    opener = build_public_opener()
    target = None
    total = 0
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    try:
        with opener.open(request, timeout=DOWNLOAD_TIMEOUT) as response:
            validate_download_url(response.geturl())
            length = response.headers.get('Content-Length')
            if length:
                try:
                    if int(length) > MAX_FILE_BYTES:
                        raise BotError('file remote melebihi batas 20 MiB')
                except ValueError:
                    pass
            header_name = response.headers.get_filename() if hasattr(response.headers, 'get_filename') else None
            url_name = urllib.parse.unquote(Path(urllib.parse.urlsplit(response.geturl()).path).name)
            requested_name = requested_name.strip()
            if len(requested_name) >= 2 and requested_name[0] == requested_name[-1] and requested_name[0] in ('"', "'"):
                requested_name = requested_name[1:-1]
            filename = requested_name or header_name or url_name or 'download.bin'
            target = unique_path(DOWNLOAD_DIR, filename)
            with target.open('wb') as stream:
                while True:
                    if time.monotonic() > deadline:
                        raise BotError('unduhan melewati batas waktu 120 detik')
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise BotError('file remote melebihi batas 20 MiB')
                    stream.write(chunk)
    except BotError:
        if target:
            target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        if target:
            target.unlink(missing_ok=True)
        raise BotError(f'unduhan gagal ({type(exc).__name__}); periksa URL dan koneksi') from None
    return target, total


def register_command_menu() -> None:
    commands = [
        {'command': 'start', 'description': 'Mulai dan lihat petunjuk bot'},
        {'command': 'help', 'description': 'Daftar semua fitur'},
        {'command': 'status', 'description': 'Status akun dan batas transfer'},
        {'command': 'run', 'description': 'Jalankan perintah shell (penuh, sebagai root)'},
        {'command': 'runbg', 'description': 'Jalankan command latar dengan log'},
        {'command': 'jobs', 'description': 'Daftar job latar'},
        {'command': 'joblog', 'description': 'Baca log job'},
        {'command': 'stop', 'description': 'Hentikan job latar'},
        {'command': 'ls', 'description': 'Lihat isi folder'},
        {'command': 'pwd', 'description': 'Lihat folder kerja default'},
        {'command': 'get', 'description': 'Kirim file lokal ke Telegram'},
        {'command': 'download', 'description': 'Kirim file lokal melalui path'},
        {'command': 'upload', 'description': 'Siapkan satu upload; nama khusus opsional'},
        {'command': 'name', 'description': 'Nama khusus; gunakan pada caption lampiran'},
        {'command': 'fetch', 'description': 'Unduh URL publik ke komputer'},
        {'command': 'find', 'description': 'Cari nama file/folder'},
        {'command': 'info', 'description': 'Metadata file atau folder'},
        {'command': 'hash', 'description': 'Hitung SHA-256 file'},
        {'command': 'copy', 'description': 'Salin file tanpa menimpa'},
        {'command': 'move', 'description': 'Pindahkan file tanpa menimpa'},
        {'command': 'rename', 'description': 'Ganti nama file tanpa menimpa'},
        {'command': 'trash', 'description': 'Pindahkan file ke trash pemulihan'},
        {'command': 'restore', 'description': 'Pulihkan file dari trash bot'},
        {'command': 'cat', 'description': 'Baca file teks kecil'},
        {'command': 'disk', 'description': 'Lihat kapasitas penyimpanan'},
        {'command': 'recent', 'description': 'Lihat file terbaru'},
        {'command': 'mkdir', 'description': 'Buat folder'},
    ]
    api_call('setMyCommands', {'commands': json.dumps(commands)})


def handle_command(chat_id: int, text: str) -> None:
    parts = text.strip().split(maxsplit=1)
    head = parts[0] if parts else ''
    command = head.split('@', 1)[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ''

    if command in ('/start', '/help'):
        send_text(chat_id,
            f'Bot privat untuk akun Linux {LINUX_USERNAME}. Daftar fitur:\n'
            '/run <command> — jalankan perintah shell sebagai root (maks. 60 detik).\n'
            '/runbg <command> — job latar dengan log; maks. 3 job, 60 menit per job.\n'
            '/jobs — daftar job. /joblog <id> [baris] — baca log. /stop <id> — hentikan job.\n'
            f'/ls [path] — daftar isi folder; default {HOME}.\n'
            '/pwd — folder kerja default command.\n'
            '/download <path> atau /get <path> — kirim file lokal ke chat.\n'
            '/upload [nama] — siapkan satu lampiran berikutnya (5 menit); nama opsional. Tanpa /upload pun lampiran langsung diterima.\n'
            'Nama asli dipertahankan; untuk nama khusus, beri caption: /name "nama baru.ext".\n'
            '/name <nama> — pakai sebagai caption file untuk menetapkan nama khusus.\n'
            f'/fetch <URL> [nama] — unduh URL publik ke {DOWNLOAD_DIR}.\n'
            '/find <nama> [folder] — cari file/folder (maks. 5 detik, 5.000 item).\n'
            '/info <path> — metadata. /hash <file> — SHA-256.\n'
            '/copy <sumber> <tujuan>, /move <sumber> <tujuan>, /rename <sumber> <nama_baru>.\n'
            '/trash <file> — pindah ke trash yang bisa dipulihkan. /restore <nama_trash>.\n'
            '/cat <path> — tampilkan file teks kecil (maks. 48 KiB).\n'
            '/disk [path] — kapasitas filesystem.\n'
            '/recent — file terbaru di folder upload/download.\n'
            '/mkdir <path> — buat satu folder. /status — status bot.\n'
            'Folder tujuan: Pictures, Videos, Documents, Music, atau Downloads sesuai jenis media.\n'
            'Batas transfer 20 MiB per file. Gunakan kutip untuk path yang mengandung spasi.')
        return

    if command == '/status':
        send_text(chat_id,
            f'Berjalan sebagai UID {os.geteuid()} ({LINUX_USERNAME}); NoNewPrivs={int(read_no_new_privs())}.\n'
            'Perintah berjalan sebagai root dengan hak penuh.\n'
            'Lampiran masuk: Pictures/Videos/Documents/Music/Downloads.\n'
            f'Unduhan URL: {DOWNLOAD_DIR}\nBatas transfer: 20 MiB.')
        return

    if command == '/pwd':
        send_text(chat_id, f'Folder kerja default: {HOME}')
        return

    if command == '/ls':
        try:
            send_text(chat_id, list_directory(args))
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    if command == '/run':
        try:
            code, output, timed_out = run_shell(args)
            send_text(chat_id, format_command_result(code, output, timed_out))
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    if command == '/runbg':
        try:
            send_text(chat_id, start_background_job(args))
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    if command == '/jobs':
        send_text(chat_id, list_background_jobs())
        return

    if command == '/joblog':
        try:
            send_text(chat_id, read_job_log(args))
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    if command == '/stop':
        try:
            parts = shlex.split(args)
            if len(parts) != 1:
                raise BotError('format: /stop <id>')
            send_text(chat_id, stop_background_job(parts[0]))
        except (BotError, ValueError) as exc:
            send_text(chat_id, str(exc) or 'format: /stop <id>')
        return

    if command in ('/get', '/download'):
        try:
            path = resolve_user_file(args)
            post_document(chat_id, path)
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    if command == '/upload':
        try:
            name_parts = shlex.split(args) if args else []
        except ValueError:
            send_text(chat_id, 'Format: /upload [nama_file] — gunakan kutip untuk nama yang mengandung spasi.')
            return
        requested_name = safe_filename(' '.join(name_parts)) if name_parts else None
        PENDING_UPLOADS[AUTHORIZED_USER_ID] = (time.monotonic() + UPLOAD_ARM_SECONDS, requested_name)
        naming = f'Nama khusus: {requested_name}. ' if requested_name else 'Nama asli dipertahankan. '
        send_text(chat_id, f'/upload aktif untuk satu lampiran selama 5 menit. {naming}Kirim foto/video/dokumen/audio; bot menyimpan ke folder sesuai jenisnya.')
        return

    if command == '/name':
        send_text(chat_id, 'Untuk mengganti nama file, kirim lampiran dengan caption: /name "nama baru.ext".')
        return

    if command == '/fetch':
        parts = args.split(maxsplit=1)
        if not parts:
            send_text(chat_id, 'Format: /fetch <URL> [nama_file] — URL harus HTTP(S) publik.')
            return
        try:
            path, size = download_url(parts[0], parts[1] if len(parts) > 1 else '')
            send_text(chat_id, f'Unduhan selesai: {path} ({human_bytes(size)}).\nKirim ke chat dengan /download "{path}".')
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    if command == '/find':
        try:
            send_text(chat_id, search_files(args))
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    if command == '/info':
        try:
            send_text(chat_id, file_info(args))
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    if command == '/hash':
        try:
            send_text(chat_id, file_sha256(args))
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    if command in ('/copy', '/move', '/rename'):
        try:
            send_text(chat_id, operate_file(args, command[1:]))
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    if command == '/trash':
        try:
            send_text(chat_id, trash_file(args))
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    if command == '/restore':
        try:
            send_text(chat_id, restore_file(args))
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    if command == '/cat':
        try:
            send_text(chat_id, read_text_file(args))
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    if command == '/disk':
        try:
            send_text(chat_id, disk_report(args))
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    if command == '/recent':
        send_text(chat_id, recent_files())
        return

    if command == '/mkdir':
        try:
            send_text(chat_id, make_directory(args))
        except BotError as exc:
            send_text(chat_id, str(exc))
        return

    send_text(chat_id, 'Perintah tidak dikenal. Kirim /help untuk daftar fitur.')


def save_offset(offset: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = STATE_DIR / f'.offset.{os.getpid()}.tmp'
    temporary.write_text(str(offset), encoding='ascii')
    os.chmod(temporary, 0o600)
    os.replace(temporary, OFFSET_FILE)


def load_offset() -> int | None:
    try:
        value = int(OFFSET_FILE.read_text(encoding='ascii').strip())
        return value if value >= 0 else None
    except (OSError, ValueError):
        return None


def process_update(update: dict) -> None:
    message = update.get('message')
    if not isinstance(message, dict) or not is_authorized(message, AUTHORIZED_USER_ID):
        return
    chat_id = message['chat']['id']
    text = message.get('text') or ''
    if text:
        if text.startswith('/'):
            handle_command(chat_id, text)
        else:
            send_text(chat_id, 'Ketik /help untuk petunjuk. Gunakan /run sebelum command shell.')
        return

    media = media_details(message)
    if media:
        item, filename, folder_name = media
        custom_name = custom_upload_name(message.get('caption') or '')
        pending = PENDING_UPLOADS.pop(AUTHORIZED_USER_ID, None)
        if pending and pending[0] < time.monotonic():
            pending = None
        if not custom_name and pending:
            custom_name = pending[1] or ''
        if custom_name:
            filename = safe_filename(custom_name)
        data = fetch_telegram_file(item['file_id'], item.get('file_size'))
        target_dir = HOME / folder_name
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target_dir.is_symlink() or not target_dir.is_dir():
            raise BotError('folder tujuan tidak aman')
        target = unique_path(target_dir, filename)
        try:
            with target.open('wb') as stream:
                stream.write(data)
        except OSError:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise BotError('file diterima tetapi tidak dapat disimpan') from None
        saved_as = 'nama khusus' if custom_name else 'nama asli'
        send_text(chat_id, f'Disimpan di folder {folder_name} dengan {saved_as}: {target} ({human_bytes(len(data))})')


def main() -> int:
    global TOKEN, AUTHORIZED_USER_ID, API_ROOT, FILE_ROOT
    try:
        TOKEN, AUTHORIZED_USER_ID = load_token()
    except BotError as exc:
        log.error('configuration error: %s', exc)
        return 2
    if os.geteuid() == 0:
        # Shellgram is intended to run as root: the installer gets full root
        # command execution over Telegram. Guard your Telegram account and bot
        # token accordingly; anyone holding them has full root on this machine.
        log.warning('running as root; all Telegram commands execute with full privileges')
    try:
        enable_no_new_privs()
    except OSError as exc:
        log.error('refusing to run: cannot enable NoNewPrivileges (%s)', type(exc).__name__)
        return 2
    no_new_privs = read_no_new_privs()
    if not no_new_privs:
        log.error('refusing to run: NoNewPrivileges could not be verified')
        return 2
    if not HOME.is_dir():
        log.error('refusing to run: current Linux home directory is unavailable')
        return 2

    for directory in (UPLOAD_DIR, DOWNLOAD_DIR, STATE_DIR):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink() or not directory.is_dir():
            log.error('refusing to run: data path is not a directory')
            return 2
        os.chmod(directory, 0o700)

    API_ROOT = f'https://api.telegram.org/bot{TOKEN}/'
    FILE_ROOT = f'https://api.telegram.org/file/bot{TOKEN}/'
    offset = load_offset()
    try:
        register_command_menu()
        log.info('Telegram command menu registered')
    except BotError as exc:
        log.warning('could not register Telegram command menu: %s', exc)
    log.info('polling started for Telegram user ID=%s; Linux user=%s UID=%s; NoNewPrivs=%s',
             AUTHORIZED_USER_ID, LINUX_USERNAME, os.geteuid(), int(no_new_privs))
    backoff = 2
    while True:
        try:
            reap_background_jobs()
            fields = {
                'timeout': str(POLL_SECONDS),
                'allowed_updates': json.dumps(['message']),
            }
            if offset is not None:
                fields['offset'] = str(offset)
            updates = api_call('getUpdates', fields)
            backoff = 2
            for update in updates:
                update_id = update.get('update_id')
                if isinstance(update_id, int):
                    offset = update_id + 1
                    # Persist before processing so a command is not re-run after an ambiguous crash.
                    save_offset(offset)
                try:
                    process_update(update)
                except BotError as exc:
                    log.warning('update handling failed: %s', exc)
                    message = update.get('message') or {}
                    if is_authorized(message, AUTHORIZED_USER_ID):
                        try:
                            send_text(message['chat']['id'], f'Gagal: {exc}')
                        except BotError:
                            log.warning('could not send error response')
                except Exception as exc:
                    log.error('update handling failed (%s)', type(exc).__name__)
                    message = update.get('message') or {}
                    if is_authorized(message, AUTHORIZED_USER_ID):
                        try:
                            send_text(message['chat']['id'], 'Terjadi kesalahan internal; periksa jurnal layanan.')
                        except BotError:
                            log.warning('could not send error response')
        except BotError as exc:
            log.warning('poll request failed: %s; retrying in %ss', exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as exc:
            log.error('poll loop failed (%s); retrying in %ss', type(exc).__name__, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == '__main__':
    if sys.argv[1:] == ['--self-check']:
        assert safe_filename('../../escape.txt') == 'escape.txt'
        assert safe_filename('..') == 'download.bin'
        assert format_command_result(0, 'ok', False).startswith('exit code: 0\nok')
        assert not is_authorized({'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'}}, 123456789)
        assert is_authorized({'from': {'id': 123456789}, 'chat': {'id': 123456789, 'type': 'private'}}, 123456789)
        assert not is_authorized({'from': {'id': 123456789}, 'chat': {'id': -100, 'type': 'supergroup'}}, 123456789)
        assert media_details({'message_id': 1, 'photo': [{'file_id': 'p'}]})[2] == 'Pictures'
        assert media_details({'message_id': 2, 'video': {'file_id': 'v', 'file_name': 'clip.mp4'}})[2] == 'Videos'
        assert media_details({'message_id': 3, 'document': {'file_id': 'd', 'file_name': 'report.pdf'}})[2] == 'Documents'
        assert custom_upload_name('/name "final report.pdf"') == 'final report.pdf'
        assert custom_upload_name('caption biasa') == ''
        for bad_url in ('ftp://example.org/file', 'http://127.0.0.1/secret', 'http://localhost/', 'https://user:pass@example.org/file'):
            try:
                validate_download_url(bad_url)
            except BotError:
                pass
            else:
                raise AssertionError(f'unsafe URL accepted: {bad_url}')
        assert human_bytes(1024) == '1.0 KiB'
        assert 'folder kosong' in list_directory(str(HOME)) or str(HOME) in list_directory(str(HOME))
        print('self-check passed')
        raise SystemExit(0)
    raise SystemExit(main())
