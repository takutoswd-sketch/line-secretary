#!/bin/bash
# LINE秘書 セットアップスクリプト
# 実行: bash setup.sh

set -e
CONFIG_DIR="$HOME/Documents/Claude/Scheduled/config"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== LINE秘書 セットアップ ==="

# ── 1. Python依存パッケージインストール ──
echo ""
echo "[1/4] Pythonパッケージをインストール中..."
pip3 install -r "$SCRIPT_DIR/requirements.txt" -q 2>/dev/null || \
pip3 install -r "$SCRIPT_DIR/requirements.txt" --break-system-packages -q
echo "✅ パッケージインストール完了"

# ── 2. 環境変数ファイル作成 ──
echo ""
echo "[2/4] 設定ファイルを確認..."
ENV_FILE="$CONFIG_DIR/line_secretary.env"

if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'EOF'
# LINE秘書 設定ファイル
# ============================================================
# LINE Developers Console → Channel Secret をコピー
LINE_CHANNEL_SECRET=ここに入力

# OpenAI API Key (https://platform.openai.com/api-keys)
OPENAI_API_KEY=sk-ここに入力

# Anthropic API Key (https://console.anthropic.com/)
ANTHROPIC_API_KEY=sk-ant-ここに入力
EOF
    echo "📝 設定ファイルを作成しました: $ENV_FILE"
    echo "   → 上記ファイルにAPIキーを入力してから再実行してください"
else
    echo "✅ 設定ファイルあり: $ENV_FILE"
fi

# ── 3. cloudflared インストール確認 ──
echo ""
echo "[3/4] Cloudflare Tunnel (cloudflared) を確認..."
if ! command -v cloudflared &> /dev/null; then
    echo "📦 cloudflaredをインストール中..."
    if command -v brew &> /dev/null; then
        brew install cloudflared
    else
        echo "⚠️  Homebrewが見つかりません。手動でインストールしてください:"
        echo "   https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
        exit 1
    fi
fi
echo "✅ cloudflared: $(cloudflared --version)"

# ── 4. 動作確認 ──
echo ""
echo "[4/4] 設定ファイルの確認..."
source "$ENV_FILE" 2>/dev/null || true

if [ -z "$OPENAI_API_KEY" ] || [ "$OPENAI_API_KEY" = "sk-ここに入力" ]; then
    echo ""
    echo "⚠️  OpenAI APIキーが未設定です"
    echo "   取得先: https://platform.openai.com/api-keys"
    echo "   設定先: $ENV_FILE"
else
    echo "✅ OpenAI APIキー: 設定済み"
fi

if [ -z "$LINE_CHANNEL_SECRET" ] || [ "$LINE_CHANNEL_SECRET" = "ここに入力" ]; then
    echo ""
    echo "⚠️  LINE Channel Secretが未設定です"
    echo "   取得先: LINE Developers Console → チャンネル → Channel Secret"
    echo "   設定先: $ENV_FILE"
else
    echo "✅ LINE Channel Secret: 設定済み"
fi

echo ""
echo "=== セットアップ完了 ==="
echo ""
echo "次のステップ:"
echo "  1. $ENV_FILE にAPIキーを入力"
echo "  2. bash start.sh でサーバー起動"
echo "  3. 表示されたURLをLINE Developers ConsoleのWebhook URLに設定"
