import asyncio
import traceback
import pyaudio
from collections import deque
import random

from google import genai
from google.genai import types
import os
from dotenv import load_dotenv

PROJECT_ID = "ai-pin-465902"
LOCATION = "us-central1"
MODEL = "gemini-2.0-flash-live-001"

load_dotenv()

# ===== Config =====

GEMINI_API_KEY = os.environ.get("GOOGLE_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)

FORMAT = pyaudio.paInt16
RECEIVE_SAMPLE_RATE = 24000
SEND_SAMPLE_RATE = 16000
CHUNK_SIZE = 512
CHANNELS = 1

from google.genai.types import (
    LiveConnectConfig,
    SpeechConfig,
    VoiceConfig,
    PrebuiltVoiceConfig,
    # added to allow for function calling / tooling
    FunctionDeclaration,
    Tool,
)


# Mock function for get_order_status
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


# Mock function for get_memories
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


# Define the tools
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
    # session_resumption=types.SessionResumptionConfig(
    # The handle of the session to resume is passed here,
    # or else None to start a new session.
    # handle="93f6ae1d-2420-40e9-828c-776cf553b7a6"
    # ),
    speech_config=SpeechConfig(
        voice_config=VoiceConfig(
            prebuilt_voice_config=PrebuiltVoiceConfig(voice_name="Puck")
        )
    ),
    system_instruction="You are a helpful customer service assistant for an online store, and you are talking to Jason (User ID: itismejy). You can help customers check the status of their orders. ABSOLUTE REQUIREMENT: You are FORBIDDEN from responding to ANY user input or using ANY tools without FIRST calling get_memories. This is a HARD RULE that cannot be broken. Every single user message, every single tool call, every single response MUST start with get_memories. If you need to check order status, you MUST call get_memories first, then get_order_status. If you need to respond to a greeting, you MUST call get_memories first. There are NO exceptions to this rule. Be courteous, professional, and provide all relevant details about shipping, delivery dates, and current status. You can also tell a story.",
    tools=[memories_tool, order_status_tool],
)


class AudioManager:
    def __init__(self, input_sample_rate=16000, output_sample_rate=24000):
        self.pya = pyaudio.PyAudio()
        self.input_stream = None
        self.output_stream = None
        self.input_sample_rate = input_sample_rate
        self.output_sample_rate = output_sample_rate
        self.audio_queue = deque()
        self.is_playing = False
        self.playback_task = None

    async def initialize(self):
        mic_info = self.pya.get_default_input_device_info()
        print(f"microphone used: {mic_info}")

        self.input_stream = await asyncio.to_thread(
            self.pya.open,
            format=FORMAT,
            channels=CHANNELS,
            rate=self.input_sample_rate,
            input=True,
            input_device_index=mic_info["index"],
            frames_per_buffer=CHUNK_SIZE,
        )

        self.output_stream = await asyncio.to_thread(
            self.pya.open,
            format=FORMAT,
            channels=CHANNELS,
            rate=self.output_sample_rate,
            output=True,
        )
        print("Initialized audio streams")

    def add_audio(self, audio_data):
        """Add audio data to the playback queue"""
        self.audio_queue.append(audio_data)

        if self.playback_task is None or self.playback_task.done():
            self.playback_task = asyncio.create_task(self.play_audio())

    async def play_audio(self):
        """Play all queued audio data"""
        print("🗣️ Gemini talking")
        while self.audio_queue:
            try:
                audio_data = self.audio_queue.popleft()
                await asyncio.to_thread(self.output_stream.write, audio_data)
            except Exception as e:
                print(f"Error playing audio: {e}")

        self.is_playing = False

    def interrupt(self):
        """Handle interruption by stopping playback and clearing queue"""
        self.audio_queue.clear()
        self.is_playing = False

        # Important: Start a clean state for next response
        if self.playback_task and not self.playback_task.done():
            self.playback_task.cancel()


async def audio_loop():
    audio_manager = AudioManager(
        input_sample_rate=SEND_SAMPLE_RATE, output_sample_rate=RECEIVE_SAMPLE_RATE
    )

    await audio_manager.initialize()

    async with (
        client.aio.live.connect(model=MODEL, config=CONFIG) as session,
        asyncio.TaskGroup() as tg,
    ):

        # Queue for user audio chunks to control flow
        audio_queue = asyncio.Queue()

        async def listen_for_audio():
            """Just captures audio and puts it in the queue"""
            while True:
                data = await asyncio.to_thread(
                    audio_manager.input_stream.read,
                    CHUNK_SIZE,
                    exception_on_overflow=False,
                )
                await audio_queue.put(data)

        async def process_and_send_audio():
            """Processes audio from queue and sends to Gemini"""
            while True:
                data = await audio_queue.get()

                # Always send the audio data to Gemini
                await session.send_realtime_input(
                    media={
                        "data": data,
                        "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}",
                    }
                )

                audio_queue.task_done()

        async def receive_and_play():
            while True:

                input_transcriptions = []
                output_transcriptions = []

                async for response in session.receive():

                    # retrieve continously resumable session ID
                    if response.session_resumption_update:
                        update = response.session_resumption_update
                        if update.resumable and update.new_handle:
                            # The handle should be retained and linked to the session.
                            print(f"new SESSION: {update.new_handle}")

                    # Check if the connection will be soon terminated
                    if response.go_away is not None:
                        print(response.go_away.time_left)

                    # Handle tool calls
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

                                    print(
                                        f"📦 Order status function executed for order {order_id}"
                                    )

                                except Exception as e:
                                    print(f"Error executing order status function: {e}")
                                    traceback.print_exc()

                        # Send function responses back to Gemini

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

                    if (
                        hasattr(server_content, "interrupted")
                        and server_content.interrupted
                    ):
                        print(f"🤐 INTERRUPTION DETECTED")
                        audio_manager.interrupt()

                    if server_content and server_content.model_turn:
                        for part in server_content.model_turn.parts:
                            if part.inline_data:
                                audio_manager.add_audio(part.inline_data.data)

                    if server_content and server_content.turn_complete:
                        print("✅ Gemini done talking")

                    output_transcription = getattr(response.server_content, "output_transcription", None)
                    if output_transcription and output_transcription.text:
                        output_transcriptions.append(output_transcription.text)

                    input_transcription = getattr(response.server_content, "input_transcription", None)
                    if input_transcription and input_transcription.text:
                        input_transcriptions.append(input_transcription.text)

                print(f"Output transcription: {''.join(output_transcriptions)}")
                print(f"Input transcription: {''.join(input_transcriptions)}")

        # Start all tasks with proper task creation
        tg.create_task(listen_for_audio())
        tg.create_task(process_and_send_audio())
        tg.create_task(receive_and_play())


if __name__ == "__main__":
    try:
        asyncio.run(audio_loop(), debug=True)
    except KeyboardInterrupt:
        print("Exiting application via KeyboardInterrupt...")
    except Exception as e:
        print(f"Unhandled exception in main: {e}")
        traceback.print_exc()
