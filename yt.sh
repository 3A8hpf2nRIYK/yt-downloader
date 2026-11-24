#!/bin/bash
# yt.sh - Download YouTube pakai yt-dlp, pilih v1/v2 lalu pilih kualitas
#
# Cara pakai (di Termux, di folder yang ada mycookies.txt):
#   chmod +x yt.sh
#   ./yt.sh
#   ./yt.sh "https://youtu.be/VIDEO_ID"        # link langsung, versi tetap ditanya
#   ./yt.sh "https://youtu.be/VIDEO_ID" 2       # link + versi 2 langsung
#
# Alur:
#   1. Tempel link YouTube
#   2. Pilih versi:
#      v1 = yt-dlp --cookies mycookies.txt --js-runtimes node --remote-components ejs:github --sleep-requests 3 -F <link>
#      v2 = yt-dlp --cookies mycookies.txt --js-runtimes deno --remote-components ejs:github --sleep-requests 3 -F <link>
#   3. Muncul menu kualitas dari hasil -F itu, sampai 8K (4320p).
#      Hanya kualitas yang benar-benar tersedia yang ditampilkan,
#      jadi kalau videonya cuma sampai 1080p, 4K/8K tidak akan muncul.
#      FPS ikut ditampilkan, misal 720p60, 1080p60, 1080p30.
#   4. Pilih nomor kualitas, video langsung di-download (video + audio terbaik).

COOKIES_FILE="mycookies.txt"

# --- 1. Link ---
LINK="$1"
if [ -z "$LINK" ]; then
  read -r -p "Tempel link YouTube: " LINK
fi
if [ -z "$LINK" ]; then
  echo "Link kosong, batal."
  exit 1
fi

# --- Cek cookies ---
if [ ! -f "$COOKIES_FILE" ]; then
  echo "Peringatan: file $COOKIES_FILE tidak ketemu di folder ini."
  echo "yt-dlp kemungkinan akan kena bot / gagal. Tekan Enter untuk tetap lanjut, atau Ctrl+C untuk batal."
  read -r
fi

# --- 2. Pilih versi v1 / v2 ---
VER="$2"
if [ -z "$VER" ]; then
  echo ""
  echo "Pilih versi:"
  echo "  1) v1 - node + --sleep-requests 3"
  echo "  2) v2 - deno + --sleep-requests 3"
  read -r -p "Pilih [1/2]: " VER
fi

case "$VER" in
  1)
    CMD=(yt-dlp --cookies "$COOKIES_FILE" --js-runtimes node --remote-components ejs:github --sleep-requests 3)
    ;;
  2)
    CMD=(yt-dlp --cookies "$COOKIES_FILE" --js-runtimes deno --remote-components ejs:github --sleep-requests 3)
    ;;
  *)
    echo "Pilihan tidak valid: $VER (pilih 1 atau 2)"
    exit 1
    ;;
esac

# --- Ambil daftar format dengan -F ---
TMP_FORMATS="$(mktemp)"
trap 'rm -f "$TMP_FORMATS"' EXIT

echo ""
echo "Mengambil daftar format..."
if ! "${CMD[@]}" -F "$LINK" > "$TMP_FORMATS" 2>&1; then
  echo "Gagal mengambil daftar format. Output yt-dlp:"
  cat "$TMP_FORMATS"
  exit 1
fi

# --- 3. Parse hasil -F: ambil kombinasi tinggi + fps yang unik ---
# Pola di output -F: "1920x1080 30", "1280x720 60", dst.
# Audio only ("audio only") dan storyboard tidak cocok pola ini, jadi otomatis ke-skip.
# Batas atas 4320p = 8K. Kalau tidak ada 4320p di hasil -F, 8K tidak akan muncul di menu.
declare -a OPTIONS=()
while IFS= read -r line; do
  # Skip storyboard (sb0, sb1, ...) - itu gambar thumbnail, bukan video
  first="${line%% *}"
  if [[ "$first" == sb* ]]; then
    continue
  fi
  if [[ "$line" =~ ([0-9]+)x([0-9]+)[[:space:]]+([0-9]+) ]]; then
    h="${BASH_REMATCH[2]}"
    fps="${BASH_REMATCH[3]}"
  elif [[ "$line" =~ ([0-9]+)x([0-9]+) ]]; then
    h="${BASH_REMATCH[2]}"
    fps="30"
  else
    continue
  fi
  if [ "$h" -lt 144 ] || [ "$h" -gt 4320 ]; then
    continue
  fi
  OPTIONS+=("$h $fps")
done < "$TMP_FORMATS"

# Hilangkan duplikat (misal 1080p60 ada di avc dan vp9), urutkan dari kecil ke besar
if [ "${#OPTIONS[@]}" -eq 0 ]; then
  echo "Tidak ada format video yang ketemu."
  echo "Kemungkinan kena bot / cookies sudah kedaluwarsa. Output yt-dlp:"
  cat "$TMP_FORMATS"
  exit 1
fi
mapfile -t OPTIONS < <(printf "%s\n" "${OPTIONS[@]}" | sort -n -k1,1 -k2,2 -u)

# --- Tampilkan menu kualitas ---
echo ""
echo "Pilih kualitas (hanya yang tersedia):"
i=1
for opt in "${OPTIONS[@]}"; do
  h="${opt%% *}"
  fps="${opt##* }"
  label="${h}p${fps}"
  if [ "$h" -eq 2160 ]; then
    label="$label (4K)"
  elif [ "$h" -eq 4320 ]; then
    label="$label (8K)"
  fi
  echo "  $i) $label"
  i=$((i + 1))
done
echo "  0) Batal"

read -r -p "Pilih [0-${#OPTIONS[@]}]: " PILIH
if [ "$PILIH" = "0" ] || [ -z "$PILIH" ]; then
  echo "Batal."
  exit 0
fi
if ! [[ "$PILIH" =~ ^[0-9]+$ ]] || [ "$PILIH" -lt 1 ] || [ "$PILIH" -gt "${#OPTIONS[@]}" ]; then
  echo "Pilihan tidak valid: $PILIH"
  exit 1
fi

SELECTED="${OPTIONS[$((PILIH - 1))]}"
H="${SELECTED%% *}"
FPS="${SELECTED##* }"
echo ""
echo "Download: ${H}p${FPS} ..."

# --- 4. Download kualitas yang dipilih ---
# bv[height=H][fps<=FPS] = video terbaik dengan tinggi & fps itu (atau di bawahnya kalau fps persisnya tidak ada)
# +ba = audio terbaik, digabung otomatis oleh yt-dlp/ffmpeg.
FORMAT="bv[height=${H}][fps<=${FPS}]+ba/b[height=${H}][fps<=${FPS}]"
"${CMD[@]}" -f "$FORMAT" -o "%(title)s [${H}p${FPS}].%(ext)s" "$LINK"
