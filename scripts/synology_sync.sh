#!/bin/sh
# 把美股篩選網頁和每日資料從 GitHub 同步到 Synology NAS 的 Web Station 資料夾。
# 在 DSM「控制台 → 任務排程表」新增「使用者定義的指令碼」，每天執行一次（建議 07:30，GitHub 每天 06:41 更新資料）：
#   sh /volume1/web/stocks/synology_sync.sh
# 可以用環境變數改設定，例如：DEST=/volume1/web/invest sh synology_sync.sh
# 只用 DSM 內建的 sh 和 curl，不需要安裝其他套件。
set -u

REPO="${REPO:-zero000ryu/isaac-Module-}"
BRANCH="${BRANCH:-main}"
DEST="${DEST:-/volume1/web/stocks}"
BASE="${BASE:-https://raw.githubusercontent.com/$REPO/$BRANCH}"
# 如果 repo 改成私人，請建立只有讀取 Contents 權限的 GitHub token，填到 SYNC_GITHUB_TOKEN
SYNC_GITHUB_TOKEN="${SYNC_GITHUB_TOKEN:-}"
FILES="stocks.html data/stocks.json data/news.json"
LOG="$DEST/sync.log"

mkdir -p "$DEST/data" || { echo "無法建立 $DEST，請確認 Web Station 的 web 共用資料夾存在" >&2; exit 1; }
TMP="$DEST/.sync-tmp"
rm -rf "$TMP" && mkdir -p "$TMP/data"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

fetch() {  # fetch <path> <out>
  if [ -n "$SYNC_GITHUB_TOKEN" ]; then
    curl -fsSL --retry 3 --retry-delay 10 --max-time 300 \
      -H "Authorization: Bearer $SYNC_GITHUB_TOKEN" -H "Accept: application/vnd.github.raw" \
      -o "$2" "https://api.github.com/repos/$REPO/contents/$1?ref=$BRANCH"
  else
    curl -fsSL --retry 3 --retry-delay 10 --max-time 300 -o "$2" "$BASE/$1"
  fi
}

ok=0; fail=0
for f in $FILES; do
  if fetch "$f" "$TMP/$f" && [ -s "$TMP/$f" ]; then
    case "$f" in
      *.json) head -c 1 "$TMP/$f" | grep -q '{' || { log "略過 $f：內容不是 JSON"; fail=$((fail+1)); continue; } ;;
      *.html) grep -q '</html>' "$TMP/$f" || { log "略過 $f：檔案不完整"; fail=$((fail+1)); continue; } ;;
    esac
    mv -f "$TMP/$f" "$DEST/$f"   # 下載完整才取代舊檔，網頁不會讀到一半的檔案
    ok=$((ok+1))
  else
    log "下載失敗：$f（保留舊檔）"; fail=$((fail+1))
  fi
done
rm -rf "$TMP"

# 打開資料夾網址時直接進入美股篩選
if [ ! -f "$DEST/index.html" ] || grep -q 'stocks-redirect' "$DEST/index.html"; then
  cat > "$DEST/index.html" <<'HTML'
<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><!-- stocks-redirect -->
<meta http-equiv="refresh" content="0; url=stocks.html"><title>美股篩選</title></head>
<body><a href="stocks.html">美股篩選</a></body></html>
HTML
fi

# 日誌只留最後 200 行
if [ -f "$LOG" ]; then tail -n 200 "$LOG" > "$LOG.tmp" && mv -f "$LOG.tmp" "$LOG"; fi
log "同步完成：成功 $ok 個，失敗 $fail 個 → $DEST"
[ "$fail" -eq 0 ]
