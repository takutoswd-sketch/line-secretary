"""
LINE秘書 - ボイスメモ → 議事録サーバー
LINE音声メッセージ → Whisper文字起こし → Claude議事録 → LINE返信
LINEレシート画像 → Claude Vision読み取り → スプレッドシート記帳 → LINE返信
"""

import os
import base64
import tempfile
import logging
import json
from datetime import datetime
from pathlib import Path

import httpx
import openai
import anthropic
import uvicorn
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from linebot.v3 import WebhookParser
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, AudioMessageContent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError

# ─────────────────────────────────────────
# 設定読み込み
# ─────────────────────────────────────────

CONFIG_DIR = Path.home() / "Documents/Claude/Scheduled/config"

def load_env():
    env_file = CONFIG_DIR / "line_secretary.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

load_env()

# 環境変数優先、なければファイルから読む（ローカル用）
def _read_token():
    if os.environ.get("LINE_TOKEN"):
        return os.environ["LINE_TOKEN"].strip()
    token_file = CONFIG_DIR / "line_token.txt"
    if token_file.exists():
        return token_file.read_text().splitlines()[0].strip()
    return ""

LINE_TOKEN          = _read_token()
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

# ─────────────────────────────────────────
# クライアント初期化
# ─────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        *([logging.FileHandler(CONFIG_DIR / "line_secretary.log")]
          if CONFIG_DIR.exists() else []),
    ]
)
logger = logging.getLogger(__name__)

line_config = Configuration(access_token=LINE_TOKEN)
parser = WebhookParser(LINE_CHANNEL_SECRET)
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

app = FastAPI(title="LINE秘書")

# ─────────────────────────────────────────
# Webhook エンドポイント
# ─────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "LINE秘書 稼働中 🤖", "time": datetime.now().isoformat()}



@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    try:
        events = parser.parse(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        logger.warning("Invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac", ".mp4"}
    MAC_SERVER_URL = os.environ.get("MAC_SERVER_URL", "").rstrip("/")

    for event in events:
        msg = getattr(event, "message", None)
        msg_type = getattr(msg, "type", "N/A")
        logger.info(f"Event: {type(event).__name__}, msg_type: {msg_type}")

        if not isinstance(event, MessageEvent):
            continue

        # LINEボイスメッセージ（audio）またはボイスメモファイル（file）
        is_audio = msg_type == "audio"
        is_audio_file = (
            msg_type == "file" and
            any(getattr(msg, "file_name", "").lower().endswith(ext) for ext in AUDIO_EXTENSIONS)
        )

        if is_audio or is_audio_file:
            user_id = event.source.user_id
            msg_id  = msg.id
            logger.info(f"Audio detected! user={user_id}, msg={msg_id}, type={msg_type}")
            background_tasks.add_task(_push, user_id, "🎙️ 音声を解析中...\n議事録ができたらお送りします")
            background_tasks.add_task(process_voice_to_minutes, msg_id, user_id)

        elif msg_type == "video":
            user_id = event.source.user_id
            msg_id  = msg.id
            logger.info(f"Video detected! user={user_id}, msg={msg_id}")
            if MAC_SERVER_URL:
                background_tasks.add_task(forward_video_to_mac, msg_id, user_id, MAC_SERVER_URL)
            else:
                background_tasks.add_task(_push, user_id, "⚠️ 動画処理サーバー未設定（MAC_SERVER_URL）")

        elif msg_type == "image":
            user_id = event.source.user_id
            msg_id  = msg.id
            logger.info(f"Image detected! user={user_id}, msg={msg_id}")
            background_tasks.add_task(_push, user_id, "🧾 レシートを読み取り中...")
            background_tasks.add_task(process_receipt, msg_id, user_id)

        elif msg_type == "text":
            user_id = event.source.user_id
            text = getattr(msg, "text", "")
            logger.info(f"Text message: user={user_id}, text={text[:50]}")
            background_tasks.add_task(process_text_message, text, user_id)

    return JSONResponse({"status": "ok"})


# ─────────────────────────────────────────
# 音声処理パイプライン
# ─────────────────────────────────────────

def process_voice_to_minutes(message_id: str, user_id: str):
    """音声ダウンロード → 文字起こし → 議事録生成 → LINE送信"""
    try:
        logger.info(f"Processing audio: {message_id}")

        # 1. 音声ダウンロード
        audio_path = _download_audio(message_id)

        # 2. Whisper で文字起こし
        transcript = _transcribe(audio_path)
        logger.info(f"Transcript ({len(transcript)}字): {transcript[:80]}...")

        # 3. Claude で議事録フォーマット
        minutes = _format_minutes(transcript)

        # 4. LINE に送信
        _push(user_id, f"📝 議事録\n\n{minutes}")

        # 5. 一時ファイル削除
        os.unlink(audio_path)
        logger.info("Done")

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        _push(user_id, f"⚠️ エラーが発生しました\n{str(e)}")


SYSTEM_PROMPT = """あなたは澤田拓人専用のLINE秘書AIです。
澤田拓人について：建築設計事務所とAIコンサルとして独立。売上目標1000万円。
主な仕事は建築デザイン、AIコンサル、アプリ開発、現場監督。
note・Instagram・X（Twitter）でSNS発信中。

返答スタイル：
- LINEのチャットなので短く簡潔に
- 結論から先に
- 敬語不要、フランクに
- 箇条書きより会話調で"""

def process_text_message(text: str, user_id: str):
    """テキストメッセージをClaudeで処理して返信"""
    try:
        today = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        msg = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"[{today}]\n{text}"
            }]
        )
        reply = msg.content[0].text
        _broadcast(reply)
    except Exception as e:
        logger.error(f"Text processing error: {e}", exc_info=True)
        _broadcast(f"⚠️ エラーが発生しました\n{str(e)}")


def forward_video_to_mac(message_id: str, user_id: str, mac_url: str):
    """動画処理タスクをMacローカルサーバーに転送"""
    try:
        with httpx.Client(timeout=10) as client:
            res = client.post(
                f"{mac_url}/process-video",
                json={"message_id": message_id, "user_id": user_id},
            )
            res.raise_for_status()
        logger.info(f"動画タスク転送完了 → {mac_url}")
    except Exception as e:
        logger.error(f"Mac転送エラー: {e}")
        _push(user_id, f"⚠️ 動画処理サーバーに接続できませんでした\n（Macのvideo_processorは起動していますか？）")


def _download_audio(message_id: str) -> str:
    """LINE Content API から音声をダウンロード"""
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_TOKEN}"}

    with httpx.Client(timeout=30) as client:
        res = client.get(url, headers=headers)
        res.raise_for_status()

    tmp = tempfile.NamedTemporaryFile(suffix=".m4a", delete=False)
    tmp.write(res.content)
    tmp.close()
    return tmp.name


def _transcribe(audio_path: str) -> str:
    """OpenAI Whisper で日本語文字起こし"""
    with open(audio_path, "rb") as f:
        result = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="ja",
            response_format="text",
        )
    return result


def _format_minutes(transcript: str) -> str:
    """Claude で議事録に整形（LINEのスマホ表示向け）"""
    today = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    msg = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": f"""以下の音声書き起こしを議事録にまとめてください。
LINEのスマホ画面で読みやすいよう、簡潔にまとめてください。

書き起こし：
{transcript}

出力形式（このまま使う）：
{today}

■ 決定事項
・（重要な決定を箇条書き。なければ「なし」）

■ TODO・アクション
・（誰が・何を・いつまでに。なければ「なし」）

■ 備考
・（重要な補足のみ。なければ省略）

余計な前置きなく、上記形式だけで回答してください。"""
        }]
    )
    return msg.content[0].text


# ─────────────────────────────────────────
# レシート処理パイプライン
# ─────────────────────────────────────────

GAS_WEBHOOK_URL = os.environ.get("GAS_WEBHOOK_URL", "").strip()

RECEIPT_PROMPT = """このレシート/領収書の画像から情報を読み取り、JSONのみで回答してください。

読み取れない項目はnullにしてください。金額は数値（円、カンマなし）。

勘定科目は以下から最適なものを選択（個人事業主・建築設計/AIコンサル/アプリ開発/現場監督業）：
消耗品費 / 旅費交通費 / 接待交際費 / 会議費 / 通信費 / 車両費 / 燃料費 /
新聞図書費 / 外注費 / 地代家賃 / 水道光熱費 / 修繕費 / 広告宣伝費 / 支払手数料 / 雑費

出力形式（JSONのみ、説明文なし）：
{
  "is_receipt": true,
  "date": "2026-07-02",
  "store": "店名",
  "total": 1234,
  "tax": 112,
  "category": "消耗品費",
  "payment": "現金 / クレジット / 電子マネー / 不明",
  "items": "主な購入品の要約（20字以内）",
  "confidence": "high / medium / low"
}

レシートや領収書でない画像の場合は {"is_receipt": false} のみ返してください。"""


def process_receipt(message_id: str, user_id: str):
    """レシート画像 → Claude Vision → スプレッドシート記帳 → LINE返信"""
    try:
        logger.info(f"Processing receipt: {message_id}")

        # 1. 画像ダウンロード
        image_bytes, media_type = _download_image(message_id)
        image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")

        # 2. Claude Vision で読み取り
        data = _extract_receipt(image_b64, media_type)

        if not data.get("is_receipt"):
            _push(user_id, "🤔 レシートとして認識できませんでした。\n明るい場所で全体が写るように撮り直してみてください。")
            return

        # 3. スプレッドシートに記帳（GAS Webhook）
        if not GAS_WEBHOOK_URL:
            _push(user_id, "⚠️ スプレッドシート未設定（GAS_WEBHOOK_URL）\n読み取り結果:\n" + json.dumps(data, ensure_ascii=False, indent=1))
            return

        payload = {
            "date": data.get("date"),
            "store": data.get("store"),
            "total": data.get("total"),
            "tax": data.get("tax"),
            "category": data.get("category"),
            "payment": data.get("payment"),
            "items": data.get("items"),
            "confidence": data.get("confidence"),
            "image_base64": image_b64,
            "media_type": media_type,
        }
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            res = client.post(GAS_WEBHOOK_URL, json=payload)
            res.raise_for_status()
            gas_result = res.json()

        # 4. LINE に確認メッセージ
        total = data.get("total")
        total_str = f"¥{total:,}" if isinstance(total, (int, float)) else "不明"
        note = "" if data.get("confidence") == "high" else "\n⚠️ 読み取り精度が低めです。シートを確認してください。"
        row = gas_result.get("row", "?")
        _push(
            user_id,
            f"✅ 記帳しました（{row}行目）\n\n"
            f"📅 {data.get('date') or '日付不明'}\n"
            f"🏪 {data.get('store') or '店名不明'}\n"
            f"💰 {total_str}\n"
            f"📂 {data.get('category') or '未分類'}\n"
            f"💳 {data.get('payment') or '不明'}"
            f"{note}"
        )
        logger.info(f"Receipt logged: {data.get('store')} {total}")

    except Exception as e:
        logger.error(f"Receipt error: {e}", exc_info=True)
        _push(user_id, f"⚠️ レシート処理でエラーが発生しました\n{str(e)}")


def _download_image(message_id: str) -> tuple[bytes, str]:
    """LINE Content API から画像をダウンロード"""
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_TOKEN}"}
    with httpx.Client(timeout=30) as client:
        res = client.get(url, headers=headers)
        res.raise_for_status()
    media_type = res.headers.get("Content-Type", "image/jpeg").split(";")[0]
    return res.content, media_type


def _extract_receipt(image_b64: str, media_type: str) -> dict:
    """Claude Vision でレシート情報を抽出"""
    msg = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": RECEIPT_PROMPT},
            ],
        }],
    )
    text = msg.content[0].text.strip()
    # コードブロックで囲まれた場合に対応
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


# ─────────────────────────────────────────
# LINE 送信ヘルパー
# ─────────────────────────────────────────

def _reply(reply_token: str, text: str):
    with ApiClient(line_config) as api:
        MessagingApi(api).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )

def _broadcast(text: str):
    url = "https://api.line.me/v2/bot/message/broadcast"
    token = LINE_TOKEN.encode("ascii", errors="ignore").decode("ascii")
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"messages": [{"type": "text", "text": text}]}
    with httpx.Client(timeout=30) as client:
        res = client.post(url, headers=headers, json=payload)
        res.raise_for_status()

def _push(user_id: str, text: str):
    url = "https://api.line.me/v2/bot/message/push"
    token = LINE_TOKEN.encode("ascii", errors="ignore").decode("ascii")
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"to": user_id, "messages": [{"type": "text", "text": text}]}
    with httpx.Client(timeout=30) as client:
        res = client.post(url, headers=headers, json=payload)
        res.raise_for_status()


# ─────────────────────────────────────────
# 起動
# ─────────────────────────────────────────

if __name__ == "__main__":
    logger.info("LINE秘書 起動中... port 8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
