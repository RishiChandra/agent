import os
import json
import base64
import asyncio
import traceback
import random
import time
from datetime import datetime, timedelta, UTC
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
from typing import Optional, TypedDict
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
from database import get_user_by_id
from agents import general_thinking_agent
from task_crud import (
    get_tasks_by_user_id,
    get_task_by_id,
    create_task,
    update_task,
    delete_task
)
from task_enqueue import enqueue_task as enqueue_task_to_service_bus
from zoneinfo import ZoneInfo

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
    return {"ok": True, "last_updated": "Dec 27 4:53 PST"}


# ===== Task API Models =====
class TaskCreateRequest(BaseModel):
    user_id: str
    task_info: Optional[dict] = None
    status: Optional[str] = None
    time_to_execute: Optional[str] = None  # ISO 8601 format datetime string
    timezone: Optional[str] = None  # Timezone name (e.g., "PST", "EST")
    timezone_offset: Optional[float] = None  # Timezone offset in hours (e.g., -8.0 for PST)
    enqueue: Optional[bool] = True  # Whether to enqueue to Service Bus after creating


class TaskUpdateRequest(BaseModel):
    task_info: Optional[dict] = None
    status: Optional[str] = None
    time_to_execute: Optional[str] = None  # ISO 8601 format datetime string
    timezone: Optional[str] = None  # Timezone name (e.g., "PST", "EST")
    timezone_offset: Optional[float] = None  # Timezone offset in hours (e.g., -8.0 for PST)


class TaskEnqueueRequest(BaseModel):
    task_id: str
    user_id: str
    task_info: Optional[dict] = None
    status: Optional[str] = None
    time_to_execute: Optional[str] = None  # ISO 8601 format datetime string


# ===== Task CRUD Endpoints =====

@app.get("/tasks/{user_id}")
async def get_tasks(user_id: str):
    """
    Get all tasks for a specific user.
    
    Args:
        user_id: The user ID to fetch tasks for
        
    Returns:
        List of tasks
    """
    try:
        tasks = get_tasks_by_user_id(user_id)
        return {"tasks": tasks, "count": len(tasks)}
    except Exception as e:
        print(f"Error fetching tasks: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error fetching tasks: {str(e)}")


@app.get("/tasks/{user_id}/{task_id}")
async def get_task(user_id: str, task_id: str):
    """
    Get a single task by ID.
    
    Args:
        user_id: The user ID (for validation)
        task_id: The task ID to fetch
        
    Returns:
        Task dictionary
    """
    try:
        task = get_task_by_id(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        
        # Verify the task belongs to the user
        if task["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Task does not belong to this user")
        
        return task
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching task: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error fetching task: {str(e)}")


@app.post("/tasks")
async def create_task_endpoint(request: TaskCreateRequest):
    """
    Create a new task.
    
    If enqueue is True (default), the task will also be enqueued to Service Bus.
    
    Returns:
        Created task dictionary
    """
    try:
        # Ensure time_to_execute has the correct timezone (user's timezone, not UTC)
        time_to_execute_final = request.time_to_execute
        if request.time_to_execute and request.timezone_offset is not None:
            try:
                from datetime import timezone, timedelta
                # Parse the datetime string (it may not have timezone info)
                dt_str = request.time_to_execute.replace('Z', '+00:00')
                dt = datetime.fromisoformat(dt_str)
                # If datetime doesn't have timezone info, assume it's in the provided timezone
                if dt.tzinfo is None:
                    # Create timezone from offset
                    tz = timezone(timedelta(hours=request.timezone_offset))
                    dt = dt.replace(tzinfo=tz)
                # If it's in UTC, convert to user's timezone (don't store in UTC)
                elif dt.tzinfo == UTC or str(dt.tzinfo) == "UTC":
                    # Convert from UTC to user's timezone
                    user_tz = timezone(timedelta(hours=request.timezone_offset))
                    dt = dt.astimezone(user_tz)
                # Keep the timezone as-is (respect user's timezone)
                time_to_execute_final = dt.isoformat()
            except Exception as e:
                print(f"Warning: Failed to set timezone for time_to_execute: {e}")
                # Fall back to original value
        
        # Create task in database (with optional enqueue)
        task = create_task(
            user_id=request.user_id,
            task_info=request.task_info,
            status=request.status,
            time_to_execute=time_to_execute_final,
            enqueue=request.enqueue if request.enqueue is not None else True
        )
        return task
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"Error creating task: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error creating task: {str(e)}")


@app.put("/tasks/{user_id}/{task_id}")
async def update_task_endpoint(user_id: str, task_id: str, request: TaskUpdateRequest):
    """
    Update an existing task.
    
    Args:
        user_id: The user ID (for validation)
        task_id: The task ID to update
        
    Returns:
        Updated task dictionary
    """
    try:
        # Verify the task exists and belongs to the user
        existing_task = get_task_by_id(task_id)
        if existing_task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        
        if existing_task["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Task does not belong to this user")
        
        # Ensure time_to_execute has the correct timezone (user's timezone, not UTC)
        time_to_execute_final = request.time_to_execute
        if request.time_to_execute and request.timezone_offset is not None:
            try:
                from datetime import timezone, timedelta
                # Parse the datetime string (it may not have timezone info)
                dt_str = request.time_to_execute.replace('Z', '+00:00')
                dt = datetime.fromisoformat(dt_str)
                # If datetime doesn't have timezone info, assume it's in the provided timezone
                if dt.tzinfo is None:
                    # Create timezone from offset
                    tz = timezone(timedelta(hours=request.timezone_offset))
                    dt = dt.replace(tzinfo=tz)
                # If it's in UTC, convert to user's timezone (don't store in UTC)
                elif dt.tzinfo == UTC or str(dt.tzinfo) == "UTC":
                    # Convert from UTC to user's timezone
                    user_tz = timezone(timedelta(hours=request.timezone_offset))
                    dt = dt.astimezone(user_tz)
                # Keep the timezone as-is (respect user's timezone)
                time_to_execute_final = dt.isoformat()
            except Exception as e:
                print(f"Warning: Failed to set timezone for time_to_execute: {e}")
                # Fall back to original value
        
        # Update task
        task = update_task(
            task_id=task_id,
            task_info=request.task_info,
            status=request.status,
            time_to_execute=time_to_execute_final
        )
        
        return task
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"Error updating task: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error updating task: {str(e)}")


@app.delete("/tasks/{user_id}/{task_id}")
async def delete_task_endpoint(user_id: str, task_id: str):
    """
    Delete a task by ID.
    
    Args:
        user_id: The user ID (for validation)
        task_id: The task ID to delete
        
    Returns:
        Success message
    """
    try:
        # Verify the task exists and belongs to the user
        existing_task = get_task_by_id(task_id)
        if existing_task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        
        if existing_task["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Task does not belong to this user")
        
        # Delete task
        deleted = delete_task(task_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Task not found")
        
        return {"success": True, "message": "Task deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error deleting task: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error deleting task: {str(e)}")


# ===== Task Enqueue Endpoint (standalone) =====

@app.post("/enqueue-task")
async def enqueue_task_endpoint(request: TaskEnqueueRequest):
    """
    Enqueue an existing task to Azure Service Bus queue.
    
    This endpoint is for enqueueing tasks that already exist in the database.
    If time_to_execute is provided, the message will be scheduled for that time.
    Otherwise, it will be sent immediately.
    """
    try:
        result = enqueue_task_to_service_bus(
            task_id=request.task_id,
            user_id=request.user_id,
            task_info=request.task_info,
            time_to_execute=request.time_to_execute
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"Error enqueueing task: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error enqueueing task to Service Bus: {str(e)}")


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
            description=(
                "Think about the user input and return the thinking results. Use this information to provide a helpful response. "
                "IMPORTANT: Only call this function ONCE per unique user input. If you see a response indicating the request was already processed, "
                "do NOT call this function again for that same input. Move on to generating your audio response instead."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "user_input": {
                        "type": "STRING",
                        "description": "The user input to think about. This should be the exact user input that was just said. Only call this function once per unique input.",
                    }
                },
                "required": ["user_input"],
            },
        )
    ]
)

end_conversation_tool = Tool(
    function_declarations=[
        FunctionDeclaration(
            name="end_conversation",
            behavior="NON_BLOCKING",
            description="Use this tool when the user indicates they want to end the conversation. This will say goodbye and close the connection.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "goodbye_message": {
                        "type": "STRING",
                        "description": "A friendly goodbye message to send to the user before ending the conversation.",
                    }
                },
                "required": ["goodbye_message"],
            },
        )
    ]
)

class UserConfigData(TypedDict):
    """Data structure for user config parameters."""
    user_info: Optional[dict]
    user_name: str
    current_time_str: str
    current_date_str: str
    timezone: str

def get_live_config(config_data: UserConfigData) -> LiveConnectConfig:
    """Generate LiveConnectConfig with user-specific information."""
    user_name = config_data["user_name"]
    current_time_str = config_data["current_time_str"]
    current_date_str = config_data["current_date_str"]
    timezone = config_data["timezone"]

    return LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription={},
        input_audio_transcription={},
        speech_config=SpeechConfig(
            voice_config=VoiceConfig(
                prebuilt_voice_config=PrebuiltVoiceConfig(voice_name="Puck")
            )
        ),
        system_instruction=(
            f"You are a personal secretary assistant for {user_name}. "
            f"You have access to their task management system. "
            f"The current time is {current_time_str}. "
            f"The current date is {current_date_str}. "
            f"The user's timezone is {timezone}. When creating or discussing tasks with times, "
            "use this timezone context to provide relevant time information.\n"
            "\n\n"
            "## CRITICAL: Function Call Rules\n"
            "- NEVER call the same function multiple times with the same input. Each unique user request should only trigger ONE function call.\n"
            "- If you see a function response indicating '[COMPLETED]' or 'already processed', that means the work is done. Do NOT call the function again.\n"
            "- After receiving a function response, generate your audio response to the user. Do NOT call the function again.\n"
            "\n\n"           
            "## Available Tools\n"
            "1. **think_and_repeat_output**: Your primary tool for task management. Use this for ANY request "
            "involving tasks (viewing, creating, updating, or deleting). This tool accesses the user's personal "
            "task database and returns the results. Call this ONCE per unique user input, then generate your audio response.\n"
            "2. **google_search**: Use for general knowledge questions or information that can be found online "
            "(news, facts, how-to information). Do NOT use this for personal information or tasks.\n"
            "3. **end_conversation**: Use when the user wants to end the call (says goodbye, wants to hang up, etc.).\n"
            "\n\n"
            "## Important Behavior Guidelines\n"
            "- When you call the think_and_repeat_output tool, you MUST provide a brief spoken acknowledgment "
            "to the user while waiting, such as 'Let me check that for you', 'One moment', or 'Looking that up now'.\n"
            "- NEVER say you cannot do something before calling the think tool. The tool has access to the user's "
            "personal data and will provide the information you need.\n"
            "- NEVER claim you don't have access to calendars, tasks, or personal information. You DO have access "
            "through the think tool.\n"
            "- CRITICAL ANTI-HALLUCINATION RULE: After receiving results from ANY TOOL, you MUST base your response " 
            "EXCLUSIVELY and STRICTLY on the information provided in the tool's response. You are FORBIDDEN from making up, " 
            "inventing, adding, or mentioning any tasks, events, meetings, deadlines, or any other information that is NOT explicitly " 
            "mentioned in the tool response. If the tool response contains task data, you MUST use ONLY that exact data - " 
            "do NOT add, infer, or create additional tasks or details that weren't in the response.\n"
            "- MANDATORY FUNCTION RESPONSE USAGE: When the function response contains a list of tasks (e.g., 'Tomorrow, you have the following tasks: 1. Eat breakfast... 2. Pack my lunch...'), " 
            "you MUST repeat that EXACT information in your audio response. You MUST NOT say 'I don't have any tasks', 'no tasks', 'unable to get tasks', or any variation that contradicts the function response. " 
            "The function response is the AUTHORITATIVE source - if it says there are tasks, there ARE tasks. If it says there are no tasks, then say there are no tasks. " 
            "NEVER contradict the function response.\n"
            "- ZERO TOLERANCE FOR HALLUCINATION: If the tool response says 'take my medicine' and 'brush my teeth', " 
            "you MUST ONLY mention those exact tasks. You MUST NOT mention ANY other tasks that are not in the tool response. " 
            "You MUST NOT say there are no tasks if the tool response lists tasks. This is a critical error that " 
            "must be avoided at all costs.\n"
            "- Provide a natural, conversational response using EXCLUSIVELY the information from the tool response. " 
            "If the tool response lists specific tasks, repeat ONLY those tasks. Do not add examples, suggestions, " 
            "or any other tasks that weren't explicitly returned by the tool. If the tool response says 'you have X tasks', " 
            "you MUST say the user has those tasks - do NOT contradict the tool response.\n"
        ),
        tools=[{"google_search": {}}, think_tool, end_conversation_tool],
    )

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    print(f"✅ Client connected with user_id: {user_id}")
    print("🚫 Server WS pings DISABLED")

    # Initialize scratchpad to track session inputs and responses
    scratchpad = []
    
    # Buffers for accumulating audio transcription chunks
    audio_buffers = {
        "user": "",
        "agent": ""
    }
    # Track user inputs that have already been processed by the think tool to avoid loops
    processed_tool_inputs = set()

    def _normalize_text(text: str) -> str:
        """Lowercase and collapse whitespace for stable comparisons."""
        if not isinstance(text, str):
            return ""
        return " ".join(text.lower().strip().split())

    def commit_audio_buffer(source):
        """Commit buffered audio transcription to scratchpad if it has content."""
        if audio_buffers[source]:
            add_to_scratchpad(source=source, format="audio", content=audio_buffers[source].strip())
            audio_buffers[source] = ""

    def add_to_scratchpad(source, format, content=None, name=None, args=None, response=None, call_id=None):
        """Helper method to add entries to the scratchpad with standardized format.
        
        Args:
            source: "user" or "agent"
            format: "text", "audio", or "function_call"
            content: Text or audio content (for text/audio formats)
            name: Function name (for function_call format)
            args: Function arguments (for function_call format - call)
            response: Function response (for function_call format - response)
            call_id: Function call ID (for function_call format)
        """
        entry = {
            "source": source,
            "format": format
        }
        
        if format in ["text", "audio"]:
            if content:
                entry["content"] = content
            # For non-audio formats or when committing audio, commit any pending audio buffers
            if format != "audio":
                # Commit any pending audio buffers when a different format is added
                if audio_buffers["user"]:
                    commit_audio_buffer("user")
                if audio_buffers["agent"]:
                    commit_audio_buffer("agent")
        elif format == "function_call":
            if name:
                entry["name"] = name
            if call_id:
                entry["call_id"] = call_id
            if args is not None:
                entry["args"] = args
            if response is not None:
                entry["response"] = response
        
        scratchpad.append(entry)

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
        #print("🗣️ Gemini talking")
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
        
        #print("🔇 Audio playback finished")

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

        # Get user's profile for the system prompt
        user_info = get_user_by_id(user_id)
        print(f"👤 User info: {user_info}")
        
        # Create config with user's info (name, timezone, etc.)
        # Extract user info with defaults
        first_name = user_info.get("first_name", "") if user_info else ""
        last_name = user_info.get("last_name", "") if user_info else ""
        timezone = user_info.get("timezone", "UTC") if user_info else "UTC"
        username = user_info.get("username", "") if user_info else ""
        
        # Build user context string
        user_name = f"{first_name} {last_name}".strip() if first_name or last_name else "the user"
        
        # Get current time in user's timezone
        try:
            user_tz = ZoneInfo(timezone)
            current_time = datetime.now(user_tz)
            current_time_str = current_time.strftime(f"%A, %B %d, %Y at %I:%M %p ({timezone})")
            current_date_str = current_time.strftime("%A, %B %d, %Y")
        except Exception:
            # Fallback to UTC if timezone is invalid
            current_time = datetime.now(UTC)
            current_time_str = current_time.strftime("%A, %B %d, %Y at %I:%M %p (UTC)")
            current_date_str = current_time.strftime("%A, %B %d, %Y")
        
        # Create config data structure
        user_config: UserConfigData = {
            "user_info": user_info,
            "user_name": user_name,
            "current_time_str": current_time_str,
            "current_date_str": current_date_str,
            "timezone": timezone
        }
        
        config = get_live_config(user_config)
        print(f"🔄 User config: {user_config}")
        
        # Keep the Gemini session alive for the entire WebSocket connection
        async with client.aio.live.connect(model=MODEL, config=config) as gemini_session:

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
                            # Commit any pending audio buffers before adding text input
                            commit_audio_buffer("user")
                            commit_audio_buffer("agent")
                            
                            # Extract text for scratchpad
                            if isinstance(text_content, str):
                                input_text = text_content
                            elif isinstance(text_content, list):
                                # For multiple turns, get the last user message
                                input_text = text_content[-1].get('parts', [{}])[0].get('text', '') if text_content else ''
                            else:
                                input_text = text_content.get('parts', [{}])[0].get('text', '')
                            
                            # Add to scratchpad
                            if input_text:
                                add_to_scratchpad(source="user", format="text", content=input_text)
                            
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
                
                # Flag to track if we should close after receiving the goodbye audio
                should_close_after_audio = False
                last_audio_received_time = None
                
                try:
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
                                # Commit any pending audio buffers before handling function calls
                                commit_audio_buffer("user")
                                commit_audio_buffer("agent")
                                
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
                                        try:
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
                                                    result = generalThinkingAgent.think(user_input, scratchpad, user_config)
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
                                        except Exception as e:
                                            print(f"❌ Error in think_and_repeat_output: {e}")
                                            traceback.print_exc()

                                    # Handle end conversation function
                                    elif name == "end_conversation":
                                        try:
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
                                        except Exception as e:
                                            print(f"❌ Error in end_conversation: {e}")
                                            traceback.print_exc()
                                            # Still try to close the connection
                                            try:
                                                await websocket.close()
                                            except Exception:
                                                pass
                                            return


                                # Send function responses back to Gemini (only if we have actual responses)
                                if function_responses:
                                    print(f"Sending function responses: {function_responses}")
                                    print(f"function_responses: {function_responses[0]['response']}")
                                    
                                    # Add function responses to scratchpad
                                    for func_response in function_responses:
                                        add_to_scratchpad(
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
                                await interrupt()  # Call the interrupt function like in working example
                                print("🔇 Audio playback interrupted and cleared")
                                break
                               
                            # Forward audio parts immediately (streaming) - identical to working example
                            if server_content and server_content.model_turn:
                                for part in server_content.model_turn.parts:
                                    if part.inline_data:
                                        # Use the add_audio function like in working example
                                        add_audio(part.inline_data.data)
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
                                    while (audio_playback_queue or (playback_task and not playback_task.done())):
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

                            # Handle transcriptions (identical to working example)
                            output_transcription = getattr(response.server_content, "output_transcription", None)
                            if output_transcription and output_transcription.text:
                                # Commit user audio buffer when agent starts responding
                                commit_audio_buffer("user")
                                
                                output_text = output_transcription.text.strip()
                                output_transcriptions.append(output_text)
                                recent_outputs.append(output_text.lower())  # Store lowercase for comparison
                                
                                # Buffer audio transcription chunks instead of adding immediately
                                if audio_buffers["agent"]:
                                    audio_buffers["agent"] += " " + output_text
                                else:
                                    audio_buffers["agent"] = output_text
                                
                                await websocket.send_text(json.dumps({"output_text": output_text}))

                            input_transcription = getattr(response.server_content, "input_transcription", None)
                            if input_transcription and input_transcription.text:
                                # Commit agent audio buffer when user starts speaking
                                commit_audio_buffer("agent")
                                
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
                                
                                # Buffer audio transcription chunks instead of adding immediately
                                if audio_buffers["user"]:
                                    audio_buffers["user"] += " " + input_text
                                else:
                                    audio_buffers["user"] = input_text
                                
                                await websocket.send_text(json.dumps({"input_text": input_text}))
                except Exception as e:
                    # Handle websocket closure and other errors gracefully
                    error_str = str(e)
                    if "ConnectionClosed" in error_str or "1011" in error_str or "closed" in error_str.lower():
                        print(f"🔄 Gemini connection closed: {e}")
                        # Don't re-raise - let the outer handler deal with it
                        # The TaskGroup will propagate this appropriately
                    else:
                        print(f"Error in receive_and_play: {e}")
                        traceback.print_exc()
                        raise
                except Exception as e:
                    # Handle websocket closure and other errors gracefully
                    error_str = str(e)
                    if "ConnectionClosed" in error_str or "1011" in error_str or "closed" in error_str.lower():
                        print(f"🔄 Gemini connection closed: {e}")
                        # Don't re-raise - let the outer handler deal with it
                        # The TaskGroup will propagate this appropriately
                    else:
                        print(f"Error in receive_and_play: {e}")
                        traceback.print_exc()
                        raise

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
        # Commit any pending audio buffers before closing
        commit_audio_buffer("user")
        commit_audio_buffer("agent")
        print(f"Scratchpad: {scratchpad}")
        # Update the session status to inactive
        update_session_status(user_id, False)
    except Exception as e:
        print(f"Error in websocket endpoint: {e}")
        # Commit any pending audio buffers before closing
        commit_audio_buffer("user")
        commit_audio_buffer("agent")
        print(f"Scratchpad: {scratchpad}")
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