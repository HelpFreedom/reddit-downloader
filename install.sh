#!/usr/bin/env bash
# Регистрирует нативный помощник (ffmpeg) расширения Reddit Downloader в Chrome / Chromium / Brave.
# Registers the Reddit Downloader native helper (ffmpeg) with Chrome / Chromium / Brave.
# Запуск / usage: ./install.sh   (повторный запуск безопасен / safe to re-run; удаление / removal: ./uninstall.sh)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_NAME="io.github.helpfreedom.reddit_downloader"
EXT_ID="ppffdkmkldfdncbpjannmploigpjdjgn"   # постоянный, задан полем "key" в extension/manifest.json / fixed by "key"
HOST_PATH="$DIR/host/reddit_downloader_host.py"

for bin in python3 ffmpeg ffprobe nice; do
  command -v "$bin" >/dev/null || {
    echo "Не найден $bin — установите его и запустите снова. / $bin not found — install it and re-run." >&2
    exit 1
  }
done
chmod +x "$HOST_PATH"

# Регистрируем там, где есть профиль браузера. / Register wherever a browser profile exists.
installed=0
for cfg in "$HOME/.config/google-chrome" "$HOME/.config/google-chrome-beta" "$HOME/.config/google-chrome-unstable" \
           "$HOME/.config/chromium" "$HOME/.config/BraveSoftware/Brave-Browser"; do
  [ -d "$cfg" ] || continue
  mkdir -p "$cfg/NativeMessagingHosts"
  cat > "$cfg/NativeMessagingHosts/$HOST_NAME.json" <<EOF
{
  "name": "$HOST_NAME",
  "description": "Reddit Downloader: ffmpeg helper (merges video + audio, converts gif to mp4)",
  "path": "$HOST_PATH",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
EOF
  echo "OK: $cfg/NativeMessagingHosts/$HOST_NAME.json"
  installed=1
done
if [ "$installed" != 1 ]; then
  echo "Не найден профиль Chrome/Chromium/Brave в ~/.config. Запустите браузер один раз и повторите." >&2
  echo "No Chrome/Chromium/Brave profile found in ~/.config. Start the browser once and re-run." >&2
  exit 1
fi

cat <<EOF

Дальше / Next:
  1. chrome://extensions → «Режим разработчика» / "Developer mode" → ON
  2. «Загрузить распакованное» / "Load unpacked" → $DIR/extension
  3. ID: $EXT_ID
EOF
