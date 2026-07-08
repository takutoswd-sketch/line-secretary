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
from zoneinfo import ZoneInfo
from pathlib import Path

JST = ZoneInfo("Asia/Tokyo")

def now_jst() -> datetime:
    """日本時間の現在時刻（サーバーはUTCで動いているため必ずこれを使う）"""
    return datetime.now(JST)

import subprocess
import threading
import time

import imageio_ffmpeg
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

import httpx
import openai
import anthropic
import uvicorn
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
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
NOTIFY_KEY          = os.environ.get("NOTIFY_KEY", "").strip()
LINE_USER_ID        = os.environ.get("LINE_USER_ID", "").strip()
RAILWAY_DOMAIN      = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
MAC_SERVER_URL      = os.environ.get("MAC_SERVER_URL", "").strip().rstrip("/")

# 動画一時保存ディレクトリ（Railway の /tmp は再起動でリセットされる）
VIDEO_TMP_DIR = Path("/tmp/line_videos")
VIDEO_TMP_DIR.mkdir(parents=True, exist_ok=True)

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
    return {"status": "LINE秘書 稼働中 🤖", "time": now_jst().isoformat()}


@app.get("/video/{filename}")
def serve_video(filename: str):
    """動画・サムネイルファイルを一時提供（LINE動画メッセージ用）"""
    path = VIDEO_TMP_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    media_type = "video/mp4" if filename.endswith(".mp4") else "image/jpeg"
    return FileResponse(str(path), media_type=media_type)



@app.get("/notify")
def notify(key: str = "", text: str = ""):
    """スケジュールタスク等からのLINE通知中継（GET）。
    実行環境からLINE APIへ直接アクセスできないため、このサーバーが中継する。"""
    if not NOTIFY_KEY or key != NOTIFY_KEY:
        raise HTTPException(status_code=403, detail="invalid key")
    if not text.strip():
        raise HTTPException(status_code=400, detail="text required")
    if not LINE_USER_ID:
        raise HTTPException(status_code=500, detail="LINE_USER_ID not set")
    _push(LINE_USER_ID, text[:4900])
    logger.info(f"Notify sent ({len(text)} chars)")
    return {"status": "sent"}


@app.get("/job/transcribe")
def job_transcribe(background_tasks: BackgroundTasks, key: str = "", name: str = ""):
    """リポジトリ同梱の音声ファイル（transcribe_queue/）をWhisperで文字起こし（非同期）"""
    if not NOTIFY_KEY or key != NOTIFY_KEY:
        raise HTTPException(status_code=403, detail="invalid key")
    audio = Path(__file__).parent / "transcribe_queue" / name
    if not audio.exists():
        raise HTTPException(status_code=404, detail=f"not found: {name}")
    background_tasks.add_task(_transcribe_to_tmp, audio)
    return {"status": "started", "file": name}


def _transcribe_to_tmp(audio_path: Path):
    out = Path("/tmp") / (audio_path.stem + ".txt")
    try:
        logger.info(f"Transcribing: {audio_path.name}")
        with open(audio_path, "rb") as f:
            text = openai_client.audio.transcriptions.create(
                model="whisper-1", file=f, language="ja", response_format="text",
            )
        out.write_text(text)
        logger.info(f"Transcribed: {len(text)} chars")
    except Exception as e:
        out.write_text(f"ERROR: {e}")
        logger.error(f"Transcribe error: {e}", exc_info=True)


@app.get("/job/result")
def job_result(key: str = "", name: str = ""):
    """文字起こし結果の取得"""
    if not NOTIFY_KEY or key != NOTIFY_KEY:
        raise HTTPException(status_code=403, detail="invalid key")
    out = Path("/tmp") / (Path(name).stem + ".txt")
    if not out.exists():
        return {"status": "processing"}
    return {"status": "done", "text": out.read_text()}


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

        elif msg_type == "video":
            user_id = event.source.user_id
            msg_id  = msg.id
            logger.info(f"Video detected! user={user_id}, msg={msg_id}")
            background_tasks.add_task(process_video_on_railway, msg_id, user_id)

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
        today = now_jst().strftime("%Y年%m月%d日 %H:%M")
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


def _try_mac_process(message_id: str, user_id: str) -> bool:
    """Mac（video_processor.py port8001）に処理を委譲。成功したらTrue。
    Macはロゴ・フックテキスト・BGM入りのフル版reel変換を行い、LINE返信まで自前で実施する。"""
    if not MAC_SERVER_URL:
        logger.info("MAC_SERVER_URL未設定 → Railwayで簡易変換")
        return False
    try:
        with httpx.Client(timeout=5) as client:
            client.get(f"{MAC_SERVER_URL}/").raise_for_status()
    except Exception as e:
        logger.warning(f"Macサーバー未応答 → Railwayで簡易変換: {e}")
        return False
    try:
        with httpx.Client(timeout=600) as client:
            res = client.post(
                f"{MAC_SERVER_URL}/process-video",
                json={"message_id": message_id, "user_id": user_id},
            )
            res.raise_for_status()
        logger.info("Mac側でreel変換完了（フル版）")
        return True
    except Exception as e:
        logger.error(f"Mac処理失敗 → Railwayフォールバック: {e}")
        return False


def process_video_on_railway(message_id: str, user_id: str):
    """動画処理。まずMac（フル版）へ委譲し、不可ならRailway上で簡易reel変換してLINE返信"""
    # Macが生きていればフル版（ロゴ・フック・BGM入り）で処理
    if _try_mac_process(message_id, user_id):
        return

    try:
        if not RAILWAY_DOMAIN:
            _push(user_id, "⚠️ RAILWAY_PUBLIC_DOMAIN が未設定です")
            return

        _push(user_id, "🎬 動画を受信しました。リール変換中...（簡易版）")

        # 1. ダウンロード
        input_path = _download_video(message_id)

        # 2. reel変換（9:16・ぼかし背景・カラーグレード）
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_name   = f"reel_{ts}.mp4"
        thumb_name = f"thumb_{ts}.jpg"
        out_path   = str(VIDEO_TMP_DIR / out_name)
        thumb_path = str(VIDEO_TMP_DIR / thumb_name)

        _reel_convert(input_path, out_path)
        _extract_video_thumbnail(out_path, thumb_path)

        # 3. LINE に動画送信
        base = f"https://{RAILWAY_DOMAIN}"
        _push_video(user_id,
                    f"{base}/video/{out_name}",
                    f"{base}/video/{thumb_name}")

        # 4. 入力削除・出力は10分後に削除
        os.unlink(input_path)
        _schedule_delete([out_path, thumb_path], delay=600)
        logger.info(f"動画処理完了: {out_name}")

    except Exception as e:
        logger.error(f"Video error: {e}", exc_info=True)
        _push(user_id, f"⚠️ 動画処理エラー\n{str(e)}")


def _download_video(message_id: str) -> str:
    """LINE Content API から動画をダウンロード"""
    url     = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_TOKEN}"}
    with httpx.Client(timeout=60) as client:
        res = client.get(url, headers=headers)
        res.raise_for_status()
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.write(res.content)
    tmp.close()
    logger.info(f"動画ダウンロード完了: {len(res.content)//1024}KB")
    return tmp.name


def _reel_convert(input_path: str, output_path: str):
    """ffmpeg で9:16縦型リール変換（黒帯パディング・軽量処理）
    Railwayのメモリ制限でOOM killされるのを防ぐため720x1280・スレッド制限で変換する。"""
    vf = (
        "scale=720:1280:force_original_aspect_ratio=decrease,"
        "pad=720:1280:(ow-iw)/2:(oh-ih)/2:black,"
        "eq=contrast=1.05:saturation=1.1"
    )
    cmd = [
        FFMPEG, "-y",
        "-i", input_path,
        "-vf", vf,
        "-t", "60",
        "-threads", "2",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpegタイムアウト（300秒超過）。動画が長すぎる可能性があります。")
    if result.returncode < 0:
        raise RuntimeError(
            f"ffmpegが強制終了されました（signal {-result.returncode}）。"
            "メモリ不足の可能性。短い動画で再送してください。"
        )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg失敗:\n{result.stderr[-500:]}")
    logger.info(f"reel変換完了: {output_path}")


def _extract_video_thumbnail(video_path: str, thumb_path: str):
    """ffmpeg で1秒目フレームをサムネイルとして抽出"""
    cmd = [
        FFMPEG, "-y",
        "-i", video_path,
        "-ss", "00:00:01", "-vframes", "1", "-q:v", "3",
        thumb_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError("サムネイル生成失敗")


def _push_video(user_id: str, video_url: str, thumb_url: str):
    """LINE に動画メッセージを送信"""
    url     = "https://api.line.me/v2/bot/message/push"
    token   = LINE_TOKEN.encode("ascii", errors="ignore").decode("ascii")
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "to": user_id,
        "messages": [
            {"type": "text", "text": "✅ リール変換完了！"},
            {
                "type": "video",
                "originalContentUrl": video_url,
                "previewImageUrl": thumb_url,
            },
        ],
    }
    with httpx.Client(timeout=30) as client:
        res = client.post(url, headers=headers, json=payload)
        res.raise_for_status()
    logger.info(f"LINE動画送信完了: {video_url}")


def _schedule_delete(paths: list, delay: int = 600):
    """指定秒後にファイルを削除（バックグラウンドスレッド）"""
    def _delete():
        time.sleep(delay)
        for p in paths:
            try:
                os.unlink(p)
                logger.info(f"Cleaned up: {p}")
            except Exception:
                pass
    threading.Thread(target=_delete, daemon=True).start()


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
    today = now_jst().strftime("%Y年%m月%d日 %H:%M")

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
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"LINE秘書 起動中... port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
