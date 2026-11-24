#!/usr/bin/env python3
"""Buat StringSession Telethon untuk jalur file besar (>50 MB).

Cara pakai di Termux (jalankan dari folder BUKAN home, supaya
tidak kena email.py di home yang menutupi stdlib email):
  mkdir -p ~/telethon-session && cd ~/telethon-session
  curl -O https://raw.githubusercontent.com/l0sSC9t5nfhX/yt-dlp/main/scripts/gen_session.py
  pip install telethon
  python3 gen_session.py

Masukkan API_ID dan API_HASH (dari my.telegram.org, sama seperti
di GitHub secret), lalu nomor HP (mis. +62812...) dan kode OTP
yang dikirim Telegram. Kalau akunmu pakai verifikasi 2 langkah,
masukkan juga password-nya.

Script mencetak SESSION_STRING — salin nilainya ke GitHub
secret SESSION_STRING di repo yt-dlp. Jangan pernah paste
nilainya di chat, karena siapa pun yang punya string ini bisa
bertindak sebagai akun Telegram kamu.
"""
import getpass

from telethon.errors import SessionPasswordNeededError
from telethon.sync import TelegramClient
from telethon.sessions import StringSession


def main():
    api_id = int(input("API_ID: ").strip())
    api_hash = input("API_HASH: ").strip()
    phone = input("Nomor HP (mis. +62812...): ").strip()
    # Kode OTP biasanya dikirim Telegram ke aplikasi (chat "Telegram"),
    # bukan SMS. Kalau kode tidak masuk-masuk, pilih y supaya kode
    # dikirim lewat SMS.
    sms = input("Kirim kode lewat SMS? (y/n, default n): ").strip().lower()
    force_sms = sms in ("y", "ya", "yes")

    client = TelegramClient(StringSession(), api_id, api_hash)
    client.connect()
    if not client.is_user_authorized():
        client.send_code_request(phone, force_sms=force_sms)
        code = input("Kode OTP dari Telegram: ").strip()
        try:
            client.sign_in(phone, code)
        except SessionPasswordNeededError:
            password = getpass.getpass("Password verifikasi 2 langkah: ")
            client.sign_in(password=password)

    session_str = client.session.save()
    print()
    print("SESSION_STRING kamu (salin ke GitHub secret, jangan ke chat):")
    print(session_str)
    # Simpan juga ke file, supaya tidak hilang kalau terminal
    # ke-scroll dan gampang disalin. File ini berisi kredensial —
    # jangan dibagikan, dan hapus setelah disalin ke GitHub secret.
    with open("session_string.txt", "w") as f:
        f.write(session_str)
    print()
    print("SESSION_STRING juga tersimpan di session_string.txt")
    print("Lihat isinya: cat session_string.txt")
    client.disconnect()


if __name__ == "__main__":
    main()
