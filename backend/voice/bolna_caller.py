"""
Bolna AI Phone Call Integration — calls the user's phone with AWS alerts.
Uses Bolna's POST /call API to trigger an outbound call through a configured agent.

Mock mode: When BOLNA_API_KEY is not set, logs the call instead (for dev/testing).
"""
import os
import logging
import json

logger = logging.getLogger(__name__)

BOLNA_API_KEY   = os.getenv("BOLNA_API_KEY", "")
BOLNA_AGENT_ID  = os.getenv("BOLNA_AGENT_ID", "")
BOLNA_BASE_URL  = os.getenv("BOLNA_BASE_URL", "https://api.bolna.ai")

# Allow the phone number to be set via env or passed at call time
DEFAULT_PHONE = os.getenv("BOLNA_PHONE_NUMBER", "")


async def trigger_call(phone_number: str, message: str) -> dict:
    """
    Trigger a Bolna phone call to deliver a message.
    
    Args:
        phone_number: E.164 format (e.g. +919876543210)
        message:      The text to be spoken by the Bolna agent
    
    Returns:
        dict with {success: bool, detail: str, execution_id?: str}
    """
    if not phone_number:
        # Fall back to env default
        phone_number = DEFAULT_PHONE
    
    if not phone_number:
        logger.warning("No phone number provided for Bolna call — skipping")
        return {"success": False, "detail": "No phone number configured"}
    
    # ── Mock mode (no API key set) ──────────────────────────────────
    if not BOLNA_API_KEY or not BOLNA_AGENT_ID:
        logger.info(
            f"[BOLNA MOCK] Would call {phone_number} with message:\n"
            f"  {message[:300]}"
        )
        return {
            "success": True,
            "detail": f"[MOCK] Simulated call to {phone_number}. "
                      f"Set BOLNA_API_KEY and BOLNA_AGENT_ID for real calls.",
            "mock": True,
        }
    
    # ── Real mode — call Bolna API ──────────────────────────────────
    import httpx
    
    payload = {
        "agent_id": BOLNA_AGENT_ID,
        "recipient_phone_number": phone_number,
        "agent_config": {
            "prompt": (
                "You are AWS Cheppu — an AWS infrastructure alert system. "
                "Speak clearly and concisely. Read the following message "
                "to the user exactly as provided, then ask if they need "
                "any further assistance.\n\n"
                f"Message: {message}"
            ),
        },
    }
    
    headers = {
        "Authorization": f"Bearer {BOLNA_API_KEY}",
        "Content-Type": "application/json",
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{BOLNA_BASE_URL}/call",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"Bolna call triggered: {json.dumps(data, indent=2)[:200]}")
            return {
                "success": True,
                "detail": f"Call initiated to {phone_number}",
                "execution_id": data.get("execution_id", ""),
                "mock": False,
            }
    except httpx.HTTPStatusError as e:
        logger.error(f"Bolna API HTTP error: {e.response.status_code} {e.response.text[:200]}")
        return {"success": False, "detail": f"Bolna API error: {e.response.status_code}"}
    except httpx.RequestError as e:
        logger.error(f"Bolna API connection error: {e}")
        return {"success": False, "detail": f"Cannot reach Bolna: {e}"}
    except Exception as e:
        logger.error(f"Bolna call failed: {e}")
        return {"success": False, "detail": str(e)[:200]}