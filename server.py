"""
LINE秘書 - ボイスメモ → 議事録サーバー
LINE音声メッセージ → Whisper文字起こし → Claude議事録 → LINE返信
"""

import os
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
    return {"status": "LINE秘書 稼働中 🤖", "version": "text-chat-v2", "time": datetime.now().isoformat()}


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
        _push(user_id, reply)
    except Exception as e:
        logger.error(f"Text processing error: {e}", exc_info=True)
        _push(user_id, f"⚠️ エラーが発生しました\n{str(e)}")


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
# LINE 送信ヘルパー
# ─────────────────────────────────────────

def _reply(reply_token: str, text: str):
    with ApiClient(line_config) as api:
        MessagingApi(api).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )

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
