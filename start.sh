#!/bin/bash
# LINE秘書 起動スクリプト
# ターミナルで: bash start.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="$HOME/Documents/Claude/Scheduled/config"
LOG_DIR="$CONFIG_DIR"

echo "=== LINE秘書 起動 ==="

# 設定読み込み
source "$CONFIG_DIR/line_secretary.env" 2>/dev/null || {
    echo "❌ $CONFIG_DIR/line_secretary.env が見つかりません"
    echo "   先に bash setup.sh を実行してください"
    exit 1
}

export LINE_CHANNEL_SECRET OPENAI_API_KEY ANTHROPIC_API_KEY

# ── サーバーをバックグラウンドで起動 ──
echo "🚀 Webhookサーバーを起動中 (port 8000)..."
cd "$SCRIPT_DIR"
python3 server.py &
SERVER_PID=$!
sleep 2

# サーバー起動確認
if ! curl -s http://localhost:8000/ > /dev/null 2>&1; then
    echo "❌ サーバー起動失敗。ログを確認してください: $LOG_DIR/line_secretary.log"
    exit 1
fi
echo "✅ サーバー起動完了 (PID: $SERVER_PID)"

# ── Cloudflare Tunnel で外部公開 ──
echo ""
echo "🌐 Cloudflare Tunnelを起動中..."
echo "   (表示されたURLをLINE Developers ConsoleのWebhook URLに設定してください)"
echo "   例: https://xxxx-xxxx.trycloudflare.com/webhook"
echo ""
echo "⚠️  このターミナルを閉じるとサーバーが停止します"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# cloudflaredを前面で起動（URLが表示される）
trap "kill $SERVER_PID 2>/dev/null; echo ''; echo 'サーバーを停止しました'" EXIT
cloudflared tunnel --url http://localhost:8000
