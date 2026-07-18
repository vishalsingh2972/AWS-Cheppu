"""
Amazon Polly TTS — converts agent text to MP3.
Neural voice (Ruth/Matthew) for natural-sounding speech.
"""
import os
import re
import uuid
import logging
import asyncio
import tempfile
from functools import partial

logger = logging.getLogger(__name__)

AUDIO_DIR = os.path.join(tempfile.gettempdir(), "awscheppu_audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

VOICE_ID = os.getenv("POLLY_VOICE_ID", "Ruth")


def _clean_for_speech(text: str) -> str:
    """Strip markdown formatting that sounds bad when read aloud."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)           # **bold**
    text = re.sub(r'\*(.+?)\*', r'\1', text)               # *italic*
    text = re.sub(r'`{1,3}[^`]*`{1,3}', '', text)          # `code` / ```blocks```
    text = re.sub(r'#{1,6}\s+', '', text)                   # ## headings
    text = re.sub(r'^\s*[-•*]\s+', '', text, flags=re.MULTILINE)  # bullet points
    text = re.sub(r'\n{2,}', '. ', text)                    # paragraph breaks → pause
    text = re.sub(r'\n', ' ', text)
    return text.strip()


async def synthesize_speech(text: str) -> str | None:
    """Synthesize text to MP3 via Polly. Returns /audio/<filename> URL path."""
    if not text:
        return None

    if len(text) > 600:
        text = text[:597] + "..."

    text = _clean_for_speech(text)

    loop = asyncio.get_event_loop()
    try:
        filename = await loop.run_in_executor(None, partial(_polly_call, text))
        return f"/audio/{filename}"
    except Exception as e:
        logger.warning(f"Polly TTS failed: {e}")
        return None


def _polly_call(text: str) -> str:
    import boto3
    region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    polly = boto3.client("polly", region_name=region)

    voice = os.getenv("POLLY_VOICE_ID", VOICE_ID)
    resp = polly.synthesize_speech(
        Text=text,
        OutputFormat="mp3",
        VoiceId=voice,
        Engine="neural",
    )

    filename = f"{uuid.uuid4().hex}.mp3"
    path = os.path.join(AUDIO_DIR, filename)
    with open(path, "wb") as f:
        f.write(resp["AudioStream"].read())
    return filename
