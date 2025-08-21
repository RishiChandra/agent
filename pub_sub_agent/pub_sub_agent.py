import json
import os
import random
import traceback
import base64
from google import genai
from google.genai import types
from google.genai.types import (
    LiveConnectConfig,
    SpeechConfig,
    VoiceConfig,
    PrebuiltVoiceConfig,
    FunctionDeclaration,
    Tool,
)

# ===== Config =====
MODEL = "gemini-2.0-flash-live-001"
client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

# ---- Mock tools ----
def get_order_status(order_id):
    statuses = ["processing", "shipped", "delivered"]
    shipment_methods = ["standard", "express", "next day", "international"]
    seed = sum(ord(c) for c in str(order_id))
    random.seed(seed)
    status = random.choice(statuses)
    shipment = random.choice(shipment_methods)
    order_date = "2024-05-" + str(random.randint(12, 28)).zfill(2)
    return {"order_id": order_id, "status": status, "shipment_method": shipment, "order_date": order_date}

def get_memories(user_id=None):
    return {"user_id": user_id or "default_user", "loyalty_status": "silver"}

# ---- Tools ----
memories_tool = Tool(function_declarations=[FunctionDeclaration(
    name="get_memories",
    description="Get user memories",
    parameters={"type": "OBJECT", "properties": {"user_id": {"type": "STRING"}}}
)])
order_status_tool = Tool(function_declarations=[FunctionDeclaration(
    name="get_order_status",
    description="Get order status",
    parameters={"type": "OBJECT","properties":{"order_id":{"type":"STRING"}},"required":["order_id"]}
)])

CONFIG = LiveConnectConfig(
    response_modalities=["AUDIO"],
    input_audio_transcription={},
    output_audio_transcription={},
    tools=[memories_tool, order_status_tool],
    speech_config=SpeechConfig(
        voice_config=VoiceConfig(prebuilt_voice_config=PrebuiltVoiceConfig(voice_name="Puck"))
    ),
    system_instruction="You are a helpful customer service assistant."
)

# === This is the function you can import in function_app.py ===
async def handle_audio_message(message: dict, actions):
    """
    message: dict from Web PubSub with {"audio": "<base64 PCM chunk>", "user_id": "..."}
    actions: Azure Functions WebPubSub out binding to send response back
    """
    try:
        user_id = message.get("user_id", "default_user")
        audio_chunk = message.get("audio")
        if not audio_chunk:
            await actions.set(json.dumps({"error": "No audio field"}))
            return

        audio_bytes = base64.b64decode(audio_chunk)

        async with client.aio.live.connect(model=MODEL, config=CONFIG) as session:
            # Always start with get_memories
            mem = get_memories(user_id)
            await session.send_tool_response(
                function_responses={"name": "get_memories", "response": mem, "id": "init"}
            )

            # Feed user audio
            await session.send_realtime_input(
                media={"data": audio_bytes, "mime_type": "audio/pcm;rate=16000"}
            )

            async for response in session.receive():
                if response.tool_call:
                    for fn in response.tool_call.function_calls:
                        if fn.name == "get_order_status":
                            res = get_order_status(fn.args["order_id"])
                            await session.send_tool_response(
                                function_responses={"name": fn.name, "response": res, "id": fn.id}
                            )

                if response.server_content and response.server_content.model_turn:
                    for part in response.server_content.model_turn.parts:
                        if part.inline_data:
                            # Send Gemini audio response back to ESP32 (base64)
                            await actions.set(json.dumps({
                                "audio": base64.b64encode(part.inline_data.data).decode("utf-8")
                            }))
    except Exception as e:
        traceback.print_exc()
        await actions.set(json.dumps({"error": str(e)}))
