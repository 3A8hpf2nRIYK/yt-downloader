#!/usr/bin/env python3
"""Kirim hasil download ke Telegram, atau balas lokasi artifact.

Dipakai workflow telegram-yt.yml, mode=download.
- Video: cari file yang namanya mengandung "[<height>p<fps>].",
  mis. "Judul Video [1080p60].mp4". Format video dipaksa MP4
  di workflow (bv mp4 + m4a, --merge-output-format mp4), jadi
  hasil seharusnya .mp4, bukan .webm.
  Video dikirim pakai sendVideo (bukan sendDocument), supaya di
  Telegram muncul preview / player seperti video biasa, lengkap
  dengan thumbnail, durasi, dan supports_streaming. Kalau pakai
  sendDocument, Telegram menampilkannya sebagai berkas/file.
- Audio: --audio mp3/m4a, cari file .mp3 / .m4a terbaru, kirim
  sebagai dokumen.

Kalau file <= 50 MB (batas kirim Bot API standar), kirim ke
Telegram. Kalau lebih besar, kirim lewat jalur file besar
Telethon/MTProto (maks 2 GB) — batas 50 MB itu batas Bot API,
bukan batas MTProto, jadi bot pun bisa kirim sampai 2 GB lewat
MTProto. Dicoba dulu pakai BOT_TOKEN (tanpa SESSION_STRING),
kalau gagal baru pakai SESSION_STRING. Butuh secret API_ID +
API_HASH (dari my.telegram.org). Kalau dua-duanya gagal, baru
kirim pesan berisi ukuran dan link run GitHub Actions (file ada
sebagai artifact di sana).
Kalau BOT_TOKEN tidak ada, cuma print lokasi file dan exit 0.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import uuid

MAX_SEND_BYTES = 50 * 1024 * 1024
AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".ogg", ".aac", ".flac", ".wav"}
VIDEO_EXTS = {".mp4": 3, ".mkv": 2, ".webm": 1}
TELETHON_ERROR = ""


def bot_username(token):
    """Username bot dari Bot API getMe, mis. 'YTDownloaderBot'.
    Dipakai supaya file besar bisa dikirim ke bot itu sendiri."""
    if not token:
        return ""
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getMe"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return (data.get("result") or {}).get("username", "")
    except Exception as e:
        print(f"getMe gagal ({e}), tidak bisa kirim ke bot.")
        return ""


def pick_file(candidates, audio_mode=False):
    """Pilih file hasil download yang benar.

    Bug 2026-11-23: yt-dlp download 398+140 tanpa ffmpeg, jadi tidak
    ter-merge. Yang tersisa dua file terpisah:
      ... [720p30].f398.mp4  (video, 20 MB)
      ... [720p30].f140.m4a  (audio, 8 MB, di-download belakangan)
    Kode lama pakai max(mtime), jadi yang terkirim malah .m4a-nya.
    Sekarang:
    - Utamakan file hasil merge (namanya TIDAK mengandung ".f123.").
    - Kalau tidak ada file merge, utamakan file video (.mp4/.mkv/.webm)
      dan jangan pernah kirim file audio (.m4a/.mp3/...) untuk mode video.
    - Mode audio tetap cari file audio, utamakan yang sudah merge.
    """
    def is_unmerged(name):
        return re.search(r"\.f\d+\.", name) is not None

    def ext(name):
        return os.path.splitext(name)[1].lower()

    if audio_mode:
        merged = [f for f in candidates if not is_unmerged(f)]
        pool = merged if merged else candidates
        return max(pool, key=os.path.getmtime)

    merged = [f for f in candidates if not is_unmerged(f)]
    if merged:
        # Utamakan .mp4, lalu yang terbaru.
        return max(merged, key=lambda f: (VIDEO_EXTS.get(ext(f), 0), os.path.getmtime(f)))
    videos = [f for f in candidates if ext(f) in VIDEO_EXTS]
    if videos:
        return max(videos, key=lambda f: (VIDEO_EXTS.get(ext(f), 0), os.path.getmtime(f)))
    print("PERINGATAN: tidak ada file video, hanya ada: " + ", ".join(candidates))
    return max(candidates, key=os.path.getmtime)


def send_message(token, chat_id, text):
    payload = {"chat_id": chat_id, "text": text}
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def edit_message(token, chat_id, message_id, text):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/editMessageText",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def delete_message(token, chat_id, message_id):
    payload = {"chat_id": chat_id, "message_id": message_id}
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/deleteMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def fmt_speed(bytes_per_sec):
    """Kecepatan upload/download, mis. 7.0 MB/s atau 850 KB/s."""
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / 1024 / 1024:.1f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.0f} KB/s"
    return f"{bytes_per_sec:.0f} B/s"


def fmt_eta(seconds):
    """Estimasi sisa waktu, mis. 01:23 atau 01:02:03."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def video_meta(filepath, height_hint=""):
    """Durasi / lebar / tinggi video.

    Coba ffprobe dulu; kalau ffprobe tidak ada (mis. ffmpeg di
    runner berasal dari imageio-ffmpeg, yang tidak menyediakan
    ffprobe — itu yang terjadi di run 45160721478: "ffprobe gagal
    ([Errno 2] No such file or directory: 'ffprobe')" sehingga
    durasi jadi 0 dan video terkirim dengan durasi 00:00),
    baca dari output `ffmpeg -i`. ffmpeg sendiri selalu ada
    karena workflow memastikannya untuk merge. Terakhir, fallback
    ke height input.
    """
    meta = {"duration": 0, "width": 0, "height": 0}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-show_entries", "format=duration",
             "-of", "json", filepath],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout or "{}")
        streams = data.get("streams") or []
        if streams:
            meta["width"] = int(streams[0].get("width") or 0)
            meta["height"] = int(streams[0].get("height") or 0)
        meta["duration"] = int(float((data.get("format") or {}).get("duration") or 0))
    except Exception as e:
        print(f"ffprobe gagal ({e}), coba ffmpeg -i.")
    if not meta["duration"] or not meta["height"]:
        try:
            out = subprocess.run(
                ["ffmpeg", "-i", filepath],
                capture_output=True, text=True, timeout=60,
            )
            err = out.stderr or ""
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)", err)
            if m and not meta["duration"]:
                h, mi, s = (int(x) for x in m.groups())
                meta["duration"] = h * 3600 + mi * 60 + s
            # Resolusi ada di baris "Stream ...: Video: ...", mis.
            # "Video: h264 (High) (avc1 / 0x31637661), yuv420p,
            # 1280x720". Jangan pakai regex (\d+)x(\d+) sembarangan:
            # tag codec "0x31637661" ikut cocok ("0"x"31637661").
            # Jadi cari di baris Video-nya saja, dan batasi 3-5 digit
            # (256x144 sampai 3840x2160).
            if not meta["height"]:
                for line in err.splitlines():
                    if "Video:" in line:
                        m2 = re.search(r"(\d{3,5})x(\d{3,5})", line)
                        if m2:
                            meta["width"] = int(m2.group(1))
                            meta["height"] = int(m2.group(2))
                        break
        except Exception as e:
            print(f"ffmpeg -i gagal ({e}), pakai height dari input.")
    if not meta["height"] and height_hint:
        try:
            meta["height"] = int(height_hint)
            meta["width"] = int(round(meta["height"] * 16 / 9))
        except ValueError:
            pass
    print(f"Meta video: durasi {meta['duration']} dtk, "
          f"{meta['width']}x{meta['height']}.")
    return meta


def make_thumbnail(filepath, duration):
    """Satu frame video sebagai thumbnail sendVideo (JPEG, lebar 320 px)."""
    thumb = "thumb.jpg"
    try:
        ss = max(1, min(5, duration // 4)) if duration else 1
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(ss), "-i", filepath,
             "-vframes", "1", "-vf", "scale=320:-1", thumb],
            capture_output=True, timeout=60, check=True,
        )
        return thumb if os.path.exists(thumb) else None
    except Exception as e:
        print(f"Buat thumbnail gagal ({e}), kirim video tanpa thumbnail.")
        return None


def _multipart_field(boundary, name, value):
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
    ).encode()


def _multipart_file(boundary, name, filepath, content_type="application/octet-stream"):
    with open(filepath, "rb") as f:
        data = f.read()
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"; '
        f'filename="{os.path.basename(filepath)}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    return head + data + b"\r\n"


def send_video(token, chat_id, filepath, caption, height_hint=""):
    """Kirim video pakai sendVideo, supaya muncul preview / player."""
    meta = video_meta(filepath, height_hint)
    thumb = make_thumbnail(filepath, meta["duration"])
    boundary = uuid.uuid4().hex
    body = b""
    body += _multipart_field(boundary, "chat_id", str(chat_id))
    if caption:
        body += _multipart_field(boundary, "caption", caption)
    if meta["duration"]:
        body += _multipart_field(boundary, "duration", str(meta["duration"]))
    if meta["width"]:
        body += _multipart_field(boundary, "width", str(meta["width"]))
    if meta["height"]:
        body += _multipart_field(boundary, "height", str(meta["height"]))
    body += _multipart_field(boundary, "supports_streaming", "true")
    if thumb:
        body += _multipart_file(boundary, "thumbnail", thumb, "image/jpeg")
    body += _multipart_file(boundary, "video", filepath, "video/mp4")
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendVideo",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode())
    finally:
        if thumb and os.path.exists(thumb):
            try:
                os.remove(thumb)
            except OSError:
                pass


def send_large_via_telethon(chat_id, filepath, caption, height_hint="",
                            audio_mode=False):
    """Kirim file >50 MB lewat Telethon/MTProto (maks 2 GB).

    Batas 50 MB itu batas Bot API (HTTP), bukan batas MTProto.
    Lewat MTProto, bot pun bisa kirim sampai 2 GB — jadi
    SESSION_STRING sebenarnya tidak wajib. Yang dibutuhkan hanya
    API_ID + API_HASH (dari my.telegram.org) dan BOT_TOKEN.

    Urutan percobaan:
    1. Bot token: Telethon login sebagai bot pakai BOT_TOKEN,
       kirim file langsung ke chat user. Kalau berhasil, file
       muncul di chat bot sebagai pesan dari bot — persis yang
       diminta. Tidak butuh SESSION_STRING sama sekali.
    2. SESSION_STRING (fallback): kalau jalur bot token gagal
       (mis. bot tidak bisa me-resolve user itu), coba pakai akun
       user dari SESSION_STRING. Kalau chat_id itu id akun
       Telethon sendiri, file dikirim ke BOT (username dari
       getMe), supaya muncul di chat bot, bukan Saved Messages.
       Worker mengabaikan pesan video tanpa teks, jadi bot tidak
       membalas apa-apa yang aneh untuk file itu.
    Kalau dua-duanya gagal, pemanggil fallback ke pesan artifact.
    Alasan gagal disimpan di TELETHON_ERROR supaya bisa
    ditampilkan di pesan fallback (tanpa nilai secret-nya).
    """
    global TELETHON_ERROR
    TELETHON_ERROR = ""
    api_id = os.environ.get("API_ID", "").strip()
    api_hash = os.environ.get("API_HASH", "").strip()
    session = os.environ.get("SESSION_STRING", "").strip()
    bot_token = os.environ.get("BOT_TOKEN", "").strip()
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from telethon.tl.types import (
            DocumentAttributeAudio,
            DocumentAttributeVideo,
        )
    except ImportError as e:
        TELETHON_ERROR = f"Telethon tidak terpasang ({e})."
        print(f"Telethon tidak terpasang ({e}), lewati jalur file besar.")
        return False

    if not (api_id and api_hash):
        TELETHON_ERROR = "secret API_ID/API_HASH belum lengkap."
        print("Secret Telethon (API_ID/API_HASH) tidak lengkap, "
              "lewati jalur file besar.")
        return False

    meta = video_meta(filepath, height_hint)
    thumb = make_thumbnail(filepath, meta["duration"]) if not audio_mode else None
    total_mb = os.path.getsize(filepath) / (1024 * 1024)
    shown = {"pct": -1, "msg_id": None, "t0": None, "sent0": 0}
    # Pesan progress upload di Telegram (satu pesan, di-edit tiap
    # 5%). Upload file besar bisa makan waktu bermenit-menit,
    # tanpa ini user tidak tahu sedang apa. Kecepatan dan estimasi
    # selesai dihitung dari byte yang sudah terkirim sejak awal
    # upload (Telethon cuma memberi sent/total, jadi speed/ETA
    # dihitung sendiri). Kalau pengiriman pesan gagal, upload
    # tetap jalan — progress cuma di log.
    if bot_token:
        try:
            r = send_message(
                bot_token, chat_id,
                f"⬆️ Upload: 0% (0.0 MB dari {total_mb:.1f} MB)")
            if r and r.get("ok"):
                shown["msg_id"] = r["result"]["message_id"]
        except Exception as e:
            print(f"Pesan progress upload gagal dikirim ({e}).")

    def progress(sent, total):
        now = time.time()
        if shown["t0"] is None:
            shown["t0"] = now
            shown["sent0"] = sent
        pct = int(sent * 100 / total) if total else 0
        if pct - shown["pct"] >= 5:
            shown["pct"] = pct
            elapsed = max(now - shown["t0"], 0.001)
            speed = (sent - shown["sent0"]) / elapsed
            eta = ""
            if speed > 0 and total:
                eta = f" • ETA {fmt_eta((total - sent) / speed)}"
            text = (f"⬆️ Upload: {pct}% ({sent / 1024 / 1024:.1f} MB "
                    f"dari {total_mb:.1f} MB) • {fmt_speed(speed)}{eta}")
            print(f"Upload file besar: {text[3:]}")
            if shown["msg_id"]:
                try:
                    edit_message(bot_token, chat_id, shown["msg_id"], text)
                except Exception as e:
                    print(f"Edit pesan progress upload gagal ({e}).")

    def _finish_progress(ok):
        if not shown["msg_id"]:
            return
        try:
            if ok:
                # File-nya sendiri sudah jadi pesan di chat, pesan
                # progress tidak perlu lagi.
                delete_message(bot_token, chat_id, shown["msg_id"])
            else:
                edit_message(bot_token, chat_id, shown["msg_id"],
                             "❌ Upload file besar gagal.")
        except Exception as e:
            print(f"Pesan progress upload gagal ditutup ({e}).")

    async def _send_file(client, target):
        if audio_mode:
            attrs = ([DocumentAttributeAudio(duration=meta["duration"])]
                     if meta["duration"] else None)
            await client.send_file(
                target, filepath, caption=caption,
                attributes=attrs, progress_callback=progress,
            )
        else:
            attrs = [DocumentAttributeVideo(
                duration=meta["duration"], w=meta["width"],
                h=meta["height"], supports_streaming=True,
            )]
            await client.send_file(
                target, filepath, caption=caption,
                thumb=thumb, attributes=attrs,
                supports_streaming=True, progress_callback=progress,
            )

    async def _send_bot_token():
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        await client.start(bot_token=bot_token)
        try:
            await _send_file(client, int(chat_id))
        finally:
            await client.disconnect()

    async def _send_session():
        client = TelegramClient(StringSession(session), int(api_id), api_hash)
        await client.start()
        try:
            # Kalau itu akun sendiri, kirim ke bot supaya muncul di
            # chat bot, bukan Saved Messages.
            target = int(chat_id)
            me = await client.get_me()
            if me and int(chat_id) == me.id:
                username = bot_username(bot_token)
                if username:
                    print(f"chat_id = akun sendiri, kirim ke bot @{username}.")
                    target = "@" + username
            await _send_file(client, target)
        finally:
            await client.disconnect()

    errors = []
    try:
        if bot_token:
            try:
                asyncio.run(_send_bot_token())
                print(f"File besar terkirim lewat Telethon "
                      f"(bot token, {total_mb:.1f} MB).")
                _finish_progress(True)
                return True
            except Exception as e:
                print(f"Jalur bot token gagal ({e}), coba SESSION_STRING...")
                errors.append(f"bot token: {e}")
        if session:
            # StringSession Telethon selalu diawali angka "1" dan
            # panjangnya sekitar 353 karakter (IPv4). Kalau tidak
            # begitu, string itu bukan StringSession Telethon.
            if not session.startswith("1") or len(session) < 300:
                errors.append(
                    "SESSION_STRING bukan format StringSession Telethon "
                    f"(harus diawali '1' dan panjang ~353 karakter, yang ini "
                    f"panjangnya {len(session)} karakter)."
                )
            else:
                try:
                    asyncio.run(_send_session())
                    print(f"File besar terkirim lewat Telethon "
                          f"(session, {total_mb:.1f} MB).")
                    _finish_progress(True)
                    return True
                except Exception as e:
                    print(f"Jalur SESSION_STRING gagal ({e}).")
                    errors.append(f"session: {e}")
        TELETHON_ERROR = ("pengiriman gagal (" + "; ".join(errors) + ")."
                          if errors else
                          "secret Telethon tidak lengkap.")
        print(f"Pengiriman lewat Telethon gagal: {TELETHON_ERROR}")
        _finish_progress(False)
        return False
    finally:
        if thumb and os.path.exists(thumb):
            try:
                os.remove(thumb)
            except OSError:
                pass


def send_document(token, chat_id, filepath, caption=""):
    boundary = uuid.uuid4().hex
    with open(filepath, "rb") as f:
        data = f.read()
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'.encode()
    if caption:
        body += f"--{boundary}\r\n".encode()
        body += (
            f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
        ).encode()
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="document"; '
        f'filename="{os.path.basename(filepath)}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    body += data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode())


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", required=True)
    ap.add_argument("--height", default="")
    ap.add_argument("--fps", default="")
    ap.add_argument("--audio", default="")
    ap.add_argument("--run-url", default="")
    args = ap.parse_args()

    if args.audio:
        ext = f".{args.audio}"
        label = args.audio.upper()
        candidates = [
            f for f in os.listdir(".")
            if f.endswith(ext) and os.path.isfile(f)
        ]
        needle = ext
    else:
        label = f"{args.height}p{args.fps}"
        # Jangan pakai glob dengan "[1080p60]" - kurung siku di glob itu
        # character class. Cek substring saja.
        needle = f"[{args.height}p{args.fps}]."
        candidates = [
            f for f in os.listdir(".")
            if needle in f and os.path.isfile(f) and not f.endswith((".txt", ".json"))
        ]
    token = os.environ.get("BOT_TOKEN", "")
    if not candidates:
        print(f"Tidak ada file hasil download dengan pola {needle}")
        if token:
            send_message(token, args.chat_id, "Download selesai tapi file tidak ketemu.")
        return 1

    filepath = pick_file(candidates, audio_mode=bool(args.audio))
    size = os.path.getsize(filepath)
    mb = size / (1024 * 1024)
    print(f"Hasil: {filepath} ({mb:.1f} MB)")
    if not token:
        print("BOT_TOKEN tidak ada, file tidak dikirim ke Telegram.")
        return 0
    if size > MAX_SEND_BYTES:
        # File besar: coba Telethon dulu (maks 2 GB). Kalau secret
        # belum diset atau pengiriman gagal, fallback ke artifact.
        if not args.audio and os.path.splitext(filepath)[1].lower() in VIDEO_EXTS:
            title = re.sub(r" \[\d+p\d+\]\.[^.]+$", "", os.path.basename(filepath))
            caption = f"{title}\n🎬 Video - {label} • {mb:.1f} MB"
        else:
            caption = (
                f"{os.path.basename(filepath)}\n"
                f"{label} • {mb:.1f} MB"
            )
        if send_large_via_telethon(
                args.chat_id, filepath, caption, args.height,
                audio_mode=bool(args.audio)):
            return 0
        send_message(
            token,
            args.chat_id,
            f"Selesai download {label} ({mb:.0f} MB, "
            f"di atas batas kirim bot 50 MB).\n"
            f"File: {os.path.basename(filepath)}\n"
            f"Jalur file besar (Telethon) tidak tersedia/gagal"
            f"{': ' + TELETHON_ERROR if TELETHON_ERROR else ''} — "
            f"filenya disimpan sebagai artifact di GitHub Actions:\n{args.run_url}",
        )
        return 0
    if size <= MAX_SEND_BYTES:
        if not args.audio and os.path.splitext(filepath)[1].lower() in VIDEO_EXTS:
            title = re.sub(r" \[\d+p\d+\]\.[^.]+$", "", os.path.basename(filepath))
            caption = f"{title}\n🎬 Video - {label} • {mb:.1f} MB"
            send_video(token, args.chat_id, filepath, caption, args.height)
        else:
            caption = (
                f"{os.path.basename(filepath)}\n"
                f"{label} • {mb:.1f} MB"
            )
            send_document(token, args.chat_id, filepath, caption)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
