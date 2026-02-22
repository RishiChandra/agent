import json
import os
from types import SimpleNamespace

from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load environment variables
load_dotenv()

# Gemini Configuration
# Use GEMINI_API_KEY in .env (or GOOGLE_API_KEY as fallback for compatibility with gemini_config)
model = "gemini-3-flash-preview"
api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _messages_to_contents(messages):
    """Convert OpenAI-style messages to Gemini contents and optional system_instruction."""
    contents = []
    system_instruction = None
    for msg in messages:
        role = (msg.get("role") or "").lower()
        content = msg.get("content") or ""
        if role == "system":
            system_instruction = content
        elif role == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=content)]))
        elif role == "assistant":
            contents.append(types.Content(role="model", parts=[types.Part(text=content)]))
    return contents, system_instruction


def _openai_tools_to_gemini(tools):
    """Convert OpenAI-style tool definitions to Gemini Tool list."""
    if not tools:
        return None
    gemini_tools = []
    for t in tools:
        if t.get("type") != "function" or "function" not in t:
            continue
        fn = t["function"]
        params = fn.get("parameters") or {}
        gemini_tools.append(
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=fn.get("name", "function"),
                        description=fn.get("description") or "",
                        parameters=params,
                    )
                ]
            )
        )
    return gemini_tools if gemini_tools else None


def call_gemini(messages, tools=None):
    """
    Call Gemini 3 Flash with the given messages and optional tools.

    Args:
        messages: List of dicts with "role" ("system"|"user"|"assistant") and "content".
        tools: Optional list of OpenAI-style tool definitions (type/function/parameters).

    Returns:
        GenerateContentResponse from the Gemini API.
    """
    client = get_gemini_client()
    model_name = get_model_name()
    contents, system_instruction = _messages_to_contents(messages)
    # Gemini API requires at least one content (user/model turn). If we only had a system
    # message, contents is empty — add a single user turn so the API accepts the request.
    if not contents:
        contents = [types.Content(role="user", parts=[types.Part(text="Select the appropriate tool(s) for the request above.")])]
    config_kw = {}
    if system_instruction:
        config_kw["system_instruction"] = system_instruction
    gemini_tools = _openai_tools_to_gemini(tools)
    if gemini_tools:
        config_kw["tools"] = gemini_tools
        config_kw["tool_config"] = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY"))

    config = types.GenerateContentConfig(**config_kw) if config_kw else None
    if config:
        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config,
        )
    else:
        response = client.models.generate_content(
            model=model_name,
            contents=contents,
        )
    return response


def gemini_response_to_openai_like(response):
    """
    Convert a Gemini GenerateContentResponse to an OpenAI-like object so that
    response.choices[0].message.content and response.choices[0].message.tool_calls
    work like OpenAI's ChatCompletion response.
    """
    text_parts = []
    tool_calls = []
    if getattr(response, "candidates", None) and len(response.candidates) > 0:
        content = getattr(response.candidates[0], "content", None)
        if content and getattr(content, "parts", None):
            for part in content.parts:
                if getattr(part, "text", None) and part.text:
                    text_parts.append(part.text)
                if getattr(part, "function_call", None) and part.function_call:
                    fc = part.function_call
                    name = getattr(fc, "name", None) or ""
                    args = getattr(fc, "args", None) or {}
                    if not isinstance(args, str):
                        args = json.dumps(args) if args else "{}"
                    tool_calls.append(
                        SimpleNamespace(
                            id=getattr(fc, "id", None) or f"call_{len(tool_calls)}",
                            function=SimpleNamespace(name=name, arguments=args),
                        )
                    )
    message = SimpleNamespace(
        content="\n".join(text_parts) if text_parts else "",
        tool_calls=tool_calls if tool_calls else None,
    )
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def get_gemini_client():
    """
    Get a configured Gemini client instance.

    Returns:
        genai.Client: Configured client instance.

    Raises:
        ValueError: If GEMINI_API_KEY or GOOGLE_API_KEY is not set.
    """
    if not api_key:
        raise ValueError(
            "Missing GEMINI_API_KEY or GOOGLE_API_KEY environment variable. "
            "Set one of them in .env to use the Gemini API."
        )
    return genai.Client(api_key=api_key)


def get_model_name():
    return model
