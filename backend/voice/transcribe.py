"""
Transcription module – converts audio to text using Amazon Transcribe or OpenAI Whisper.
Falls back to a mock response in dev mode.

Returns (transcript, language_code) where language_code is BCP-47 (e.g. "ta-IN", "en-IN").
The transcript text is always the original spoken language — Claude handles understanding it.
"""
import os
import uuid
import logging
import asyncio
import tempfile
from functools import partial

logger = logging.getLogger(__name__)

TRANSCRIPTION_BACKEND = os.getenv("TRANSCRIPTION_BACKEND", "transcribe")  # "transcribe" | "whisper" | "gemini" | "mock"

# Whisper returns lowercase English language names; map to BCP-47
_WHISPER_TO_BCP47: dict[str, str] = {
    "tamil": "ta-IN", "hindi": "hi-IN", "telugu": "te-IN",
    "kannada": "kn-IN", "malayalam": "ml-IN", "marathi": "mr-IN",
    "gujarati": "gu-IN", "bengali": "bn-IN", "english": "en-IN",
    "odia": "od-IN", "punjabi": "pa-IN",
}


async def transcribe_audio(audio_bytes: bytes, filename: str = "audio.webm") -> tuple[str, str]:
    """Returns (transcript_text, bcp47_language_code)."""
    if TRANSCRIPTION_BACKEND == "mock":
        return ("Show idle EC2 instances", "en-IN")

    if TRANSCRIPTION_BACKEND == "whisper":
        return await _transcribe_whisper(audio_bytes, filename)

    if TRANSCRIPTION_BACKEND == "gemini":
        return await _transcribe_gemini(audio_bytes, filename)

    if TRANSCRIPTION_BACKEND == "sarvam":
        return await _transcribe_sarvam(audio_bytes, filename)

    return await _transcribe_aws(audio_bytes, filename)


async def _transcribe_aws(audio_bytes: bytes, filename: str) -> str:
    """Use Amazon Transcribe streaming or S3-based transcription."""
    import boto3

    # Write audio to temp file
    suffix = os.path.splitext(filename)[-1] or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name

    try:
        # Upload to S3
        s3 = boto3.client("s3")
        bucket = os.environ["VOICEOPS_AUDIO_BUCKET"]
        key = f"transcribe-input/{uuid.uuid4()}{suffix}"
        s3.upload_file(tmp_path, bucket, key)

        transcribe = boto3.client("transcribe")
        job_name = f"awscheppu-{uuid.uuid4().hex[:8]}"
        media_uri = f"s3://{bucket}/{key}"
        media_format = suffix.lstrip(".")

        loop = asyncio.get_event_loop()

        def start_and_poll():
            transcribe.start_transcription_job(
                TranscriptionJobName=job_name,
                Media={"MediaFileUri": media_uri},
                MediaFormat=media_format,
                LanguageCode="en-US",
            )
            import time
            while True:
                resp = transcribe.get_transcription_job(TranscriptionJobName=job_name)
                status = resp["TranscriptionJob"]["TranscriptionJobStatus"]
                if status == "COMPLETED":
                    uri = resp["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
                    import urllib.request, json as _json
                    with urllib.request.urlopen(uri) as r:
                        data = _json.load(r)
                    return data["results"]["transcripts"][0]["transcript"]
                elif status == "FAILED":
                    raise RuntimeError("Transcription job failed")
                time.sleep(2)

        return await loop.run_in_executor(None, start_and_poll)

    finally:
        os.unlink(tmp_path)
        # Clean up S3 object (best effort)
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception:
            pass


async def _transcribe_gemini(audio_bytes: bytes, filename: str) -> str:
    """Use Gemini Flash to transcribe audio — supports webm natively, no S3 needed."""
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set — add it to .env")

    suffix = os.path.splitext(filename)[-1].lower()
    mime_map = {".webm": "audio/webm", ".mp3": "audio/mpeg", ".wav": "audio/wav",
                ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".flac": "audio/flac"}
    mime_type = mime_map.get(suffix, "audio/webm")

    client = genai.Client(api_key=api_key)

    response = await asyncio.to_thread(
        client.models.generate_content,
        model="gemini-2.0-flash",
        contents=types.Content(parts=[
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
            types.Part.from_text(
                "Transcribe this audio exactly as spoken. "
                "Return only the transcribed text, no commentary or punctuation fixes."
            ),
        ]),
    )
    return response.text.strip()


async def _transcribe_whisper(audio_bytes: bytes, filename: str) -> str:
    """Use OpenAI Whisper API."""
    import openai

    client = openai.AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    suffix = os.path.splitext(filename)[-1] or ".webm"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name

    try:
        with open(tmp_path, "rb") as f:
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
            )
        return transcript
    finally:
        os.unlink(tmp_path)


async def _transcribe_sarvam(audio_bytes: bytes, filename: str) -> str:
    """Use Sarvam AI speech-to-text-translate — accepts Hindi/Indian languages, returns English."""
    import httpx

    api_key = os.environ.get("SARVAM_API_KEY", "")
    if not api_key:
        raise ValueError("SARVAM_API_KEY not set — add it to .env")

    suffix = os.path.splitext(filename)[-1].lower() or ".webm"
    mime_map = {
        ".webm": "audio/webm", ".mp3": "audio/mpeg", ".wav": "audio/wav",
        ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".flac": "audio/flac",
    }
    mime_type = mime_map.get(suffix, "audio/webm")

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.sarvam.ai/speech-to-text-translate",
            headers={"api-subscription-key": api_key},
            files={"file": (filename, audio_bytes, mime_type)},
        )
        response.raise_for_status()
        data = response.json()

    transcript = data.get("transcript", "").strip()
    if not transcript:
        raise RuntimeError(f"Sarvam returned empty transcript: {data}")
    logger.info("Sarvam transcript: %s", transcript)
    return transcript
