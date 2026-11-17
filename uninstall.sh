#!/usr/bin/env bash
# Убирает регистрацию нативного помощника. Файлы проекта и загрузки не трогает.
# Removes the native helper registration. Project files and downloads are left untouched.
set -euo pipefail
HOST_NAME="io.github.helpfreedom.reddit_downloader"
for cfg in "$HOME/.config/google-chrome" "$HOME/.config/google-chrome-beta" "$HOME/.config/google-chrome-unstable" \
           "$HOME/.config/chromium" "$HOME/.config/BraveSoftware/Brave-Browser"; do
  f="$cfg/NativeMessagingHosts/$HOST_NAME.json"
  if [ -f "$f" ]; then rm -- "$f"; echo "Удалён / removed: $f"; fi
done
echo "Журнал помощника / helper log: ~/.cache/reddit-downloader (можно удалить вручную / safe to delete manually)"
