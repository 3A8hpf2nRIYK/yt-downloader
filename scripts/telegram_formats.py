#!/usr/bin/env python3
"""Kirim menu format ke Telegram dari hasil `yt-dlp -F` + `yt-dlp -J`.

Dipakai workflow telegram-yt.yml, mode=list.

Tampilan yang dipakai (bukan scriptnya) meniru bot lain yang Yao
tunjukkan di screenshot: foto thumbnail di atas, judul, channel,
durasi, lalu tombol format 2 kolom, ukuran perkiraan satu angka
(±MB), opsi audio MP3 / M4A, dan tombol Batal.

Cara kerja, ditulis sendiri di sini:
- Metadata (judul, channel, durasi, thumbnail) dan daftar format
  diambil dari info.json hasil `yt-dlp -J`. Kalau info.json tidak
  ada / rusak, fallback ke parsing formats.txt hasil `-F`.
- Untuk tiap kualitas (tinggi+fps) dipilih SATU format video MP4
  (supaya hasil download .mp4, bukan .webm): diutamakan ext=mp4,
  codec av01 dulu lalu avc1. Ukuran perkiraan = ukuran video itu
  + ukuran audio m4a terbaik. Ini yang bikin angka di tombol mirip
  contoh: 1080p60 av01 14.8 MiB + m4a 6.6 MiB = ±21.4 MB.
- Audio: M4A = format audio m4a terbaik (biasanya 140, 128k),
  MP3 = durasi * 192 kbps / 8 (hasil konversi ke mp3 192k).
- Tombol video diurutkan dari yang tertinggi, 2 per baris,
  lalu satu baris audio MP3/M4A, lalu satu baris Batal.
- Kalau BOT_TOKEN tidak ada di env, cuma print pilihan ke stdout
  dan exit 0, supaya workflow tetap sukses dan formats.txt bisa
  dilihat sebagai artifact.
"""
import html
import json
import os
import re
import sys
import urllib.request

FORMAT_RE = re.compile(r"(\d+)x(\d+)(?:\s+(\d+))?")
SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(GiB|MiB|KiB|B)\b")
SIZE_MULT = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}


def fmt_duration(sec):
    if not sec:
        return "-"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_mb(nbytes):
    return f"{nbytes / (1024 * 1024):.1f} MB"


def size_from_format(f, duration):
    """Ukuran satu format: filesize, filesize_approx, atau tbr*durasi."""
    for key in ("filesize", "filesize_approx"):
        v = f.get(key)
        if v:
            return float(v)
    tbr = f.get("tbr")
    if tbr and duration:
        return float(tbr) * 1000 / 8 * duration
    return 0


def vcodec_rank(vcodec):
    v = (vcodec or "").lower()
    if v.startswith("av01"):
        return 3
    if v.startswith("avc"):
        return 2
    if v.startswith("vp9") or v.startswith("vp09"):
        return 1
    return 0


def build_options_from_info(info):
    """Return (meta, video_options, audio_options) dari info.json.

    video_options: [(height, fps, size_bytes), ...] terurut menurun.
    audio_options: [("mp3", size_bytes), ("m4a", size_bytes)]
    """
    meta = {
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail") or "",
    }
    formats = info.get("formats") or []
    duration = meta["duration"]

    # Audio m4a terbaik (untuk digabung ke video, dan untuk tombol M4A).
    m4a_audios = [
        f for f in formats
        if (f.get("acodec") or "none") != "none"
        and (f.get("vcodec") or "none") == "none"
        and f.get("ext") == "m4a"
    ]
    audios = [
        f for f in formats
        if (f.get("acodec") or "none") != "none"
        and (f.get("vcodec") or "none") == "none"
    ]
    best_m4a = max(m4a_audios, key=lambda f: f.get("tbr") or 0) if m4a_audios else None
    audio_size = size_from_format(best_m4a, duration) if best_m4a else 0
    if not audio_size and audios:
        best_any = max(audios, key=lambda f: f.get("tbr") or 0)
        audio_size = size_from_format(best_any, duration)

    # Video: kelompokkan per (tinggi, fps), pilih satu format MP4.
    groups = {}
    for f in formats:
        if (f.get("vcodec") or "none") == "none":
            continue
        h = f.get("height")
        if not h or not 144 <= h <= 4320:
            continue
        fps = f.get("fps") or 30
        groups.setdefault((h, int(fps)), []).append(f)

    video_options = []
    for (h, fps), cands in groups.items():
        mp4s = [f for f in cands if f.get("ext") == "mp4"]
        pool = mp4s if mp4s else cands
        # av01 dulu, lalu avc1, lalu vp9; di dalam codec yang sama
        # yang tbr-nya paling besar.
        chosen = max(
            pool,
            key=lambda f: (vcodec_rank(f.get("vcodec")), f.get("tbr") or 0),
        )
        vsize = size_from_format(chosen, duration)
        total = vsize + audio_size if vsize else 0
        if total:
            video_options.append((h, fps, total))
    video_options.sort(key=lambda x: (-x[0], -x[1]))

    audio_options = []
    if duration:
        audio_options.append(("mp3", int(duration * 192_000 / 8)))
    if audio_size:
        audio_options.append(("m4a", audio_size))
    return meta, video_options, audio_options


def parse_fallback_formats(text):
    """Fallback dari formats.txt (-F): kembalikan (meta_kosong, video, audio).

    Ukuran dihitung dari kolom FILESIZE baris video MP4 yang dipilih
    (av01 dulu lalu avc1) + ukuran audio m4a. MP3 tidak bisa
    dihitung tanpa durasi, jadi hanya M4A yang ditawarkan di sini.
    """
    groups = {}
    m4a_size = 0
    for line in text.splitlines():
        if re.search(r"\bsb\d+\b", line):
            continue
        # Baris audio only dulu: tidak ada WxH, jadi harus dicek
        # sebelum FORMAT_RE (yang akan gagal dan skip baris ini).
        if "audio only" in line:
            tokens = line.split()
            if len(tokens) > 1 and tokens[1] == "m4a":
                sm = SIZE_RE.search(line)
                if sm:
                    m4a_size = max(
                        m4a_size,
                        float(sm.group(1)) * SIZE_MULT[sm.group(2)],
                    )
            continue
        m = FORMAT_RE.search(line)
        if not m:
            continue
        h = int(m.group(2))
        fps = int(m.group(3)) if m.group(3) else 30
        if not 144 <= h <= 4320:
            continue
        tokens = line.split()
        ext = tokens[1] if len(tokens) > 1 else ""
        # vcodec ada setelah kolom PROTO, ambil token yang mirip codec.
        vcodec = ""
        for tok in tokens:
            if tok.startswith(("av01", "avc1", "vp9", "vp09")):
                vcodec = tok
                break
        sm = SIZE_RE.search(line, m.end())
        size = float(sm.group(1)) * SIZE_MULT[sm.group(2)] if sm else 0
        groups.setdefault((h, fps), []).append(
            {"ext": ext, "vcodec": vcodec, "size": size}
        )
    video_options = []
    for (h, fps), cands in groups.items():
        mp4s = [c for c in cands if c["ext"] == "mp4"]
        pool = mp4s if mp4s else cands
        chosen = max(pool, key=lambda c: (vcodec_rank(c["vcodec"]), c["size"]))
        total = chosen["size"] + m4a_size if chosen["size"] else 0
        if total:
            video_options.append((h, fps, total))
    video_options.sort(key=lambda x: (-x[0], -x[1]))
    audio_options = [("m4a", m4a_size)] if m4a_size else []
    meta = {"title": "", "uploader": "", "duration": 0, "thumbnail": ""}
    return meta, video_options, audio_options


def failure_reason(text, version):
    # 429 dulu: ini penyebab paling sering untuk kegagalan sesekali.
    # YouTube membatasi IP server GitHub Actions (IP bersama, dipakai
    # banyak orang untuk yt-dlp), jadi request ditolak sementara.
    # Bukan cookies — cookies sudah dipakai, dan coba lagi beberapa
    # menit kemudian biasanya berhasil (run 45135694044 gagal 429
    # jam 15:04, run 45136352121 untuk video yang sama berhasil
    # jam 15:09). Jangan suruh user perbarui COOKIES_TXT untuk ini.
    if "429" in text or "Too Many Requests" in text:
        return (
            "YouTube membatasi sementara (HTTP 429, terlalu banyak "
            "request dari server GitHub). Ini bukan cookies — "
            "cookies sudah dipakai. Coba lagi beberapa menit lagi, "
            "bot akan coba otomatis."
        )
    if "n challenge solving failed" in text:
        return (
            f"v{version} gagal menyelesaikan JS challenge YouTube "
            "(n challenge), cuma storyboard yang muncul. "
            "Ini bukan soal cookies — cookies sudah dipakai dan v2 (deno) "
            "biasanya bisa. Coba pilih v2."
        )
    if "Only images are available" in text:
        return (
            f"v{version} cuma dapat gambar/storyboard dari YouTube. "
            "Coba pilih v2 (deno)."
        )
    if "Sign in to confirm" in text or "confirm you're not a bot" in text:
        return (
            "YouTube minta login / kena bot. Coba versi lain, "
            "atau perbarui COOKIES_TXT."
        )
    return (
        "Tidak ada format yang ketemu. Kemungkinan kena bot / "
        "cookies kedaluwarsa. Coba versi lain, atau perbarui COOKIES_TXT."
    )


def build_caption(meta, video_options, note=""):
    lines = []
    if meta.get("title"):
        lines.append(f"<b>{html.escape(meta['title'])}</b>")
    if meta.get("uploader"):
        lines.append(f"👤 {html.escape(meta['uploader'])}")
    if meta.get("duration"):
        lines.append(f"⏱ {fmt_duration(meta['duration'])}")
    if lines:
        lines.append("")
    lines.append("Pilih format yang mau didownload (ukuran perkiraan):")
    if any(size > 50 * 1024 * 1024 for _, _, size in video_options):
        lines.append(
            "ℹ️ Opsi di atas 50 MB dikirim lewat jalur file besar "
            "(maks 2 GB, lebih lambat)."
        )
    caption = "\n".join(lines)
    if note:
        caption = f"{note}\n\n{caption}"
    return caption


def build_keyboard(video_id, version, video_options, audio_options):
    rows = []
    # Video: 2 tombol per baris, dari kualitas tertinggi.
    for i in range(0, len(video_options), 2):
        row = []
        for h, fps, size in video_options[i:i + 2]:
            row.append({
                "text": f"🎬 {h}p • {fps}fps • ±{fmt_mb(size)}",
                "callback_data": f"dl:{video_id}:{version}:{h}:{fps}",
            })
        rows.append(row)
    # Audio: satu baris MP3 + M4A.
    if audio_options:
        row = []
        for kind, size in audio_options:
            row.append({
                "text": f"🎵 {kind.upper()} • ±{fmt_mb(size)}",
                "callback_data": f"au:{video_id}:{version}:{kind}",
            })
        rows.append(row)
    rows.append([{
        "text": "✖️ Batal",
        "callback_data": f"cancel:{video_id}",
    }])
    return {"inline_keyboard": rows}


def tg_request(token, method, payload):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--formats-file", required=True)
    ap.add_argument("--info-file", default="")
    ap.add_argument("--chat-id", required=True)
    ap.add_argument("--video-id", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    with open(args.formats_file, encoding="utf-8", errors="ignore") as f:
        text = f.read()

    meta, video_options, audio_options = None, [], []
    if args.info_file and os.path.exists(args.info_file):
        try:
            with open(args.info_file, encoding="utf-8") as f:
                info = json.load(f)
            # yt-dlp -J yang gagal (mis. 429) bisa menulis "null",
            # jadi info bukan dict. Itu bukan metadata yang valid.
            if not isinstance(info, dict):
                raise ValueError("info.json bukan objek JSON")
            meta, video_options, audio_options = build_options_from_info(info)
        except Exception as e:
            print(f"info.json tidak bisa dibaca ({e}), fallback ke -F.")
    if not video_options:
        meta, video_options, audio_options = parse_fallback_formats(text)

    token = os.environ.get("BOT_TOKEN", "")

    if not video_options:
        print("Tidak ada format video yang ketemu.")
        print(failure_reason(text, args.version))
        if token:
            tg_request(token, "sendMessage", {
                "chat_id": args.chat_id,
                "text": failure_reason(text, args.version),
            })
        return 1

    for h, fps, size in video_options:
        print(f"{h}p{fps} • ±{fmt_mb(size)}")
    for kind, size in audio_options:
        print(f"{kind.upper()} • ±{fmt_mb(size)}")

    caption = build_caption(meta, video_options, args.note)
    keyboard = build_keyboard(
        args.video_id, args.version, video_options, audio_options
    )
    if not token:
        print("BOT_TOKEN tidak ada, tombol tidak dikirim ke Telegram.")
        print(caption)
        return 0

    # Kirim sebagai foto thumbnail kalau ada, kalau gagal kirim teks biasa.
    sent = False
    if meta.get("thumbnail"):
        try:
            tg_request(token, "sendPhoto", {
                "chat_id": args.chat_id,
                "photo": meta["thumbnail"],
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": keyboard,
            })
            sent = True
        except Exception as e:
            print(f"sendPhoto gagal ({e}), kirim sebagai teks.")
    if not sent:
        tg_request(token, "sendMessage", {
            "chat_id": args.chat_id,
            "text": caption,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
