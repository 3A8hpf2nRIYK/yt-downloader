# Bot Telegram yt-dlp di Cloudflare Worker

Worker ini frontend Telegram. Download-nya dikerjakan GitHub Actions
(workflow `.github/workflows/telegram-yt.yml` di repo ini), karena
Worker tidak bisa menjalankan yt-dlp / ffmpeg / node / deno.

## Alur

1. Kirim link YouTube ke bot
2. Bot balas tombol **v1 - node** / **v2 - deno**
3. Worker trigger workflow, mode `list`
4. Actions jalanin `yt-dlp ... -F <link>`, kirim tombol kualitas
   ke Telegram (hanya yang tersedia, sampai 4320p/8K, lengkap fps)
5. Tap kualitas, mis. `1080p60`
6. Worker trigger workflow, mode `download`
7. Actions download `bv[height=H][fps<=FPS]+ba`, kirim file ke
   Telegram kalau <= 50 MB, kalau tidak simpan sebagai artifact
   dan kirim link run GitHub Actions-nya.

Tiap langkah Actions butuh waktu, biasanya sekitar 1 menit untuk
daftar format, lebih lama untuk download. Itu normal.

## Yang dibutuhkan sebelum deploy

- Bot Telegram dari @BotFather, simpan tokennya. **Jangan paste
  token di chat, dan jangan commit ke repo.**
- Akun Cloudflare, dan `wrangler` (Node.js).
- Fine-grained PAT GitHub untuk Worker, repo `l0sSC9t5nfhX/yt-dlp`,
  izin **Actions: Read and write** dan **Metadata: Read**.
  PAT lama yang cuma Contents + Workflows akan dibalas 403 saat
  Worker memanggil workflow_dispatch.
- Di repo GitHub ini, Settings > Secrets and variables > Actions:
  - `BOT_TOKEN` — token bot yang sama
  - `COOKIES_TXT` — opsional, isi file `mycookies.txt` kamu
    (format Netscape). Tanpa ini YouTube lebih mudah kena bot.

## Deploy Worker

```bash
cd worker
npm install -g wrangler
npx wrangler login
npx wrangler secret put BOT_TOKEN
npx wrangler secret put GITHUB_TOKEN
npx wrangler secret put WEBHOOK_SECRET
# WEBHOOK_SECRET boleh string acak apa saja, mis. hasil `openssl rand -hex 16`
npx wrangler deploy
```

Catat URL Worker-nya, misal
`https://yt-dlp.<subdomain-kamu>.workers.dev`

Isi `ALLOWED_USER_ID` di `wrangler.toml` dengan Telegram user id
kamu (angka) supaya bot cuma bisa dipakai kamu, lalu deploy ulang.
Cara tahu user id: kirim pesan ke @userinfobot di Telegram.

## Daftarkan webhook ke Telegram

Ganti `<TOKEN>`, `<WEBHOOK_SECRET>`, dan `<URL-WORKER>` di bawah,
jalankan di terminal (bukan di chat):

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<URL-WORKER>/webhook&secret_token=<WEBHOOK_SECRET>"
```

Cek statusnya:

```bash
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

## Tes

Kirim link ke bot, misal `https://youtu.be/QQV-00UGxGs`, pilih
v1/v2, lalu pilih kualitas. Kalau tombol kualitas tidak muncul,
cek tab Actions di repo, dan log Worker dengan:

```bash
npx wrangler tail
```
