import os
import json
import base64
import asyncio
import traceback
import random
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from dotenv import load_dotenv
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

# python -m uvicorn app.main:app --host 0.0.0.0 --port \$PORT

# https://ai.google.dev/gemini-api/docs/live-guide
load_dotenv()

from fastapi import FastAPI
app = FastAPI()

@app.get("/healthz")
def healthz():
    print("/healthz called and accepted")
    return {"ok": True}


# ===== Gemini config =====
PROJECT_ID = "ai-pin-465902"
LOCATION = "us-central1"
MODEL = "gemini-2.0-flash-live-001"
client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

# ===== Audio Config =====
FORMAT = "pcm"
RECEIVE_SAMPLE_RATE = 24000
SEND_SAMPLE_RATE = 16000
CHUNK_SIZE = 512
CHANNELS = 1

# ---- Mock tools (identical to working example) ----
def get_order_status(order_id):
    """Mock order status API that returns randomized status for an order ID."""
    # Define possible order statuses and shipment methods
    statuses = ["processing", "shipped", "delivered"]
    shipment_methods = ["standard", "express", "next day", "international"]

    # Generate random data based on the order ID to ensure consistency for the same ID
    # Using the sum of ASCII values of the order ID as a seed
    seed = sum(ord(c) for c in str(order_id))
    random.seed(seed)

    # Generate order data
    status = random.choice(statuses)
    shipment = random.choice(shipment_methods)

    # Generate dates based on status
    order_date = "2024-05-" + str(random.randint(12, 28)).zfill(2)

    estimated_delivery = None
    shipped_date = None
    delivered_date = None

    if status == "processing":
        estimated_delivery = "2024-06-" + str(random.randint(1, 15)).zfill(2)
    elif status == "shipped":
        shipped_date = "2024-05-" + str(random.randint(1, 28)).zfill(2)
        estimated_delivery = "2024-06-" + str(random.randint(1, 15)).zfill(2)
    elif status == "delivered":
        shipped_date = "2024-05-" + str(random.randint(1, 20)).zfill(2)
        delivered_date = "2024-05-" + str(random.randint(21, 28)).zfill(2)

    # Reset random seed to ensure other functions aren't affected
    random.seed()

    result = {
        "order_id": order_id,
        "status": status,
        "order_date": order_date,
        "shipment_method": shipment,
        "estimated_delivery": estimated_delivery,
    }

    if shipped_date:
        result["shipped_date"] = shipped_date

    if delivered_date:
        result["delivered_date"] = delivered_date

    print(f"Order status for {order_id}: {status}")

    return result

def get_memories(user_id=None):
    """Mock memories API that returns user context and conversation history."""
    # Mock user memories and context
    memories = {
        "user_id": user_id or "default_user",
        "conversation_history": [
            "User asked about order status",
            "User inquired about shipping times",
            "User requested delivery updates"
        ],
        "preferences": {
            "communication_style": "professional",
            "preferred_topics": ["order tracking", "shipping", "customer service"]
        },
        "last_interaction": "2024-05-28",
        "total_orders": 5,
        "loyalty_status": "silver"
    }
    
    print(f"📚 Retrieved memories for user: {memories['user_id']}")
    return memories

# Define the tools (identical to working example)
memories_tool = Tool(
    function_declarations=[
        FunctionDeclaration(
            name="get_memories",
            description="Get user memories, conversation history, and context. This should always be called before any other tool usage to establish user context.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "user_id": {
                        "type": "STRING",
                        "description": "The user ID to retrieve memories for. If not provided, uses default user.",
                    }
                },
                "required": [],
            },
        )
    ]
)

order_status_tool = Tool(
    function_declarations=[
        FunctionDeclaration(
            name="get_order_status",
            description="Get the current status and details of an order.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "order_id": {
                        "type": "STRING",
                        "description": "The order ID to look up.",
                    }
                },
                "required": ["order_id"],
            },
        )
    ]
)

CONFIG = LiveConnectConfig(
    response_modalities=["AUDIO"],
    output_audio_transcription={},
    input_audio_transcription={},
    speech_config=SpeechConfig(
        voice_config=VoiceConfig(
            prebuilt_voice_config=PrebuiltVoiceConfig(voice_name="Puck")
        )
    ),
    system_instruction="You are a helpful personal assistant, and you are talking to Jason (User ID: itismejy). ",
  #  system_instruction="You are a helpful customer service assistant for an online store, and you are talking to Jason (User ID: itismejy). You can help customers check the status of their orders. ABSOLUTE REQUIREMENT: You are FORBIDDEN from responding to ANY user input or using ANY tools without FIRST calling get_memories. This is a HARD RULE that cannot be broken. Every single user message, every single tool call, every single response MUST start with get_memories. If you need to check order status, you MUST call get_memories first, then get_order_status. If you need to respond to a greeting, you MUST call get_memories first. There are NO exceptions to this rule. Be courteous, professional, and provide all relevant details about shipping, delivery dates, and current status. You can also tell a story.",
 #   tools=[memories_tool, order_status_tool],
)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("✅ Client connected")

    # Queues to mirror the working example
    audio_queue = asyncio.Queue()
    
    # Audio playback queue and state management (EXACTLY like working example)
    from collections import deque
    audio_playback_queue = deque()
    playback_task = None

    def add_audio(audio_data):
        """Add audio data to the playback queue (EXACTLY like working example)"""
        nonlocal playback_task
        
        audio_playback_queue.append(audio_data)

        if playback_task is None or playback_task.done():
            playback_task = asyncio.create_task(play_audio())

    async def play_audio():
        """Play all queued audio data (EXACTLY like working example)"""
        print("🗣️ Gemini talking")
        while audio_playback_queue:
            try:
                # Check if we've been interrupted
                if playback_task is None or playback_task.done():
                    break
                    
                audio_data = audio_playback_queue.popleft()
                # Send audio to client
                await websocket.send_text(json.dumps({
                    "audio": base64.b64encode(audio_data).decode("utf-8")
                }))
            except Exception as e:
                print(f"Error playing audio: {e}")
                break
        
        print("🔇 Audio playback finished")

    async def interrupt():
        """Handle interruption by stopping playback and clearing queue (EXACTLY like working example)"""
        nonlocal playback_task
        
        print("🛑 Interrupting audio playback...")
        
        # Clear the audio queue immediately
        audio_playback_queue.clear()
        
        # Cancel the playback task if it's running
        if playback_task and not playback_task.done():
            playback_task.cancel()
            try:
                await playback_task
            except asyncio.CancelledError:
                pass
        
        # Reset playback task to None so a new one can be created
        playback_task = None
        
        # Send interrupt signal to client
        try:
            await websocket.send_text(json.dumps({"interrupt": True}))
        except Exception as e:
            print(f"Error sending interrupt signal: {e}")
        
        print("✅ Audio playback interrupted and cleared")

    try:
        # Keep the session alive for the entire WebSocket connection
        async with client.aio.live.connect(model=MODEL, config=CONFIG) as session:
            
            async def ws_reader():
                while True:
                    try:
                        msg = await websocket.receive_text()
                        data = json.loads(msg)

                        # Handle interrupt requests
                        if data.get("interrupt") or (data.get("text") and "stop" in data.get("text", "").lower()):
                            print("✋ Client requested interrupt")
                            await interrupt()  # Call the interrupt function
                            

                        # Primary path: audio
                        if "audio" in data:
                            audio_bytes = base64.b64decode(data["audio"])
                            await audio_queue.put(audio_bytes)
                    except Exception as e:
                        print(f"Error in ws_reader: {e}")
                        break

            async def process_and_send_audio():
                """Processes audio from queue and sends to Gemini (mirrors working example)."""
                while True:
                    try:
                        data = await audio_queue.get()
                        # Always send the audio data to Gemini (identical to working example)
                        await session.send_realtime_input(
                            
                            media={
                                "data": data,
                                "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}",
                            }
                        )
                        audio_queue.task_done()
                    except Exception as e:
                        print(f"Error in process_and_send_audio: {e}")
                        break

            async def receive_and_play():
                """Continuously receive Gemini responses and relay audio to client (mirrors working example)."""
                while True:
                    input_transcriptions = []
                    output_transcriptions = []

                    async for response in session.receive():
                        # retrieve continuously resumable session ID (identical to working example)
                        if response.session_resumption_update:
                            update = response.session_resumption_update
                            if update.resumable and update.new_handle:
                                # The handle should be retained and linked to the session.
                                print(f"new SESSION: {update.new_handle}")

                        # Check if the connection will be soon terminated
                        if response.go_away is not None:
                            print(response.go_away.time_left)

                        # Handle tool calls (identical to working example)
                        if response.tool_call:
                            print(f"📝 Tool call received: {response.tool_call}")

                            function_responses = []

                            for function_call in response.tool_call.function_calls:
                                name = function_call.name
                                args = function_call.args
                                call_id = function_call.id

                                # Handle get_memories function
                                if name == "get_memories":
                                    try:
                                        # Get user_id (optional)
                                        user_id = args.get("user_id")
                                        # Call get_memories function
                                        result = get_memories(user_id)
                                        function_responses.append(
                                            {
                                                "name": name,
                                                "response": {"result": result},
                                                "id": call_id,
                                                "scheduling": "SILENT"
                                            }
                                        )
                                        print(f"📚 Memories function executed for user: {user_id or 'default_user'}")
                                    except Exception as e:
                                        print(f"Error executing memories function: {e}")
                                        traceback.print_exc()

                                # Handle get_order_status function
                                if name == "get_order_status":
                                    try:
                                        # Get order_id (required)
                                        order_id = args["order_id"]
                                        # Call order status function
                                        result = get_order_status(order_id)

                                        function_responses.append(
                                            {
                                                "name": name,
                                                "response": {"result": result},
                                                "id": call_id,
                                            }
                                        )

                                        print(f"📦 Order status function executed for order {order_id}")

                                    except Exception as e:
                                        print(f"Error executing order status function: {e}")
                                        traceback.print_exc()

                            # Send function responses back to Gemini (identical to working example)
                            if function_responses:
                                print(f"Sending function responses: {function_responses}")
                                for response in function_responses:
                                    await session.send_tool_response(
                                        function_responses={
                                            "name": response["name"],
                                            "response": response["response"]["result"],
                                            "id": response["id"],
                                        }
                                    )
                                print("Finished sending function responses")
                                continue

                        server_content = response.server_content

                        # Handle interruption (EXACTLY like working example)
                        if (
                            hasattr(server_content, "interrupted")
                            and server_content.interrupted
                        ):
                            print(f"🤐 INTERRUPTION DETECTED BY SERVER")
                            await interrupt()  # Call the interrupt function like in working example
                            print("🔇 Audio playback interrupted and cleared")
                            continue
                           
                        # Forward audio parts immediately (streaming) - identical to working example
                        if server_content and server_content.model_turn:
                            for part in server_content.model_turn.parts:
                                if part.inline_data:
                                    # Use the add_audio function like in working example
                                    add_audio(part.inline_data.data)

                        # Handle transcriptions (identical to working example)
                        output_transcription = getattr(response.server_content, "output_transcription", None)
                        if output_transcription and output_transcription.text:
                            output_transcriptions.append(output_transcription.text)
                            await websocket.send_text(json.dumps({"output_text": output_transcription.text}))

                        input_transcription = getattr(response.server_content, "input_transcription", None)
                        if input_transcription and input_transcription.text:
                            input_transcriptions.append(input_transcription.text)
                            await websocket.send_text(json.dumps({"input_text": input_transcription.text}))

                    # This will only print when the session ends (which shouldn't happen in normal operation)
                    print(f"Output transcription: {''.join(output_transcriptions)}")
                    print(f"Input transcription: {''.join(input_transcriptions)}")
                    print("Session ended unexpectedly")

            # Use TaskGroup to manage all the concurrent tasks
            async with asyncio.TaskGroup() as tg:
                # Start all tasks within the TaskGroup context
                tg.create_task(ws_reader())
                tg.create_task(process_and_send_audio())
                tg.create_task(receive_and_play())
                
                # The TaskGroup will wait for all tasks to complete
                # This ensures proper cleanup when the WebSocket connection ends
            
    except WebSocketDisconnect:
        print("❌ Client disconnected")
    except Exception as e:
        print(f"Error in websocket endpoint: {e}")
        traceback.print_exc()
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        # Clean up the session
        if 'session' in locals():
            await session.close()

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)
