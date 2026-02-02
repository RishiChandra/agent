import os
from typing import Optional, TypedDict
from google import genai
from google.genai.types import (
    LiveConnectConfig,
    SpeechConfig,
    VoiceConfig,
    PrebuiltVoiceConfig,
    FunctionDeclaration,
    Tool,
)

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
            "- CRITICAL: If you receive a message that says 'Tell the user that it is time for them to complete this task now' "
            "followed by task JSON data (with task_id, task_info, etc.), this is a TASK REMINDER. "
            "Do NOT call think_and_repeat_output for task reminders. Instead, immediately speak the reminder to the user "
            "in a natural, friendly way (e.g., 'It's time for you to take your medicine' or 'Hey, just a reminder to complete your task: [task description]'). "
            "The task reminder message is an instruction for you to speak, not user input that needs processing.\n"
            "- When you call the think_and_repeat_output tool, you MUST provide a brief spoken acknowledgment "
            "to the user while waiting, such as 'Let me check that for you', 'One moment', or 'Looking that up now'.\n"
            "- CRITICAL EXCEPTION: If you just reminded the user about a task and they respond with ONLY 'thanks' or 'okay' "
            "without clearly indicating they completed the task, do NOT call think_and_repeat_output. "
            "Instead, directly respond asking for clarification (e.g., 'Did you complete the task?' or 'Have you finished taking your medicine?'). "
            "This provides faster responses and better user experience. Only call think_and_repeat_output if they clearly indicate completion "
            "(e.g., 'thanks, just took my medicine', 'done', 'completed', 'I finished it', 'I took my medicine', etc.).\n"
            "- IMPORTANT: If the user wants to defer the task (e.g., 'I'll do it later', 'not yet', 'I need more time', 'I haven't finished', "
            "'I'm not done yet', 'remind me later', etc.), you MUST call think_and_repeat_output so the system can defer the task by 5 minutes.\n"
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
