"""
LLM client — auto-switches between Anthropic SDK (direct) and AWS Bedrock.
If ANTHROPIC_API_KEY is set in env → uses Anthropic API directly.
Otherwise → uses Bedrock boto3 (requires AWS account model access).
"""
import asyncio
import logging
import os
from functools import partial

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
BEDROCK_MODEL   = os.getenv("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0")


# ── Anthropic SDK (direct) ────────────────────────────────────────────────────

def _to_anthropic_messages(messages: list) -> list:
    """boto3 converse format → Anthropic SDK message format."""
    out = []
    for msg in messages:
        content = []
        for block in msg["content"]:
            if "text" in block:
                content.append({"type": "text", "text": block["text"]})
            elif "toolUse" in block:
                tu = block["toolUse"]
                content.append({"type": "tool_use", "id": tu["toolUseId"],
                                 "name": tu["name"], "input": tu.get("input", {})})
            elif "toolResult" in block:
                tr = block["toolResult"]
                text = next((c["text"] for c in tr.get("content", []) if "text" in c), "")
                content.append({"type": "tool_result", "tool_use_id": tr["toolUseId"],
                                 "content": text})
        out.append({"role": msg["role"], "content": content})
    return out


def _to_anthropic_tools(tools: list) -> list:
    """boto3 toolSpec format → Anthropic SDK tool format."""
    return [
        {
            "name":         t["toolSpec"]["name"],
            "description":  t["toolSpec"]["description"],
            "input_schema": t["toolSpec"]["inputSchema"]["json"],
        }
        for t in tools
    ]


def _from_anthropic_response(resp) -> dict:
    """Anthropic SDK response → boto3 converse response format (so claude_agent.py stays unchanged)."""
    content = []
    for block in resp.content:
        if block.type == "text":
            content.append({"text": block.text})
        elif block.type == "tool_use":
            content.append({"toolUse": {
                "toolUseId": block.id,
                "name":      block.name,
                "input":     block.input,
            }})
    return {
        "stopReason": resp.stop_reason,
        "output": {"message": {"role": "assistant", "content": content}},
    }


def _converse_anthropic(messages: list, system_prompt: str, tools: list) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    kwargs = {
        "model":      ANTHROPIC_MODEL,
        "max_tokens": 1024,
        "system":     system_prompt,
        "messages":   _to_anthropic_messages(messages),
    }
    if tools:
        kwargs["tools"] = _to_anthropic_tools(tools)
    resp = client.messages.create(**kwargs)
    logger.info(f"Anthropic API used {resp.usage.input_tokens}in / {resp.usage.output_tokens}out tokens")
    return _from_anthropic_response(resp)


# ── AWS Bedrock (boto3) ───────────────────────────────────────────────────────

_bedrock_client = None

def _get_bedrock_client():
    global _bedrock_client
    if _bedrock_client is None:
        import boto3
        key    = os.getenv("BEDROCK_AWS_ACCESS_KEY_ID")
        secret = os.getenv("BEDROCK_AWS_SECRET_ACCESS_KEY")
        region = os.getenv("BEDROCK_AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
        if key and secret:
            # Dedicated Bedrock account (separate from tools account)
            _bedrock_client = boto3.client(
                "bedrock-runtime",
                aws_access_key_id=key,
                aws_secret_access_key=secret,
                region_name=region,
            )
            logger.info("Bedrock client using dedicated BEDROCK_AWS_* credentials")
        else:
            _bedrock_client = boto3.client("bedrock-runtime", region_name=region)
    return _bedrock_client


def _converse_bedrock(messages: list, system_prompt: str, tools: list) -> dict:
    import botocore.exceptions
    client = _get_bedrock_client()
    kwargs = {
        "modelId":  BEDROCK_MODEL,
        "messages": messages,
        "system":   [{"text": system_prompt}],
        "inferenceConfig": {"maxTokens": 1024, "temperature": 0.1},
    }
    if tools:
        kwargs["toolConfig"] = {"tools": tools}
    try:
        return client.converse(**kwargs)
    except botocore.exceptions.ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        logger.error(f"Bedrock [{code}]: {msg}")
        raise


# ── Public async interface ────────────────────────────────────────────────────

async def converse(messages: list, system_prompt: str, tools: list = []) -> dict:
    """
    Async LLM call. Auto-selects backend:
      - ANTHROPIC_API_KEY set → Anthropic API (direct, no AWS quota)
      - Otherwise             → AWS Bedrock
    Returns a dict in boto3 converse response shape.
    """
    use_anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))
    backend = "Anthropic" if use_anthropic else "Bedrock"
    fn = _converse_anthropic if use_anthropic else _converse_bedrock
    logger.info(f"LLM backend: {backend}")

    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, partial(fn, messages, system_prompt, tools))
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        raise
