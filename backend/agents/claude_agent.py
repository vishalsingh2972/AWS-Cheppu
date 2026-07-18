"""
Claude agent loop — the decision-making brain of AWSCheppu.

Two entry points:
  run_agent()    — blocking, returns full text (kept for backwards compat)
  stream_agent() — async generator, yields SSE strings as tokens arrive
"""
import asyncio
import json
import logging
import os
from collections import deque

from agents.tools import TOOLS, execute_tool
from llm.bedrock import converse, _get_bedrock_client, BEDROCK_MODEL

logger = logging.getLogger(__name__)

_history: deque[dict] = deque(maxlen=20)

SYSTEM_PROMPT = """You are AWSCheppu — an AWS infrastructure assistant that executes voice commands.

RESPONSE RULES (critical — responses are spoken aloud via text-to-speech):
- Keep every response to 1-3 sentences. Short and direct.
- Never mention tool names, API calls, or internal steps.
- Speak instance IDs and resource names naturally: "instance Titaaaaa" not just "i-0abc".
- Use plain English: "2 security groups" not JSON or formatted tables.

ACTION RULES:
- For READ operations (list, describe, cost, security_audit): call the tool immediately, no need to ask.
- For WRITE operations (stop instance, modify security group, create_vpc, delete_vpc,
  create_security_group, create_ecr_repository): say what you will do and ask the user to confirm.
  Example: "I'll stop instance Titaaaaa. Say yes to confirm."
- delete_vpc is IRREVERSIBLE and takes down its subnet, internet gateway, and route table —
  warn the user clearly (same as terminate_ec2_instance and delete_iam_user) before calling it.
- If the user says yes, confirm, go ahead, or do it — execute the pending write action.
- If a tool returns an error, say what went wrong in plain terms.

SCOPE:
- Only answer AWS infrastructure questions. Politely decline anything unrelated.
- You have access to EC2, EBS, Security Groups, VPC networking, Cost, ECR, EKS, S3, RDS, IAM,
  and a built-in security audit scanner (security_audit). Proactively suggest running a security
  audit when the user asks about account security, safety, or "what's risky."
"""


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


_STOP = object()  # sentinel — StopIteration cannot cross an asyncio Future boundary

def _try_next(it):
    """Wraps next() so StopIteration never escapes to the event loop."""
    try:
        return next(it)
    except StopIteration:
        return _STOP

async def _next_event(it):
    """Pull one item from a synchronous iterator without blocking the event loop."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _try_next, it)
    return None if result is _STOP else result


async def stream_agent(user_message: str):
    """
    Async generator — yields SSE strings as the model produces them.

    Event types:
      {"type":"tool",  "name":"list_ec2_instances"}   — tool call starting
      {"type":"token", "text":"You have 3 ..."}        — text token
      {"type":"audio", "url":"/audio/abc.mp3"}         — Polly audio ready
      {"type":"done"}                                  — stream finished
      {"type":"error", "text":"..."}                   — fatal error
    """
    from voice.polly import synthesize_speech

    _history.append({"role": "user", "content": [{"text": user_message}]})
    messages = list(_history)

    use_anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))
    full_text = ""

    for _ in range(8):
        try:
            if use_anthropic:
                # Anthropic SDK: non-streaming call, simulate token stream
                response = await converse(messages, SYSTEM_PROMPT, TOOLS)
                stop_reason = response.get("stopReason", "")
                content = response.get("output", {}).get("message", {}).get("content", [])

                if stop_reason == "tool_use":
                    messages.append({"role": "assistant", "content": content})
                    tool_results = []
                    for block in content:
                        if "toolUse" not in block:
                            continue
                        tu = block["toolUse"]
                        yield _sse({"type": "tool", "name": tu["name"]})
                        result = await execute_tool(tu["name"], tu.get("input", {}))
                        tool_results.append({"toolResult": {
                            "toolUseId": tu["toolUseId"],
                            "content": [{"text": json.dumps(result)}],
                        }})
                    messages.append({"role": "user", "content": tool_results})
                    continue

                full_text = " ".join(b["text"] for b in content if "text" in b).strip() or "Done."
                for word in full_text.split():
                    yield _sse({"type": "token", "text": word + " "})
                    await asyncio.sleep(0.012)
                _history.append({"role": "assistant", "content": content})
                break

            else:
                # Bedrock: real token streaming via converse_stream
                bedrock = _get_bedrock_client()
                kwargs = {
                    "modelId": BEDROCK_MODEL,
                    "messages": messages,
                    "system": [{"text": SYSTEM_PROMPT}],
                    "inferenceConfig": {"maxTokens": 1024, "temperature": 0.1},
                }
                if TOOLS:
                    kwargs["toolConfig"] = {"tools": TOOLS}

                loop = asyncio.get_event_loop()
                raw = await loop.run_in_executor(None, lambda: bedrock.converse_stream(**kwargs))
                it = iter(raw["stream"])

                text_parts, tool_calls = [], []
                current_tool, current_text = None, []
                stop_reason = "end_turn"
                assistant_content = []

                while True:
                    event = await _next_event(it)
                    if event is None:
                        break

                    if "contentBlockStart" in event:
                        start = event["contentBlockStart"].get("start", {})
                        if "toolUse" in start:
                            current_tool = {
                                "toolUseId": start["toolUse"]["toolUseId"],
                                "name":      start["toolUse"]["name"],
                                "input_json": "",
                            }
                            yield _sse({"type": "tool", "name": current_tool["name"]})

                    elif "contentBlockDelta" in event:
                        delta = event["contentBlockDelta"]["delta"]
                        if "text" in delta:
                            tok = delta["text"]
                            current_text.append(tok)
                            yield _sse({"type": "token", "text": tok})
                        elif "toolUse" in delta and current_tool:
                            current_tool["input_json"] += delta["toolUse"].get("input", "")

                    elif "contentBlockStop" in event:
                        if current_text:
                            joined = "".join(current_text)
                            text_parts.append(joined)
                            assistant_content.append({"text": joined})
                            current_text = []
                        if current_tool:
                            try:
                                current_tool["input"] = json.loads(current_tool["input_json"] or "{}")
                            except Exception:
                                current_tool["input"] = {}
                            tool_calls.append(current_tool)
                            assistant_content.append({"toolUse": {
                                "toolUseId": current_tool["toolUseId"],
                                "name":      current_tool["name"],
                                "input":     current_tool["input"],
                            }})
                            current_tool = None

                    elif "messageStop" in event:
                        stop_reason = event["messageStop"]["stopReason"]

                messages.append({"role": "assistant", "content": assistant_content})

                if stop_reason == "end_turn":
                    full_text = "".join(text_parts).strip() or "Done."
                    _history.append({"role": "assistant", "content": assistant_content})
                    break

                elif stop_reason == "tool_use":
                    tool_results = []
                    for tc in tool_calls:
                        result = await execute_tool(tc["name"], tc.get("input", {}))
                        logger.info(f"Tool {tc['name']} → {str(result)[:200]}")
                        tool_results.append({"toolResult": {
                            "toolUseId": tc["toolUseId"],
                            "content":   [{"text": json.dumps(result)}],
                        }})
                    messages.append({"role": "user", "content": tool_results})

        except Exception as e:
            logger.error(f"stream_agent error: {e}")
            yield _sse({"type": "error", "text": str(e)[:160]})
            try:
                _history.pop()
            except Exception:
                pass
            yield _sse({"type": "done"})
            return

    # Polly TTS — runs after text is fully streamed
    if full_text and os.getenv("TTS_ENABLED", "false").lower() == "true":
        try:
            audio_url = await synthesize_speech(full_text)
            if audio_url:
                yield _sse({"type": "audio", "url": audio_url})
        except Exception as e:
            logger.warning(f"TTS failed (non-fatal): {e}")

    yield _sse({"type": "done"})


async def run_agent(user_message: str) -> str:
    """Blocking wrapper around stream_agent — collects full text."""
    text = ""
    async for chunk in stream_agent(user_message):
        try:
            evt = json.loads(chunk.removeprefix("data: ").strip())
            if evt.get("type") == "token":
                text += evt.get("text", "")
        except Exception:
            pass
    return text.strip() or "Done."


def clear_history() -> None:
    _history.clear()
