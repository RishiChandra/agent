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

from agents import general_thinking_agent

# Instantiate the general thinking agent
generalThinkingAgent = general_thinking_agent.GeneralThinkingAgent()

# python -m uvicorn app.main:app --host 0.0.0.0 --port \$PORT

# https://ai.google.dev/gemini-api/docs/live-guide
load_dotenv()

from fastapi import FastAPI
app = FastAPI()

@app.get("/healthz")
def healthz():
    print("/healthz called and accepted")
    return {"ok": True, "last_updated": "Oct 26 18:45"}


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

think_tool = Tool(
    function_declarations=[
        FunctionDeclaration(
            name="think_and_repeat_output",
            behavior="NON_BLOCKING",
            description="Think about the user input and return the thinking results. Use this information to provide a helpful response.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "user_input": {
                        "type": "STRING",
                        "description": "The user input to think about. This should be the exact user input that was just said.",
                    }
                },
                "required": ["user_input"],
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
    #system_instruction="You are a helpful personal assistant, and you are talking to Jason (User ID: itismejy). ",
    system_instruction=(
        "You are a helpful assistant. For any user input, you want to have sufficient "
        "information before you make a response. If you don't have sufficient information, "
        "think about it first using the think tool, and then use the information returned "
        "by the think tool to provide a helpful response to the user. If you use the Think "
        "tool, provide a short response to the user such as \"Let me see\" immediately and then wait for the tool to complete. "
        "If used, always incorporate the results from the think tool into your response."
    ),
   tools=[think_tool],
)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("✅ Client connected")
    print("🚫 Server WS pings DISABLED")

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
                                if name == "think_and_repeat_output":
                                    try:
                                        # Get user_id (optional)
                                        user_id = args.get("user_input")
                                        # Call think_and_repeat_output function
                                        result = generalThinkingAgent.think(user_id)
                                        print(f"generalThinkingAgent.think(user_id) {result}")
                                        return_string = f"{result}."
                                        function_responses.append(
                                            {
                                                "name": name,
                                                "response": {"result": return_string},
                                                "id": call_id,
                                                "scheduling": "WHEN_IDLE"
                                            }
                                        )
                                    except Exception as e:
                                        print(f"Error: {e}")
                                        traceback.print_exc()


                            # Send function responses back to Gemini
                            if function_responses:
                                print(f"Sending function responses: {function_responses}")
                                print(f"function_responses: {function_responses[0]['response']}")
                                
                                # Create proper FunctionResponse objects
                                gemini_function_responses = []
                                for response in function_responses:
                                    gemini_response = types.FunctionResponse(
                                        id=response["id"],
                                        name=response["name"],
                                        response=response["response"]
                                    )
                                    gemini_function_responses.append(gemini_response)
                                
                                await session.send_tool_response(function_responses=gemini_function_responses)
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="debug",
        ws="websockets",          # ensure the websockets backend
        ws_ping_interval=None,    # completely disable server pings
        ws_ping_timeout=None,      # disable timeout

    )

        #     ws_ping_interval=None,    # completely disable server pings
        # ws_ping_timeout=None      # disable timeout