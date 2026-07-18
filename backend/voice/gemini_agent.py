"""
Gemini Live Agent — voice command processing with AWS function calling.
Audio bytes in → Gemini understands speech + calls tools → text response out.
"""
import os
import logging
from typing import Any

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-native-audio-dialog")

SYSTEM_PROMPT = (
    "You are AWSCheppu, an AWS cloud infrastructure assistant. "
    "When the user asks about their AWS resources, call the matching tool immediately — "
    "do not ask clarifying questions for simple list/cost/status commands. "
    "Keep responses concise and conversational; they will be spoken aloud. "
    "For list results, summarise: say 'You have 3 running instances' before listing IDs. "
    "If a tool returns an error, report it plainly without apologising."
)

# All 7 AWS operations the agent can call
_TOOL_DECLARATIONS = [
    {
        "name": "list_ec2_instances",
        "description": "List all EC2 instances with current state and CPU utilisation.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_ebs_volumes",
        "description": "List all EBS volumes with size, type, and attachment status.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_security_groups",
        "description": "List all security groups and their inbound/outbound rules.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_cost_report",
        "description": "Get AWS cost broken down by service for the current month.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "stop_ec2_instance",
        "description": "Stop a running EC2 instance.",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "EC2 instance ID, e.g. i-0abc123def456"}
            },
            "required": ["instance_id"],
        },
    },
    {
        "name": "start_ec2_instance",
        "description": "Start a stopped EC2 instance.",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "EC2 instance ID, e.g. i-0abc123def456"}
            },
            "required": ["instance_id"],
        },
    },
    {
        "name": "disable_port",
        "description": "Remove an inbound rule to block a port on all or a specific security group.",
        "parameters": {
            "type": "object",
            "properties": {
                "port":     {"type": "integer", "description": "Port number, e.g. 80"},
                "group_id": {"type": "string",  "description": "Security group ID (optional)"},
            },
            "required": ["port"],
        },
    },
]

# Maps Gemini tool name → (AWSAgent intent, params builder)
_INTENT_MAP = {
    "list_ec2_instances":   lambda a: ("LIST_EC2",             {}),
    "list_ebs_volumes":     lambda a: ("LIST_EBS",             {}),
    "list_security_groups": lambda a: ("LIST_SECURITY_GROUPS", {}),
    "get_cost_report":      lambda a: ("COST_REPORT",          {}),
    "stop_ec2_instance":    lambda a: ("STOP_EC2",   {"instance_id": a.get("instance_id", "")}),
    "start_ec2_instance":   lambda a: ("START_EC2",  {"instance_id": a.get("instance_id", "")}),
    "disable_port":         lambda a: ("DISABLE_PORT", {"port": a.get("port"), "group_id": a.get("group_id")}),
}


async def _run_tool(name: str, args: dict, aws_agent: Any) -> str:
    if name not in _INTENT_MAP:
        return f"Unknown tool: {name}"
    intent, params = _INTENT_MAP[name](args)
    result = await aws_agent.execute(intent, params)
    return result.get("display", str(result))


async def process_voice_command(audio_bytes: bytes, mime_type: str, aws_agent: Any) -> str:
    """
    Send audio to Gemini Live, handle AWS tool calls, return final text.
    Falls back to regular generateContent if Live API is unavailable for this model.
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set in environment")

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise ImportError("Run: pip install google-genai")

    client = genai.Client(api_key=GEMINI_API_KEY)

    # --- Try Live API first (for gemini-*-live-* models) ---
    if "live" in GEMINI_MODEL.lower() or "dialog" in GEMINI_MODEL.lower():
        try:
            return await _live_call(client, types, audio_bytes, mime_type, aws_agent)
        except Exception as e:
            logger.warning(f"Live API failed ({e}), falling back to generateContent")

    # --- Fallback: regular generateContent with audio + function calling ---
    return await _generate_call(client, types, audio_bytes, mime_type, aws_agent)


async def _live_call(client, types, audio_bytes, mime_type, aws_agent) -> str:
    """BidiGenerateContent WebSocket path for Live models."""
    config = types.LiveConnectConfig(
        response_modalities=["TEXT"],
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=[
            types.FunctionDeclaration(**t) for t in _TOOL_DECLARATIONS
        ])],
    )

    response_parts = []

    async with client.aio.live.connect(model=GEMINI_MODEL, config=config) as session:
        await session.send(
            input=types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
            end_of_turn=True,
        )

        async for msg in session.receive():
            # Collect text
            if getattr(msg, "text", None):
                response_parts.append(msg.text)

            # Handle tool calls
            if getattr(msg, "tool_call", None):
                fn_responses = []
                for fc in msg.tool_call.function_calls:
                    logger.info(f"Gemini tool call: {fc.name}({fc.args})")
                    result = await _run_tool(fc.name, dict(fc.args or {}), aws_agent)
                    fn_responses.append(types.FunctionResponse(
                        name=fc.name,
                        id=fc.id,
                        response={"result": result},
                    ))
                await session.send(
                    input=types.LiveClientToolResponse(function_responses=fn_responses)
                )

            # Stop on turn complete
            if getattr(getattr(msg, "server_content", None), "turn_complete", False):
                break

    return " ".join(response_parts).strip() or "Command processed."


async def _generate_call(client, types, audio_bytes, mime_type, aws_agent) -> str:
    """Regular generateContent path — works with non-Live Gemini models."""
    import base64, asyncio

    tools = types.Tool(function_declarations=[
        types.FunctionDeclaration(**t) for t in _TOOL_DECLARATIONS
    ])

    model_name = GEMINI_MODEL
    if "live" in model_name.lower() or "dialog" in model_name.lower():
        model_name = "gemini-2.0-flash"  # safe fallback

    contents = [
        types.Content(role="user", parts=[
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
        ])
    ]

    # Agentic loop: up to 5 rounds of tool calls
    for _ in range(5):
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=[tools],
            ),
        )

        candidate = response.candidates[0]
        parts = candidate.content.parts

        # Check for function calls
        fn_calls = [p for p in parts if p.function_call]
        if not fn_calls:
            # No more tool calls — collect text
            return " ".join(p.text for p in parts if getattr(p, "text", None)).strip()

        # Append model's function-call turn
        contents.append(types.Content(role="model", parts=parts))

        # Execute each function call and build tool response
        fn_response_parts = []
        for p in fn_calls:
            fc = p.function_call
            logger.info(f"Gemini tool call: {fc.name}({fc.args})")
            result = await _run_tool(fc.name, dict(fc.args or {}), aws_agent)
            fn_response_parts.append(types.Part.from_function_response(
                name=fc.name,
                response={"result": result},
            ))
        contents.append(types.Content(role="user", parts=fn_response_parts))

    return "Could not complete the request."
