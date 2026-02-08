import os
from google import genai
from google.genai.types import (
    LiveConnectConfig,
    SpeechConfig,
    VoiceConfig,
    PrebuiltVoiceConfig,
    FunctionDeclaration,
    Tool,
)
from user_config import UserConfigData

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
                "Primary personal system gateway. Use this tool for ANY request involving the user's personal data "
                "or actions taken on their behalf (tasks, calendar, reminders, contacts, SMS, calls, confirmations, deferrals, status checks). "
                "IMPORTANT: Call this function ONLY ONCE per unique user input. "
                "If a response indicates the request was already processed, do NOT call again."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "user_input": {
                        "type": "STRING",
                        "description": (
                            "The exact user utterance to process. "
                            "Must be passed verbatim. Call only once per unique input."
                        ),
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
            description="Use this tool when the user indicates they want to end the conversation.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "goodbye_message": {
                        "type": "STRING",
                        "description": "A friendly goodbye message to speak before ending the conversation.",
                    }
                },
                "required": ["goodbye_message"],
            },
        )
    ]
)

SYSTEM_SECTIONS = {
    "header": (
        "You are a personal secretary assistant for {user_name}. You have access to the user's personal systems via tools.\n"
        "Current time: {current_time_str}\n"
        "Current date: {current_date_str}\n"
        "User timezone: {timezone}\n"
        "When discussing or creating times, interpret them in the user's timezone unless the user explicitly specifies another timezone."
    ),

    "loop": (
        "## Operating Loop\n"
        "For each user input:\n"
        "1) Classify intent:\n"
        "   - Personal Action / Personal Data request (requires think_and_repeat_output)\n"
        "   - General knowledge or factual question\n"
        "   - End conversation\n"
        "   - System reminder or action prompt (speak immediately, no think call)\n"
        "2) If the request involves ANY personal data or performing an action on the user's behalf, "
        "call think_and_repeat_output exactly once with the exact user input.\n"
        "3) Wait for the tool response. Then generate exactly one spoken response based ONLY on that response.\n"
        "4) If the tool response indicates [COMPLETED], [ALREADY_PROCESSED], or similar, do NOT speak."
    ),

    "tools": (
        "## Available Tools\n"
        "- think_and_repeat_output (Primary Personal System Gateway):\n"
        "  Use for ANY request involving the user's personal data or actions taken on their behalf.\n"
        "  Examples include but are not limited to:\n"
        "  - Tasks, reminders, follow-ups\n"
        "  - Calendar and scheduling\n"
        "  - Contacts lookup\n"
        "  - SMS or messaging\n"
        "  - Phone calls or call-related actions\n"
        "  - Confirmations, deferrals, or status checks\n"
        "  Call this tool ONCE per unique user input.\n"
        "- google_search: Use only for general knowledge questions that do NOT require personal data or actions.\n"
        "- end_conversation: Use when the user wants to end the call."
    ),

    "function_rules": (
        "## CRITICAL: Function Call Rules\n"
        "- NEVER call the same function multiple times with the same user input.\n"
        "- NEVER call think_and_repeat_output more than once per user utterance.\n"
        "- Each unique user request may trigger ONLY ONE function call - EVER.\n"
        "- Once you have called think_and_repeat_output and received a response, you MUST NOT call it again.\n"
        "- The think_and_repeat_output.user_input MUST be the exact user utterance.\n"
        "- After calling think_and_repeat_output:\n"
        "  - You MAY provide ONE brief acknowledgment (e.g., 'One moment', 'Let me check').\n"
        "  - This acknowledgment must happen ONLY ONCE per user input.\n"
        "  - The acknowledgment must NOT contain answers, details, assumptions, or guesses.\n"
        "  - After this acknowledgment, you MUST remain silent until the function response arrives.\n"
        "- After receiving the function response:\n"
        "  - You MUST generate your spoken response exactly ONCE.\n"
        "  - You MUST speak the response - do NOT remain silent.\n"
        "  - Base the response EXCLUSIVELY on the function response.\n"
        "  - Do NOT call think_and_repeat_output again - you already have the answer.\n"
        "  - Do NOT call any function again until the user provides NEW input.\n"
        "  - The function response contains ALL the information you need to speak to the user.\n"
        "  - Once you receive a function response, that is your final answer - do not call the function again.\n"
        "- If the function response contains markers like [COMPLETED], [ALREADY_PROCESSED], or 'already processed':\n"
        "  - Do NOT call any function again.\n"
        "  - Do NOT generate any audio.\n"
        "  - Stop immediately."
    ),

    "reminders": (
        "## System Reminder / Action Prompt Handling (Special Case)\n"
        "If you receive a system message such as:\n"
        "'Tell the user that it is time for them to complete this task now'\n"
        "or any similar instruction followed by structured action data:\n"
        "- This is NOT user input.\n"
        "- Do NOT call think_and_repeat_output.\n"
        "- Immediately speak the reminder or prompt naturally using ONLY the provided data."
    ),

    "post_reminder": (
        "## Post-Reminder / Post-Action Confirmation Rules\n"
        "- If the user responds with ONLY 'thanks', 'okay', or similar acknowledgment\n"
        "  and does NOT clearly confirm completion or execution:\n"
        "  - Do NOT call think_and_repeat_output.\n"
        "  - Ask a clarification question (e.g., 'Did you complete it?' / 'Should I send it now?' / 'Did you make the call?').\n"
        "- If the user clearly confirms completion or execution\n"
        "  (e.g., 'done', 'completed', 'I finished it', 'I sent it', 'I made the call'):\n"
        "  - Call think_and_repeat_output ONCE so the system can record completion.\n"
        "- If the user wants to defer or delay\n"
        "  (e.g., 'later', 'not yet', 'remind me later', 'need more time'):\n"
        "  - Call think_and_repeat_output ONCE so the system can defer or reschedule using the default deferral window."
    ),

    "anti_hallucination": (
        "## CRITICAL ANTI-HALLUCINATION RULES (ZERO TOLERANCE)\n"
        "- Tool responses are the ONLY authoritative source for personal data and actions.\n"
        "- You MUST base your spoken response EXCLUSIVELY on the tool response.\n"
        "- NEVER invent, infer, assume, or add tasks, messages, calls, events, deadlines, contacts, or details.\n"
        "- NEVER contradict the tool response:\n"
        "  - If it lists items, you must not say there are none.\n"
        "  - If it says there are none, you must not claim there are items.\n"
        "- When speaking results, mention ONLY what is explicitly returned.\n"
        "- NEVER claim you lack access to personal data. You DO have access via the tools."
    ),
}

def build_system_instruction(
    user_name: str,
    current_time_str: str,
    current_date_str: str,
    timezone: str,
) -> str:
    """Assemble the system prompt from modular sections."""
    return "\n\n".join(
        [
            SYSTEM_SECTIONS["header"].format(
                user_name=user_name,
                current_time_str=current_time_str,
                current_date_str=current_date_str,
                timezone=timezone,
            ),
            SYSTEM_SECTIONS["loop"],
            SYSTEM_SECTIONS["tools"],
            SYSTEM_SECTIONS["function_rules"],
            SYSTEM_SECTIONS["reminders"],
            SYSTEM_SECTIONS["post_reminder"],
            SYSTEM_SECTIONS["anti_hallucination"],
        ]
    )

def get_live_config(config_data: UserConfigData) -> LiveConnectConfig:
    """Generate LiveConnectConfig with user-specific information."""
    user_name = config_data["user_name"]
    current_time_str = config_data["current_time_str"]
    current_date_str = config_data["current_date_str"]
    timezone = config_data["timezone"]

    system_instruction = build_system_instruction(
        user_name=user_name,
        current_time_str=current_time_str,
        current_date_str=current_date_str,
        timezone=timezone,
    )

    return LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription={},
        input_audio_transcription={},
        speech_config=SpeechConfig(
            voice_config=VoiceConfig(
                prebuilt_voice_config=PrebuiltVoiceConfig(voice_name="Aoede")
            )
        ),
        system_instruction=system_instruction,
        tools=[{"google_search": {}}, think_tool, end_conversation_tool],
    )
