import json
import base64
import asyncio
import traceback
import time
from typing import List, Dict, Any, Optional

from fastapi import WebSocket, WebSocketDisconnect
from google.genai import types
from agents import general_thinking_agent
from gemini_config import (
    client,
    MODEL,
    SEND_SAMPLE_RATE,
)
from user_session_manager import UserSessionManager, update_user_session_status
from routes.message_crud import (
    get_pending_messages_for_user,
    mark_messages_as_read,
    clear_pending_text_message_job_for_user,
)
from routes.task_crud import get_task_by_id
from audio_manager import AudioManager
from transcription_handler import TranscriptionHandler

# Instantiate the general thinking agent
generalThinkingAgent = general_thinking_agent.GeneralThinkingAgent()


async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    print(f"✅ Client connected with user_id: {user_id}")

    def _normalize_text(text: str) -> str:
        """Lowercase and collapse whitespace for stable comparisons."""
        if not isinstance(text, str):
            return ""
        return " ".join(text.lower().strip().split())

    # Track user inputs that have already been processed by the think tool to avoid loops
    processed_tool_inputs = set()

    # Initialize user session and get configuration
    session_manager = None
    try:
        session_manager = UserSessionManager(user_id)
        user_config = session_manager.user_config
        config = session_manager.config
        scratchpad = session_manager.scratchpad
        
        # Audio manager (handles both input and output audio queues)
        audio_manager = AudioManager(websocket)
        
        # Transcription handler for processing input/output transcriptions
        transcription_handler = TranscriptionHandler(scratchpad, websocket)
        
        # Keep the Gemini session alive for the entire WebSocket connection
        async with client.aio.live.connect(model=MODEL, config=config) as gemini_session:

            async def send_client_content(content=None, mark_turn_complete=True):
                """Helper method to send JSON content to Gemini.
                
                Args:
                    content: The message content to send to Gemini. Must be JSON format:
                        - A single dict with role and parts: {"role": "user", "parts": [{"text": "Hello"}]}
                        - A list of dicts for multiple turns: [
                            {"role": "user", "parts": [{"text": "What's the weather?"}]},
                            {"role": "model", "parts": [{"text": "I don't have access to weather data."}]},
                            {"role": "user", "parts": [{"text": "Can you help me find it online?"}]}
                          ]
                        Examples:
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
                """
                if content:
                    # Extract text for logging
                    if isinstance(content, list):
                        # For multiple turns, log the last user message
                        text_to_log = content[-1].get('parts', [{}])[0].get('text', '') if content else ''
                    else:
                        text_to_log = content.get('parts', [{}])[0].get('text', '')
                    
                    # Send JSON content to Gemini
                    await gemini_session.send_client_content(
                        turns=content,
                        turn_complete=mark_turn_complete
                    )
                    print(f"📤 Sent text to Gemini: {text_to_log}")

            async def ws_reader():
                while True:
                    try:
                        msg = await websocket.receive_text()
                        print(f"[DEBUG] WebSocket received raw message (length={len(msg)})")
                        data = json.loads(msg)
                        print(f"[DEBUG] WebSocket message parsed keys={list(data.keys())}")

                        if data.get("text") is not None:
                            print(f"[DEBUG] Received text (top-level) user_id={user_id} text={data.get('text')!r}")

                        # Handle interrupt requests
                        if data.get("interrupt") or (data.get("text") and "stop" in data.get("text", "").lower()):
                            print("✋ Client requested interrupt")
                            await audio_manager.interrupt()
                            continue

                        # --- pending_message true: get messages from DB and ask AI to tell the user about incoming messages ---
                        # ESP32 may send this inside turns as a JSON string: {"command":"start_websocket","reason":"text_message","pending_messages":true,...}
                        parsed_turns = None
                        if "turns" in data:
                            raw_turns = data["turns"]
                            if isinstance(raw_turns, str):
                                try:
                                    parsed_turns = json.loads(raw_turns)
                                except (json.JSONDecodeError, TypeError):
                                    parsed_turns = None
                            else:
                                parsed_turns = raw_turns
                        pending_from_turns = (
                            isinstance(parsed_turns, dict)
                            and (
                                parsed_turns.get("pending_messages") is True
                                or parsed_turns.get("reason") == "text_message"
                            )
                        )
                        pending_task_from_turns = (
                            isinstance(parsed_turns, dict)
                            and (
                                parsed_turns.get("pending_task") is True
                                or parsed_turns.get("reason") == "task"
                            )
                        )
                        if pending_from_turns:
                            print(f"[DEBUG] ESP32 text_message/pending_messages in turns user_id={user_id}")
                        if pending_task_from_turns:
                            print(f"[DEBUG] ESP32 pending_task/task in turns user_id={user_id}")
                        if data.get("pending_message") is True or data.get("pending_messages") is True or pending_from_turns:
                            commit_audio_buffer("user")
                            commit_audio_buffer("agent")
                            pending_list = await asyncio.to_thread(get_pending_messages_for_user, user_id)
                            if pending_list:
                                lines = [
                                    f"From {m['sender_name']}: {m['content']}"
                                    for m in pending_list
                                ]
                                raw_messages = "\n".join(lines)
                                instruction = (
                                    "The user has new incoming messages. Tell them about these messages in a natural, "
                                    "helpful way. Do not invent or add any messages; only report what is below.\n\n"
                                    "Incoming messages:\n"
                                )
                                message = instruction + raw_messages
                                add_to_scratchpad(source="user", format="text", content=message)
                                gemini_content = {"role": "user", "parts": [{"text": message}]}
                                await send_client_content(content=gemini_content, mark_turn_complete=True)
                                print(f"📤 Sent {len(pending_list)} pending message(s) to Gemini (instructed to tell user)")
                                await asyncio.to_thread(mark_messages_as_read, pending_list)
                                await asyncio.to_thread(clear_pending_text_message_job_for_user, user_id)
                            continue

                        # --- pending_task true: get task (from payload or DB) and ask AI to tell the user about the task ---
                        # ESP32 may send this inside turns as a JSON string: {"command":"...","reason":"task","pending_task":true,"task_id":"...",...}
                        if data.get("pending_task") is True or pending_task_from_turns:
                            commit_audio_buffer("user")
                            commit_audio_buffer("agent")
                            task_source = parsed_turns if (pending_task_from_turns and isinstance(parsed_turns, dict)) else data
                            task_id = task_source.get("task_id")
                            task = None
                            if task_id:
                                try:
                                    task = await asyncio.to_thread(get_task_by_id, task_id)
                                except Exception:
                                    task = None
                            if task is None and (task_source.get("task_id") or task_source.get("title") or task_source.get("description")):
                                task = {
                                    "task_id": task_source.get("task_id"),
                                    "task_info": task_source.get("task_info") or {"title": task_source.get("title"), "description": task_source.get("description") or task_source.get("info", "")},
                                    "time_to_execute": task_source.get("time_to_execute"),
                                }
                            if task:
                                info = task.get("task_info") or {}
                                if isinstance(info, dict):
                                    title = info.get("title") or info.get("info", "Task")
                                    desc = info.get("description") or info.get("info", "")
                                else:
                                    title, desc = "Task", str(info)
                                when = task.get("time_to_execute") or "now"
                                instruction = (
                                    "It is time for the user to do this task. Tell them about it in a natural, helpful way. "
                                    "Do not invent any other tasks.\n\n"
                                    f"Task: {title}\nDescription: {desc}\nWhen: {when}"
                                )
                                add_to_scratchpad(source="user", format="text", content=instruction)
                                gemini_content = {"role": "user", "parts": [{"text": instruction}]}
                                await send_client_content(content=gemini_content, mark_turn_complete=True)
                                print("📤 Sent pending task to Gemini (instructed to tell user)")
                            continue

                        # Handle text input (supports multiple formats)
                        if "audio" not in data:
                            print(f"🔄 DATA: {data}")
                        json_content = None
                        turn_complete = True
                        if "turns" in data:
                            # Use parsed_turns if we already parsed (e.g. ESP32 sends turns as JSON string)
                            json_content = parsed_turns if parsed_turns is not None else data["turns"]
                            turn_complete = data.get("turn_complete", True)
                        # Only send to Gemini when we have a dict with actual chat content (message/task), not command payloads
                        if isinstance(json_content, dict) and ("message" in json_content or "task" in json_content):
                            # Commit any pending audio buffers before adding text input
                            commit_audio_buffer("user")
                            commit_audio_buffer("agent")
                            scratchpad.commit_audio_buffer("user")
                            scratchpad.commit_audio_buffer("agent")
                            
                            message = ""
                            if "message" in json_content:
                                message += json_content.get("message", "")
                            if "task" in json_content:
                                task = json_content.get("task", {})
                                message += json.dumps(task)
                                                            
                            scratchpad.add_entry(source="user", format="text", content=message)
                            gemini_content = {"role": "user", "parts": [{"text": message}]}
                            await send_client_content(content=gemini_content, mark_turn_complete=turn_complete)
                            continue

                        # Primary path: audio
                        if "audio" in data:
                            audio_bytes = base64.b64decode(data["audio"])
                            await audio_manager.audio_queue.put(audio_bytes)
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
                    data = await audio_manager.audio_queue.get()
                    # Always send the audio data to Gemini (identical to working example)
                    await gemini_session.send_realtime_input(
                        
                        media={
                            "data": data,
                            "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}",
                        }
                    )
                    audio_manager.audio_queue.task_done()

            async def receive_and_play():
                """Continuously receive Gemini responses and relay audio to client (mirrors working example)."""
                # Flag to track if we should close after receiving the goodbye audio
                should_close_after_audio = False
                last_audio_received_time = None
                
                while True:
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
                            # Commit any pending audio buffers before handling function calls
                            scratchpad.commit_audio_buffer("user")
                            scratchpad.commit_audio_buffer("agent")
                            
                            print(f"📝 Tool call received: {response.tool_call}")

                            function_responses = []

                            for function_call in response.tool_call.function_calls:
                                name = function_call.name
                                args = function_call.args
                                call_id = function_call.id

                                # Check if this is a status notification (not a real tool call)
                                # Status notifications have 'status' or 'id' in args but no actual function parameters
                                if "status" in args or ("id" in args and "user_input" not in args):
                                    print(f"📋 Status notification received: {args}")
                                    # Status notifications are informational, not tool calls to execute
                                    # We don't need to send a response for these
                                    continue

                                # Handle think function
                                if name == "think_and_repeat_output":
                                    # Only process if we have actual user input (not a status notification)
                                    if "user_input" in args:
                                        # Get user_id (optional)
                                        user_input = args.get("user_input")
                                        normalized_input = _normalize_text(user_input)

                                        # Skip duplicate tool calls for the same user input within this session
                                        if normalized_input in processed_tool_inputs:
                                            print(f"⚠️ Duplicate think_and_repeat_output for input '{user_input}', skipping execution.")
                                            # Return a clear message that the work is complete and no further action is needed
                                            function_responses.append(
                                                {
                                                    "name": name,
                                                    "response": {"result": "[COMPLETED] This request was already fully processed and completed. No further action needed. The task has been created and confirmed. Do not call this function again for this input."},
                                                    "id": call_id,
                                                    "scheduling": "WHEN_IDLE"
                                                }
                                            )
                                            # Continue to next iteration to avoid processing this duplicate
                                            continue
                                        else:
                                            processed_tool_inputs.add(normalized_input)
                                            # Call think_and_repeat_output function
                                            result = generalThinkingAgent.think(user_input, scratchpad.get_entries(), user_config)
                                            print(f"generalThinkingAgent.think(user_input) {result}")
                                            return_string = f"{result}."
                                            function_responses.append(
                                                {
                                                    "name": name,
                                                    "response": {"result": return_string},
                                                    "id": call_id,
                                                    "scheduling": "WHEN_IDLE"
                                                }
                                            )
                                    else:
                                        print(f"Think_and_repeat_output called but 'user_input' not in args: {args}")

                                # Handle end conversation function
                                elif name == "end_conversation":
                                    goodbye_message = args.get("goodbye_message", "Goodbye! Have a great day!")
                                    print(f"👋 Ending conversation: {goodbye_message}")
                                    
                                    # Send response to Gemini to acknowledge the tool call
                                    # This will cause Gemini to generate the goodbye audio
                                    gemini_end_response = types.FunctionResponse(
                                        id=call_id,
                                        name=name,
                                        response={"result": "Conversation ended successfully"}
                                    )
                                    await gemini_session.send_tool_response(function_responses=[gemini_end_response])
                                    print("✅ Sent end_conversation response to Gemini, waiting for goodbye audio...")
                                    
                                    # Set flag to close after we receive and send the goodbye audio
                                    should_close_after_audio = True
                                    
                                    # Don't return here - continue in the loop to receive the audio response
                                    continue


                            # Send function responses back to Gemini (only if we have actual responses)
                            if function_responses:
                                print(f"Sending function responses: {function_responses}")
                                print(f"function_responses: {function_responses[0]['response']}")
                                
                                # Add function responses to scratchpad
                                for func_response in function_responses:
                                    scratchpad.add_entry(
                                        source="agent",
                                        format="function_call",
                                        name=func_response["name"],
                                        response=func_response["response"],
                                        call_id=func_response["id"]
                                    )
                                
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
                            await audio_manager.interrupt()
                            print("🔇 Audio playback interrupted and cleared")
                            break
                           
                        # Forward audio parts immediately (streaming) - identical to working example
                        if server_content and server_content.model_turn:
                            for part in server_content.model_turn.parts:
                                if part.inline_data:
                                    # Use the audio manager to add audio
                                    audio_manager.add_audio(part.inline_data.data)
                                    # Track when we last received audio (for goodbye detection)
                                    if should_close_after_audio:
                                        last_audio_received_time = time.time()

                        # Check for turn completion - this indicates Gemini has finished generating the response
                        turn_complete = False
                        if server_content and hasattr(server_content, "turn_complete"):
                            turn_complete = server_content.turn_complete
                        
                        # If we're ending the conversation, wait for turn completion and audio playback
                        if should_close_after_audio:
                            # Check if turn is complete (either via turn_complete flag or by waiting for no new audio)
                            current_time = time.time()
                            
                            # If turn_complete is True, or if we haven't received audio in 1 second, proceed to close
                            should_proceed_to_close = False
                            if turn_complete:
                                print("✅ Gemini turn complete")
                                should_proceed_to_close = True
                            elif last_audio_received_time and (current_time - last_audio_received_time) > 1.0:
                                print("✅ No new audio received for 1 second, assuming turn complete")
                                should_proceed_to_close = True
                            
                            if should_proceed_to_close:
                                print("🎤 Goodbye turn complete, waiting for audio playback to finish...")
                                # Wait for playback queue to be empty and playback task to complete
                                max_wait_iterations = 100  # Maximum wait iterations (10 seconds at 0.1s per iteration)
                                wait_iterations = 0
                                while audio_manager.is_playing():
                                    if wait_iterations >= max_wait_iterations:
                                        print("⏱️ Timeout waiting for audio playback, closing anyway")
                                        break
                                    wait_iterations += 1
                                    await asyncio.sleep(0.1)
                                # Give a small additional delay to ensure audio is fully sent to client
                                await asyncio.sleep(1.0)  # Increased delay to ensure audio is fully played
                                print("✅ Goodbye audio playback complete, closing connection")
                                try:
                                    await websocket.send_text(json.dumps({
                                        "end_conversation": True
                                    }))
                                except Exception:
                                    pass  # Connection might already be closing
                                await websocket.close()
                                print("✅ WebSocket connection closed")
                                return

                        # Handle transcriptions using TranscriptionHandler
                        output_transcription = getattr(response.server_content, "output_transcription", None)
                        if output_transcription:
                            await transcription_handler.handle_output_transcription(output_transcription)

                        input_transcription = getattr(response.server_content, "input_transcription", None)
                        if input_transcription:
                            await transcription_handler.handle_input_transcription(input_transcription)

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
            
    except (WebSocketDisconnect, Exception) as e:
        # Commit any pending audio buffers before closing
        if session_manager:
            session_manager.scratchpad.commit_audio_buffer("user")
            session_manager.scratchpad.commit_audio_buffer("agent")
            print(f"Scratchpad: {session_manager.scratchpad.get_entries()}")
            session_manager.update_user_session_status(False)
        
        if isinstance(e, WebSocketDisconnect):
            print("❌ Client disconnected")
        else:
            print(f"Error in websocket endpoint: {e}")
            traceback.print_exc()
            try:
                await websocket.close()
            except Exception:
                pass
