"""
AWSCheppu – Backend
FastAPI server: Claude agent loop via Bedrock converse, voice I/O, log streaming.
"""
import asyncio
import logging
import os
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from collections import deque

from agents.claude_agent import run_agent, stream_agent, clear_history
from voice.polly import AUDIO_DIR
from voice.transcribe import transcribe_audio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── UI log streaming ────────────────────────────────────────────────────────
_log_buffer: deque[str] = deque(maxlen=300)
_log_queues: list[asyncio.Queue] = []

class _UILogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        _log_buffer.append(msg)
        for q in list(_log_queues):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

_ui_handler = _UILogHandler()
_ui_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(_ui_handler)

app = FastAPI(title="AWSCheppu", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def index():
    """Serve the browser-based chat/voice UI."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    audio_url: str | None = None


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """SSE endpoint — streams tokens as they arrive from Bedrock."""
    logger.info(f"User: {req.message}")

    async def generate():
        async for chunk in stream_agent(req.message):
            yield chunk

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Non-streaming fallback (kept for compatibility)."""
    reply = await run_agent(req.message)
    return ChatResponse(reply=reply)


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    """Receive audio from browser, run Sarvam (or configured backend), return English text."""
    audio_bytes = await file.read()
    logger.info(
        f"Received audio: {len(audio_bytes)} bytes, "
        f"filename={file.filename!r}, content_type={file.content_type!r}"
    )
    try:
        text = await transcribe_audio(audio_bytes, file.filename or "audio.webm")
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    return {"text": text}


@app.post("/reset")
async def reset():
    """Clear conversation history."""
    clear_history()
    return {"status": "cleared"}


@app.get("/audio/{filename}")
async def get_audio(filename: str):
    path = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/debug")
async def debug():
    """Diagnose AWS connectivity — check credentials, EC2, and Bedrock."""
    import boto3
    report = {}

    key = os.getenv("AWS_ACCESS_KEY_ID", "")
    report["credentials"] = {
        "key_id_set":    bool(key),
        "key_id_prefix": key[:8] + "..." if key else "NOT SET",
        "region":        os.getenv("AWS_DEFAULT_REGION", "NOT SET"),
        "secret_set":    bool(os.getenv("AWS_SECRET_ACCESS_KEY", "")),
    }

    try:
        ec2 = boto3.client("ec2", region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
        r = ec2.describe_instances()
        count = sum(len(res["Instances"]) for res in r["Reservations"])
        report["ec2"] = {"status": "ok", "total_instances": count}
    except Exception as e:
        report["ec2"] = {"status": "error", "error": str(e)}

    try:
        br = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
        # Use converse for the health check (same API path as the agent)
        br.converse(
            modelId="us.amazon.nova-pro-v1:0",
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 5},
        )
        report["bedrock"] = {"status": "ok", "model": "claude-sonnet-4-6"}
    except Exception as e:
        report["bedrock"] = {"status": "error", "error": str(e)}

    return report


@app.get("/logs/stream")
async def stream_logs():
    """SSE — streams backend logs to the UI in real time."""
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
    _log_queues.append(q)

    async def generate():
        for line in list(_log_buffer):
            yield f"data: {line}\n\n"
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            try:
                _log_queues.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/settings/phone")
async def save_phone(req: dict):
    """Save the user's phone number for Bolna call alerts."""
    phone = req.get("phone_number", "").strip()
    import json as _json
    settings_path = os.path.join(os.path.dirname(__file__), "data", "settings.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    with open(settings_path, "w") as f:
        _json.dump({"phone_number": phone}, f)
    logger.info(f"Phone number saved: {phone}")
    return {"status": "saved", "phone_number": phone}


@app.get("/settings/phone")
async def get_phone():
    """Get the saved phone number."""
    import json as _json
    settings_path = os.path.join(os.path.dirname(__file__), "data", "settings.json")
    try:
        with open(settings_path) as f:
            data = _json.load(f)
            return {"phone_number": data.get("phone_number", "")}
    except (FileNotFoundError, _json.JSONDecodeError):
        return {"phone_number": ""}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "AWSCheppu"}
