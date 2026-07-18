"""Agent loop — streaming version (yields SSE tokens). Active in app.py via claude_agent.py."""
import json
import logging
import asyncio
from collections import deque
from typing import AsyncGenerator

from agents.tools import TOOLS, execute_tool
from llm.bedrock import converse  # handles Anthropic SDK or Bedrock automatically

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are VoiceOps, an expert AI agent that helps engineers manage AWS infrastructure through voice and text.

You have tools to query and modify AWS resources. Use them to answer questions and carry out requests.

RULES:
1. Before calling stop_ec2_instance or modify_security_group with action=remove, output a clear warning describing exactly what will happen and ask the user to confirm. Only call the destructive tool after they say "yes", "confirm", "proceed", or equivalent.
2. Never guess resource IDs. If you need an instance ID, group ID, or cluster name you don't have, call the appropriate list tool first, then act.
3. If a request is ambiguous (e.g. "stop it" without prior context), ask which resource they mean.
4. Always include numbers: costs, counts, sizes, CPU percentages.
5. Format lists with bullet points. Keep responses concise.
6. If a tool returns an error, tell the user what failed and suggest how to fix it."""


# ─── Session management ───────────────────────────────────────────────────────

class AgentSession:
    def __init__(self, maxlen: int = 20):
        self._history: deque = deque(maxlen=maxlen)

    def add(self, role: str, content):
        self._history.append({"role": role, "content": content})

    def messages(self) -> list:
        return list(self._history)

    def replace(self, messages: list):
        self._history.clear()
        for m in messages:
            self._history.append(m)


_sessions: dict[str, AgentSession] = {}


def get_session(session_id: str) -> AgentSession:
    if session_id not in _sessions:
        _sessions[session_id] = AgentSession()
    return _sessions[session_id]


# ─── Agent loop ───────────────────────────────────────────────────────────────

async def run_agent(message: str, session_id: str) -> AsyncGenerator[str, None]:
    """
    Run the Claude agent loop, yielding SSE-ready JSON strings.
    Yields: {"token": "..."} for text, {"tool": "name"} for tool calls, {"done": true} at end.
    """
    session = get_session(session_id)
    session.add("user", message)
    messages = session.messages()

    while True:
        try:
            response = await converse(messages, SYSTEM_PROMPT, TOOLS)
        except Exception as e:
            logger.error(f"Converse error: {e}")
            yield json.dumps({"token": f"[Error: {e}]"})
            yield json.dumps({"done": True})
            return

        output_message = response["output"]["message"]
        stop_reason = response["stopReason"]

        messages.append({"role": "assistant", "content": output_message["content"]})

        if stop_reason == "end_turn":
            for block in output_message["content"]:
                if "text" in block:
                    # Stream word-by-word for real-time feel
                    words = block["text"].split(" ")
                    for i, word in enumerate(words):
                        chunk = word + (" " if i < len(words) - 1 else "")
                        yield json.dumps({"token": chunk})
                        await asyncio.sleep(0.008)
            break

        elif stop_reason == "tool_use":
            tool_results = []
            for block in output_message["content"]:
                if "toolUse" not in block:
                    continue
                tool_name = block["toolUse"]["name"]
                tool_id   = block["toolUse"]["toolUseId"]
                tool_input = block["toolUse"]["input"]

                yield json.dumps({"tool": tool_name})
                logger.info(f"Calling tool [{tool_name}] with {tool_input}")

                result = await execute_tool(tool_name, tool_input)
                logger.info(f"Tool [{tool_name}] → {json.dumps(result)[:300]}")

                tool_results.append({
                    "toolResult": {
                        "toolUseId": tool_id,
                        "content": [{"text": json.dumps(result)}],
                    }
                })

            messages.append({"role": "user", "content": tool_results})

        else:
            yield json.dumps({"token": "\n[Response truncated — max tokens reached]"})
            break

    # Persist updated history back to session
    session.replace(messages)
    yield json.dumps({"done": True})
