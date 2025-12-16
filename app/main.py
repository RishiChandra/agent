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
from session_management_utils import get_session, create_session, update_session_status
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
    return {"ok": True, "last_updated": "Dec 6 4:03 PST"}


# ===== Gemini config =====
PROJECT_ID = "ai-pin-465902"
LOCATION = "us-central1"
MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
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
        "You also have access to a Google Search tool for information that can be easily found online; use it when appropriate, "
        "but continue to prefer the think tool, especially when you need any personal information about the user."
    ),
   tools=[{"google_search": {}}, think_tool],
)

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    print(f"✅ Client connected with user_id: {user_id}")
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
        # Get the database session for the user_id
        #user_id = websocket.user_id
        db_session = get_session(user_id)
        print(f"🔄 DB SESSION: {db_session}")
        if not db_session:
            db_session = create_session(user_id)
        else:
            print(f"🔄 SESSION FOUND FOR USER {user_id}")
            print(f"🔄 SESSION: {db_session}")
            update_session_status(user_id, True)

        
        # Keep the Gemini session alive for the entire WebSocket connection
        async with client.aio.live.connect(model=MODEL, config=CONFIG) as gemini_session:

            async def send_client_content(content=None, mark_turn_complete=True):
                """Helper method to send text content to Gemini.
                
                Args:
                    content: The message content to send to Gemini. Can be:
                        - A string (simple text message): "Hello, how are you?"
                        - A single dict with role and parts: {"role": "user", "parts": [{"text": "Hello"}]}
                        - A list of dicts for multiple turns: [
                            {"role": "user", "parts": [{"text": "What's the weather?"}]},
                            {"role": "model", "parts": [{"text": "I don't have access to weather data."}]},
                            {"role": "user", "parts": [{"text": "Can you help me find it online?"}]}
                          ]
                        Examples:
                            # Simple string
                            await send_client_content("What time is it?")
                            
                            # Single dict format
                            await send_client_content({"role": "user", "parts": [{"text": "Tell me a joke"}]})
                            
                            # Multiple turns (conversation history)
                            await send_client_content([
                                {"role": "user", "parts": [{"text": "My name is Jason"}]},
                                {"role": "model", "parts": [{"text": "Nice to meet you, Jason!"}]},
                                {"role": "user", "parts": [{"text": "What did I just tell you my name was?"}]}
                            ])
                    
                    mark_turn_complete: Boolean indicating whether to mark the turn as complete.
                        When True (default), Gemini will process the message immediately.
                        When False, allows sending partial content that will be completed later.
                        Example:
                            # Send partial message
                            await send_client_content("This is part one...", mark_turn_complete=False)
                            await send_client_content("...and this is part two.", mark_turn_complete=True)
                """
                if content:
                    try:
                        # Extract text for logging before conversion
                        if isinstance(content, str):
                            text_to_log = content
                        elif isinstance(content, list):
                            # For multiple turns, log the last user message
                            text_to_log = content[-1].get('parts', [{}])[0].get('text', '') if content else ''
                        else:
                            text_to_log = content.get('parts', [{}])[0].get('text', '')
                        
                        # If content is a string, convert it to the proper format
                        if isinstance(content, str):
                            content = {"role": "user", "parts": [{"text": content}]}
                        
                        # Send text input to Gemini using send_client_content
                        await gemini_session.send_client_content(
                            turns=content,
                            turn_complete=mark_turn_complete
                        )
                        print(f"📤 Sent text to Gemini: {text_to_log}")
                    except Exception as e:
                        print(f"Error sending text to Gemini: {e}")
                        traceback.print_exc()

            async def ws_reader():
                while True:
                    try:
                        msg = await websocket.receive_text()
                        data = json.loads(msg)

                        # Handle interrupt requests
                        if data.get("interrupt") or (data.get("text") and "stop" in data.get("text", "").lower()):
                            print("✋ Client requested interrupt")
                            await interrupt()  # Call the interrupt function
                            continue

                        # Handle text input (supports multiple formats)
                        text_content = None
                        turn_complete = True
                        
                        if "turns" in data:
                            text_content = data["turns"]
                            turn_complete = data.get("turn_complete", True)
                        elif "text" in data:
                            text_content = data["text"]
                            turn_complete = data.get("turn_complete", True)
                        elif "input_text" in data:
                            text_content = data["input_text"]
                            turn_complete = data.get("turn_complete", True)
                        
                        if text_content:
                            await send_client_content(content=text_content, mark_turn_complete=turn_complete)
                            continue

                        # Primary path: audio
                        if "audio" in data:
                            audio_bytes = base64.b64decode(data["audio"])
                            await audio_queue.put(audio_bytes)
                    except WebSocketDisconnect:
                        # Re-raise to be caught by outer handler
                        raise
                    except Exception as e:
                        # Check if this is a connection closure
                        error_str = str(e)
                        error_repr = repr(e)
                        # Handle various connection closure indicators:
                        # - WebSocket close codes like (1000, '')
                        # - Connection closed/disconnected messages
                        # - RuntimeError/ConnectionError from websockets library
                        is_connection_closed = (
                            "closed" in error_str.lower() or 
                            "disconnect" in error_str.lower() or 
                            "(1000" in error_repr or  # Close code 1000 (normal closure)
                            isinstance(e, (RuntimeError, ConnectionError, OSError))
                        )
                        
                        if is_connection_closed:
                            print(f"🔄 Connection closed detected: {e}")
                            raise WebSocketDisconnect()
                        
                        print(f"Error in ws_reader: {e}")
                        break

            async def process_and_send_audio():
                """Processes audio from queue and sends to Gemini (mirrors working example)."""
                while True:
                    try:
                        data = await audio_queue.get()
                        # Always send the audio data to Gemini (identical to working example)
                        await gemini_session.send_realtime_input(
                            
                            media={
                                "data": data,
                                "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}",
                            }
                        )
                        audio_queue.task_done()
                    except Exception as e:
                        print(f"Error in process_and_send_audio: {e}")
                        print(traceback.format_exc())
                        break

            async def receive_and_play():
                """Continuously receive Gemini responses and relay audio to client (mirrors working example)."""
                # Track recent output transcriptions to filter out echo/feedback
                from collections import deque
                recent_outputs = deque(maxlen=10)  # Keep last 10 output transcriptions
                
                while True:
                    input_transcriptions = []
                    output_transcriptions = []

                    async for response in gemini_session.receive():
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
                                
                                await gemini_session.send_tool_response(function_responses=gemini_function_responses)
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
                            break
                           
                        # Forward audio parts immediately (streaming) - identical to working example
                        if server_content and server_content.model_turn:
                            for part in server_content.model_turn.parts:
                                if part.inline_data:
                                    # Use the add_audio function like in working example
                                    add_audio(part.inline_data.data)

                        # Handle transcriptions (identical to working example)
                        output_transcription = getattr(response.server_content, "output_transcription", None)
                        if output_transcription and output_transcription.text:
                            output_text = output_transcription.text.strip()
                            output_transcriptions.append(output_text)
                            recent_outputs.append(output_text.lower())  # Store lowercase for comparison
                            await websocket.send_text(json.dumps({"output_text": output_text}))

                        input_transcription = getattr(response.server_content, "input_transcription", None)
                        if input_transcription and input_transcription.text:
                            input_text = input_transcription.text.strip()
                            
                            # Filter out input transcriptions that match recent output (prevent echo/feedback)
                            input_lower = input_text.lower()
                            is_echo = False
                            
                            # Check if input matches any recent output (exact, substring, or significant word overlap)
                            for recent_output in recent_outputs:
                                # Check for exact match or substring match
                                if input_lower == recent_output or input_lower in recent_output or recent_output in input_lower:
                                    is_echo = True
                                    break
                                
                                # Check for significant word overlap (more than 50% of words match)
                                input_words = set(input_lower.split())
                                output_words = set(recent_output.split())
                                if len(input_words) > 0 and len(output_words) > 0:
                                    overlap = len(input_words & output_words) / max(len(input_words), len(output_words))
                                    if overlap > 0.5:
                                        is_echo = True
                                        break
                            
                            if is_echo:
                                print(f"🚫 Filtered echo input transcription: '{input_text}' (matches recent output)")
                                continue
                            
                            input_transcriptions.append(input_text)
                            await websocket.send_text(json.dumps({"input_text": input_text}))

                    # This will only print when the session ends (which shouldn't happen in normal operation)
                    print(f"Output transcription: {''.join(output_transcriptions)}")
                    print(f"Input transcription: {''.join(input_transcriptions)}")
                    print("Session ended unexpectedly")

            # Use TaskGroup to manage all the concurrent tasks
            try:
                async with asyncio.TaskGroup() as tg:
                    # Start all tasks within the TaskGroup context
                    tg.create_task(ws_reader())
                    tg.create_task(process_and_send_audio())
                    tg.create_task(receive_and_play())
                    
                    # The TaskGroup will wait for all tasks to complete
                    # This ensures proper cleanup when the WebSocket connection ends
            except* Exception as eg:
                # TaskGroup wraps exceptions in ExceptionGroup
                # Check if any exception in the group is a WebSocketDisconnect
                for exc in eg.exceptions:
                    if isinstance(exc, WebSocketDisconnect):
                        raise exc
                # If not, re-raise the exception group
                raise
            
    except WebSocketDisconnect:
        print("❌ Client disconnected")
        # Update the session status to inactive
        update_session_status(user_id, False)
    except Exception as e:
        print(f"Error in websocket endpoint: {e}")
        traceback.print_exc()
        try:
            await websocket.close()
        except Exception:
            pass
        update_session_status(user_id, False)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
        ws="websockets",          # ensure the websockets backend
        ws_ping_interval=None,    # completely disable server pings
        ws_ping_timeout=None,      # disable timeout

    )

        #     ws_ping_interval=None,    # completely disable server pings
        # ws_ping_timeout=None      # disable timeout