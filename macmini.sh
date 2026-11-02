#!/bin/bash
# 맥미니에서 앱 서버를 상시 실행한다: 로그인하면 자동으로 켜지고, 죽으면 다시 켜진다.
#   ./macmini.sh [LIBRARY_DIR] [--lan]   설치 또는 최신 코드로 갱신 (다시 실행해도 됨)
#   ./macmini.sh --uninstall             자동 실행 해제 (진도·단어장은 남음)
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Library/Application Support/FriendsEnglish/app"  # Dropbox 폴더는 백그라운드 실행 시 권한 문제가 있어 복사해서 쓴다
LABEL=local.friends-english
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/friends-english.log"
DOMAIN="gui/$(id -u)"
LIB="$HOME/Movies/Friends"
LAN_ARG=""

for a in "$@"; do
  case "$a" in
    --uninstall)
      launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
      rm -f "$PLIST"
      echo "자동 실행을 해제했습니다. (진도·단어장: ~/Library/Application Support/FriendsEnglish)"
      exit 0 ;;
    --lan) LAN_ARG="<string>--lan</string>" ;;
    -*) echo "모르는 옵션: $a" >&2; exit 1 ;;
    *) LIB="$(cd "$a" 2>/dev/null && pwd || echo "$a")" ;;
  esac
done

[ -x /usr/bin/python3 ] || { echo "python3가 없습니다: xcode-select --install" >&2; exit 1; }
for t in yt-dlp ffmpeg; do
  PATH="/opt/homebrew/bin:/usr/local/bin:$PATH" command -v "$t" >/dev/null || echo "[주의] $t 없음 → brew install $t"
done

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  sleep 1
  lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1 && { echo "8765 포트를 이미 다른 프로그램이 쓰고 있습니다:" >&2; lsof -nP -iTCP:8765 -sTCP:LISTEN >&2; exit 1; }
fi

mkdir -p "$APP" "$LIB" "$(dirname "$PLIST")" "$(dirname "$LOG")"
cp "$SRC"/serve.py "$SRC"/captions.py "$SRC"/prep.py "$SRC"/index.html "$SRC"/icon.png "$SRC"/catalog.json "$APP"/
mkdir -p "$APP/study-captions"
cp "$SRC"/study-captions/*.srt "$APP/study-captions"/

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$APP/serve.py</string>
    <string>$LIB</string>
    <string>--no-open</string>
    $LAN_ARG
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

for i in 1 2 3 4 5; do
  launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && break
  sleep 1  # 직전 실행이 완전히 내려가기를 기다림
done
for i in 1 2 3 4 5 6 7 8 9 10; do
  curl -s -o /dev/null http://localhost:8765/api/state && break
  sleep 0.5
done
if ! curl -s -o /dev/null http://localhost:8765/api/state; then
  echo "서버가 뜨지 않았습니다. 로그: $LOG" >&2
  tail -5 "$LOG" >&2
  exit 1
fi

echo "실행 중: http://localhost:8765   (라이브러리: $LIB, 로그: $LOG)"
if [ -n "$LAN_ARG" ]; then
  echo "집 Wi-Fi의 다른 기기: http://$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<맥미니 IP>'):8765"
fi
TS="$(command -v tailscale || true)"
[ -z "$TS" ] && [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ] && TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
if [ -n "$TS" ]; then
  NAME="$("$TS" status --json 2>/dev/null | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
  echo "밖에서 쓰기: $TS serve --bg 8765   →  https://${NAME:-<맥미니>.<tailnet>.ts.net}/"
else
  echo "밖에서(아이폰·회사) 쓰려면: Tailscale 설치·로그인 후  tailscale serve --bg 8765"
fi
