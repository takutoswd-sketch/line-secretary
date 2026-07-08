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
            background_tasks.add_task(queue_video, msg_id, user_id)

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
            if text.strip() in ("変換", "結合", "へんかん", "けつごう"):
                background_tasks.add_task(flush_pending_videos, user_id)
            else:
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



# ─────────────────────────────────────────
# 動画キュー（複数動画の結合対応）
# 動画受信 → 90秒待機。追加が来れば束ねて、来なければそのまま変換。
# テキスト「変換」「結合」で即時実行。
# ─────────────────────────────────────────

ASSETS_DIR = Path(__file__).parent / "assets"
COLLECT_WINDOW = 90  # 秒

_pending_videos: dict = {}   # user_id -> {"items": [(msg_id, path)], "timer": threading.Timer}
_pending_lock = threading.Lock()


def queue_video(message_id: str, user_id: str):
    """動画をキューに追加し、90秒の待機タイマーを(再)設定する"""
    try:
        path = _download_video(message_id)
    except Exception as e:
        logger.error(f"動画DL失敗: {e}", exc_info=True)
        _push(user_id, f"⚠️ 動画のダウンロードに失敗しました\n{str(e)}")
        return

    with _pending_lock:
        entry = _pending_videos.setdefault(user_id, {"items": [], "timer": None})
        entry["items"].append((message_id, path))
        n = len(entry["items"])
        if entry["timer"]:
            entry["timer"].cancel()
        t = threading.Timer(COLLECT_WINDOW, flush_pending_videos, args=[user_id])
        t.daemon = True
        entry["timer"] = t
        t.start()

    if n == 1:
        _push(user_id, "📥 動画を受け取りました（1本目）\n90秒以内に追加の動画を送ると1本のリールに結合します。\nすぐ変換する場合は「変換」と送ってください")
    else:
        _push(user_id, f"📥 {n}本目を受け取りました（結合予定）\n続けて送るか、「変換」で開始します")


def flush_pending_videos(user_id: str):
    """キューの動画をリール変換してLINE返信（1本ならそのまま、複数なら結合）"""
    with _pending_lock:
        entry = _pending_videos.pop(user_id, None)
        if entry and entry["timer"]:
            entry["timer"].cancel()

    if not entry or not entry["items"]:
        _push(user_id, "🤔 変換待ちの動画がありません。先に動画を送ってください")
        return

    items = entry["items"]
    paths = [p for _, p in items]
    try:
        if not RAILWAY_DOMAIN:
            _push(user_id, "⚠️ RAILWAY_PUBLIC_DOMAIN が未設定です")
            return

        label = f"{len(paths)}本を結合して" if len(paths) > 1 else ""
        _push(user_id, f"🎬 {label}リール変換中...（ロゴ・キャプション・BGM入り）\n1〜3分ほどかかります")

        ts = now_jst().strftime("%Y%m%d_%H%M%S")
        out_name   = f"reel_{ts}.mp4"
        thumb_name = f"thumb_{ts}.jpg"
        out_path   = str(VIDEO_TMP_DIR / out_name)
        thumb_path = str(VIDEO_TMP_DIR / thumb_name)

        src = paths[0] if len(paths) == 1 else _mix_segments(paths)
        _reel_convert_full(src, out_path)
        _extract_video_thumbnail(out_path, thumb_path)

        base = f"https://{RAILWAY_DOMAIN}"
        _push_video(user_id,
                    f"{base}/video/{out_name}",
                    f"{base}/video/{thumb_name}")

        for p in paths:
            try: os.unlink(p)
            except OSError: pass
        if src not in paths:
            try: os.unlink(src)
            except OSError: pass
        _schedule_delete([out_path, thumb_path], delay=600)
        logger.info(f"動画処理完了: {out_name}（{len(paths)}本）")

    except Exception as e:
        logger.error(f"Video error: {e}", exc_info=True)
        _push(user_id, f"⚠️ 動画処理エラー\n{str(e)}")


def _mix_segments(paths: list) -> str:
    """複数動画から均等に切り出して1本に結合（shokunin mix方式・16秒）"""
    n = min(len(paths), 4)
    seg = 16.0 / n
    tmp_dir = tempfile.mkdtemp(dir=str(VIDEO_TMP_DIR))
    converted = []
    for i, p in enumerate(paths[:n]):
        conv = os.path.join(tmp_dir, f"clip_{i:03d}.mp4")
        cmd = [FFMPEG, "-y", "-i", p, "-t", str(seg),
               "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,"
                      "pad=720:1280:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30",
               "-threads", "2",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
               "-c:a", "aac", "-ar", "44100", "-ac", "2",
               "-pix_fmt", "yuv420p", conv]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(f"クリップ変換失敗({i+1}本目):\n{r.stderr[-300:]}")
        converted.append(conv)

    out = os.path.join(tmp_dir, "concat.mp4")
    cmd = [FFMPEG, "-y"]
    for c in converted:
        cmd += ["-i", c]
    fc = "".join(f"[{i}:v][{i}:a]" for i in range(len(converted)))
    fc += f"concat=n={len(converted)}:v=1:a=1[vout][aout]"
    cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
            "-threads", "2",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
            "-c:a", "aac", "-pix_fmt", "yuv420p", out]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"結合失敗:\n{r.stderr[-300:]}")
    logger.info(f"結合完了: {len(converted)}本 × {seg:.1f}秒")
    return out


# ─────────────────────────────────────────
# フル版リール変換（ロゴ・2段キャプション・BGM）
# shokunin_editor.py v2.0 のreel処理をRailway向けに移植（720x1280）
# ─────────────────────────────────────────

def _find_jp_font():
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    for f in candidates:
        if os.path.exists(f):
            return f
    return None


def _load_hooks():
    """assets/hooks.json からフック・サブテキストのペアを読む"""
    try:
        with open(ASSETS_DIR / "hooks.json", encoding="utf-8") as f:
            pairs = json.load(f).get("hooks", [])
        pairs = [(p["hook"], p["sub"]) for p in pairs if p.get("hook") and p.get("sub")]
        if pairs:
            return pairs
    except Exception as e:
        logger.warning(f"hooks.json読み込み失敗: {e}")
    return [("見えない場所ほど、\n丁寧に。", "誰も見ない場所に\n技術者の思想が宿る")]


def _make_overlays(w: int, h: int, tmp_dir: str):
    """Pillowでロゴ・フック・サブのPNGオーバーレイを生成"""
    import random as _random
    import textwrap
    from PIL import Image, ImageDraw, ImageFont

    font_path = _find_jp_font()
    hook_text, sub_text = _random.choice(_load_hooks())

    # ロゴ（左上・高さ66px = 1080版の2/3スケール）
    logo_png = os.path.join(tmp_dir, "logo.png")
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    logo_src = ASSETS_DIR / "logo_white.png"
    if logo_src.exists():
        logo = Image.open(logo_src).convert("RGBA")
        lh = 66
        lw = int(logo.width * lh / logo.height)
        logo = logo.resize((lw, lh), Image.LANCZOS)
        canvas.paste(logo, (27, 27), logo)
    canvas.save(logo_png)

    def _caption(text, font_size, filename):
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(font_path, font_size)
        except Exception:
            font = ImageFont.load_default()
        # 手動改行を優先しつつ16文字で折り返し
        lines = []
        for seg in text.split("\n"):
            lines.extend(textwrap.wrap(seg, width=16) or [seg])
        line_h = font_size + 6
        total_h = len(lines) * line_h
        ty = int(h * 0.40) - total_h // 2
        for i, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
            tx = (w - tw) // 2
            cur_y = ty + i * line_h
            pad = 10
            draw.rectangle([tx - pad, cur_y - pad // 2,
                            tx + tw + pad, cur_y + line_h - pad // 2 + pad],
                           fill=(0, 0, 0, 110))
            draw.text((tx + 2, cur_y + 2), line, font=font, fill=(0, 0, 0, 150))
            draw.text((tx, cur_y), line, font=font, fill=(255, 255, 255, 240))
        path = os.path.join(tmp_dir, filename)
        img.save(path)
        return path

    hook_png = _caption(hook_text, 37, "hook.png")
    sub_png  = _caption(sub_text, 27, "sub.png")
    logger.info(f"フック: {hook_text.replace(chr(10), ' ')} / サブ: {sub_text.replace(chr(10), ' ')}")
    return logo_png, hook_png, sub_png


def _reel_convert_full(input_path: str, output_path: str):
    """フル版reel変換: 9:16化 → カラーグレード → ロゴ常時 → フック(0-3s) → サブ(3-7s) → BGM"""
    w, h = 720, 1280
    tmp_dir = tempfile.mkdtemp(dir=str(VIDEO_TMP_DIR))
    try:
        logo_png, hook_png, sub_png = _make_overlays(w, h, tmp_dir)
        bgm = ASSETS_DIR / "motohiko_bgm_reel_30s.mp3"
        has_bgm = bgm.exists()

        grade = ("eq=contrast=1.08:brightness=0.02:saturation=1.12,"
                 "colorchannelmixer=rr=1.05:gg=1.0:bb=0.95,"
                 "vignette=PI/5")

        cmd = [FFMPEG, "-y", "-i", input_path,
               "-i", logo_png, "-i", hook_png, "-i", sub_png]
        if has_bgm:
            cmd += ["-stream_loop", "-1", "-i", str(bgm)]

        fc = (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,{grade}[graded];"
            f"[graded][1:v]overlay=0:0[logo];"
            f"[logo][2:v]overlay=0:0:enable='between(t,0,3)'[hook];"
            f"[hook][3:v]overlay=0:0:enable='between(t,3,7)'[vout]"
        )
        if has_bgm:
            fc += ";[4:a]volume=0.35[aout]"
            cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "[aout]", "-shortest"]
        else:
            cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "0:a?"]

        cmd += ["-t", "16", "-threads", "2",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                output_path]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise RuntimeError("ffmpegタイムアウト（300秒超過）")
        if result.returncode < 0:
            raise RuntimeError(f"ffmpegが強制終了されました（signal {-result.returncode}）。メモリ不足の可能性")
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg失敗:\n{result.stderr[-500:]}")
        logger.info(f"フル版reel変換完了: {output_path}")
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
