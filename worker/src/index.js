// worker/src/index.js - Bot Telegram yt-dlp di Cloudflare Worker
//
// Worker ini HANYA frontend Telegram. Dia TIDAK menjalankan yt-dlp,
// karena Worker adalah V8 isolate: tanpa filesystem, tanpa child_process,
// memori 128 MB. yt-dlp / ffmpeg / node / deno dijalankan di GitHub Actions
// (backend), yang di-trigger Worker lewat GitHub API workflow_dispatch.
//
// Alur:
//   1. User kirim link YouTube ke bot
//      -> Worker balas tombol v1 (node) / v2 (deno)
//   2. User tap v1/v2
//      -> Worker trigger workflow telegram-yt.yml, mode=list
//      -> GitHub Actions jalanin `yt-dlp ... -F <link>`, lalu kirim
//         tombol kualitas ke Telegram (hanya yang tersedia, sampai
//         4320p/8K, lengkap dengan fps, mis. 720p60, 1080p60)
//   3. User tap kualitas, mis. 1080p60, atau audio MP3/M4A
//      -> Worker trigger workflow yang sama, mode=download
//      -> GitHub Actions download video MP4
//         `bv[ext=mp4][height=H][fps<=FPS]+ba[ext=m4a]` dengan
//         --merge-output-format mp4, atau audio MP3/M4A,
//         kirim file ke Telegram kalau <= 50 MB, kalau tidak
//         simpan sebagai artifact dan kirim link run-nya.
//      Tombol Batal -> hapus keyboard, balas "Dibatalkan."
//
// Secrets (wrangler secret put, JANGAN tulis di kode / di chat):
//   BOT_TOKEN      - token bot dari @BotFather
//   GITHUB_TOKEN   - fine-grained PAT, repo l0sSC9t5nfhX/yt-dlp,
//                    izin Actions: Read and write, Metadata: Read.
//                    (PAT lama yang cuma Contents + Workflows tidak cukup
//                    untuk workflow_dispatch, akan dibalas 403.)
//   WEBHOOK_SECRET - opsional, dipakai sebagai secret_token saat setWebhook
//
// Vars (di wrangler.toml):
//   GITHUB_OWNER, GITHUB_REPO, WORKFLOW_FILE, ALLOWED_USER_ID
//   ALLOWED_USER_ID: isi dengan Telegram user id kamu supaya bot cuma
//   bisa dipakai kamu. Kosongkan = siapa pun yang menemukan bot bisa
//   trigger Actions kamu (tidak disarankan).

const YT_RE =
  /(?:youtu\.be\/|youtube\.com\/(?:watch\?v=|shorts\/|embed\/|v\/))([A-Za-z0-9_-]{11})/;

function extractVideoId(text) {
  const m = text.match(YT_RE);
  return m ? m[1] : null;
}

async function telegram(env, method, payload) {
  const res = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return res;
}

async function sendMessage(env, chatId, text, replyMarkup) {
  const payload = { chat_id: chatId, text };
  if (replyMarkup) payload.reply_markup = replyMarkup;
  await telegram(env, "sendMessage", payload);
}

async function answerCallback(env, callbackQueryId, text) {
  await telegram(env, "answerCallbackQuery", {
    callback_query_id: callbackQueryId,
    text: text || "",
  });
}

function inlineKeyboard(rows) {
  return {
    inline_keyboard: rows.map((row) =>
      row.map(([label, data]) => ({ text: label, callback_data: data })),
    ),
  };
}

// Trigger GitHub Actions. Berhasil = HTTP 204 (tanpa body).
async function dispatchWorkflow(env, inputs) {
  const owner = env.GITHUB_OWNER || "l0sSC9t5nfhX";
  const repo = env.GITHUB_REPO || "yt-dlp";
  const file = env.WORKFLOW_FILE || "telegram-yt.yml";
  const res = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${file}/dispatches`,
    {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        // WAJIB: GitHub API menolak request tanpa User-Agent dengan 403
        // "Request forbidden by administrative rules... User-Agent header".
        // fetch() di Cloudflare Worker tidak mengirim User-Agent sendiri,
        // jadi harus diisi manual di sini.
        "User-Agent": "yt-dlp-telegram-bot",
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs }),
    },
  );
  const body = await res.text();
  return { ok: res.status === 204, status: res.status, body };
}

async function handleUpdate(env, update) {
  const msg = update.message;
  const cb = update.callback_query;

  // --- Batasi pemakai (kalau ALLOWED_USER_ID diisi) ---
  const fromId = msg?.from?.id ?? cb?.from?.id;
  if (env.ALLOWED_USER_ID && String(fromId) !== String(env.ALLOWED_USER_ID)) {
    const chatId = msg?.chat?.id ?? cb?.message?.chat?.id;
    if (chatId) await sendMessage(env, chatId, "Bot ini private.");
    return;
  }

  // --- Pesan teks ---
  if (msg && msg.text) {
    const chatId = msg.chat.id;
    const text = msg.text.trim();

    if (text.startsWith("/start") || text.startsWith("/help")) {
      await sendMessage(
        env,
        chatId,
        "Kirim link YouTube, lalu pilih v1/v2 dan formatnya.\n\n" +
          "Contoh: https://youtu.be/VIDEO_ID\n\n" +
          "v1 = node, v2 = deno + --sleep-requests 3.\n" +
          "Video sampai 8K (hanya yang tersedia, lengkap dengan fps), " +
          "hasil video MP4, audio bisa MP3 atau M4A.",
      );
      return;
    }

    const videoId = extractVideoId(text);
    if (!videoId) {
      await sendMessage(env, chatId, "Kirim link YouTube ya, contoh:\nhttps://youtu.be/VIDEO_ID");
      return;
    }

    await sendMessage(env, chatId, "Pilih versi:", inlineKeyboard([
      [
        ["v1 - node", `ver:${videoId}:1`],
        ["v2 - deno", `ver:${videoId}:2`],
      ],
    ]));
    return;
  }

  // --- Tombol inline (callback_query) ---
  if (cb) {
    const chatId = cb.message?.chat?.id;
    const data = cb.data || "";
    const parts = data.split(":");
    await answerCallback(env, cb.id);

    // ver:VIDEOID:1|2 -> trigger mode=list
    if (parts[0] === "ver" && parts.length === 3) {
      const [, videoId, version] = parts;
      const label = version === "1" ? "v1 (node)" : "v2 (deno)";
      await sendMessage(
        env,
        chatId,
        `Mengambil daftar format (${label})...\nGitHub Actions sedang jalan, tunggu 1-2 menit, nanti tombol kualitas muncul di sini. Kadang YouTube membatasi sementara (429), bot akan coba lagi otomatis.`,
      );
      const r = await dispatchWorkflow(env, {
        mode: "list",
        url: `https://youtu.be/${videoId}`,
        version,
        chat_id: String(chatId),
        video_id: videoId,
        height: "",
        fps: "",
        audio: "",
      });
      if (!r.ok) {
        await sendMessage(
          env,
          chatId,
          `Gagal menjalankan workflow (GitHub: ${r.status}).\n${r.body.slice(0, 500)}`,
        );
      }
      return;
    }

    // dl:VIDEOID:VER:H:FPS -> trigger mode=download (video MP4)
    if (parts[0] === "dl" && parts.length === 5) {
      const [, videoId, version, height, fps] = parts;
      await sendMessage(env, chatId, `Download: ${height}p${fps} ...`);
      const r = await dispatchWorkflow(env, {
        mode: "download",
        url: `https://youtu.be/${videoId}`,
        version,
        chat_id: String(chatId),
        video_id: videoId,
        height,
        fps,
        audio: "",
      });
      if (!r.ok) {
        await sendMessage(
          env,
          chatId,
          `Gagal menjalankan workflow (GitHub: ${r.status}).\n${r.body.slice(0, 500)}`,
        );
      }
      return;
    }

    // au:VIDEOID:VER:mp3|m4a -> trigger mode=download (audio saja)
    if (parts[0] === "au" && parts.length === 4) {
      const [, videoId, version, audio] = parts;
      if (audio !== "mp3" && audio !== "m4a") {
        await sendMessage(env, chatId, "Format audio tidak dikenal.");
        return;
      }
      await sendMessage(env, chatId, `Download: ${audio.toUpperCase()} ...`);
      const r = await dispatchWorkflow(env, {
        mode: "download",
        url: `https://youtu.be/${videoId}`,
        version,
        chat_id: String(chatId),
        video_id: videoId,
        height: "",
        fps: "",
        audio,
      });
      if (!r.ok) {
        await sendMessage(
          env,
          chatId,
          `Gagal menjalankan workflow (GitHub: ${r.status}).\n${r.body.slice(0, 500)}`,
        );
      }
      return;
    }

    // cancel:VIDEOID -> hapus keyboard, balas Dibatalkan.
    if (parts[0] === "cancel") {
      try {
        await telegram(env, "editMessageReplyMarkup", {
          chat_id: chatId,
          message_id: cb.message?.message_id,
          reply_markup: { inline_keyboard: [] },
        });
      } catch {
        // Abaikan, pesan Dibatalkan tetap dikirim di bawah.
      }
      await sendMessage(env, chatId, "Dibatalkan.");
      return;
    }

    await sendMessage(env, chatId, "Tombol tidak dikenal, kirim link-nya lagi.");
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      return new Response("ok - telegram yt-dlp worker");
    }

    if (request.method !== "POST") {
      return new Response("ok");
    }

    // Terima webhook di /webhook (yang didaftarkan ke Telegram) dan di /.
    if (url.pathname !== "/webhook" && url.pathname !== "/") {
      return new Response("not found", { status: 404 });
    }

    // Kalau WEBHOOK_SECRET diisi, tolak request yang bukan dari Telegram.
    if (env.WEBHOOK_SECRET) {
      const got = request.headers.get("x-telegram-bot-api-secret-token");
      if (got !== env.WEBHOOK_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
    }

    if (!env.BOT_TOKEN || !env.GITHUB_TOKEN) {
      return new Response("bot belum dikonfigurasi (BOT_TOKEN / GITHUB_TOKEN)", {
        status: 500,
      });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad json", { status: 400 });
    }

    await handleUpdate(env, update);
    return new Response("ok");
  },
};
