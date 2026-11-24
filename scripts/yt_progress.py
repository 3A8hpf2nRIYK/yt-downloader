#!/usr/bin/env python3
"""Jalankan perintah yt-dlp sambil menampilkan progress download di Telegram.

Dipakai workflow telegram-yt.yml, mode=download, membungkus perintah
yt-dlp. Tanpa progress, download file besar (mis. 578 MB) tidak ada
kabarnya sama sekali selama 1-2 menit — user hanya melihat pesan
"Download: 1080p60 ..." lalu menunggu.

Cara kerja: tambahkan --newline ke yt-dlp supaya tiap update progress
dicetak sebagai baris baru, baca baris-baris `[download] ...% of ...`,
lalu edit satu pesan Telegram (sendMessage sekali, lalu
editMessageText). Edit dibatasi: tiap naik >=5% atau tiap 15 detik,
supaya tidak kena rate limit Telegram.

yt-dlp download video+audio sebagai dua file terpisah (video .mp4
dulu, lalu audio .m4a), jadi persen akan mulai lagi dari 0 untuk file
kedua. Baris "Destination:" dipakai untuk membedakan keduanya
("Download video" / "Download audio").

Kalau BOT_TOKEN / CHAT_ID tidak ada di env, script ini cuma
meneruskan perintah apa adanya (tanpa pesan Telegram). Exit code
yt-dlp diteruskan apa adanya supaya workflow tetap gagal kalau
download gagal.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
BASE_LABEL = os.environ.get("PROGRESS_LABEL", "Download")


def tg(method, data):
    """Satu panggilan Bot API. Kembalikan dict hasil, atau None."""
    if not TOKEN or not CHAT_ID:
        return None
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}", data=body
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"Telegram {method} gagal ({e}).")
        return None


def main():
    if len(sys.argv) < 2:
        print("Pakai: yt_progress.py <perintah yt-dlp...>")
        return 2
    cmd = [sys.argv[1], "--newline"] + sys.argv[2:]

    msg_id = None
    r = tg("sendMessage", {
        "chat_id": CHAT_ID,
        "text": f"\u2b07\ufe0f {BASE_LABEL}: mulai...",
    })
    if r and r.get("ok"):
        msg_id = r["result"]["message_id"]

    kind = ""
    last_pct = -1.0
    last_edit = 0.0
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        dest = re.search(r"\[download\] Destination:\s*(.+)", line)
        if dest:
            name = dest.group(1).strip()
            new_kind = "audio" if name.lower().endswith(
                (".m4a", ".opus", ".ogg", ".mp3")) else "video"
            if new_kind != kind:
                kind = new_kind
                last_pct = -1.0
        m = re.search(
            r"\[download\]\s+(\d+(?:\.\d+)?)% of\s+([\d.]+[KMGT]iB)", line)
        if not m or not msg_id:
            continue
        pct = float(m.group(1))
        total = m.group(2)
        now = time.time()
        if pct - last_pct < 5 and pct < 100 and now - last_edit < 15:
            continue
        last_pct = pct
        last_edit = now
        extra = ""
        sm = re.search(r"at\s+([\d.]+[KMGT]iB/s)(?:\s+ETA\s+(\S+))?", line)
        if sm:
            extra = f" \u2022 {sm.group(1)}"
            if sm.group(2):
                extra += f" \u2022 ETA {sm.group(2)}"
        label = f"{BASE_LABEL} {kind}" if kind else BASE_LABEL
        tg("editMessageText", {
            "chat_id": CHAT_ID,
            "message_id": msg_id,
            "text": f"\u2b07\ufe0f {label}: {pct:.0f}% dari {total}{extra}",
        })
    rc = proc.wait()
    if msg_id:
        if rc == 0:
            tg("editMessageText", {
                "chat_id": CHAT_ID,
                "message_id": msg_id,
                "text": f"{BASE_LABEL} selesai, mengirim ke Telegram...",
            })
        else:
            tg("editMessageText", {
                "chat_id": CHAT_ID,
                "message_id": msg_id,
                "text": f"{BASE_LABEL} gagal (yt-dlp exit {rc}).",
            })
    return rc


if __name__ == "__main__":
    sys.exit(main())
