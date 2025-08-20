# gemini_live_stream.py
# Mic -> Gemini Live API (WebSocket) -> Speaker (real-time)

import os, asyncio, json, base64, signal
import numpy as np
import sounddevice as sd
import websockets
from collections import deque
from dotenv import load_dotenv

load_dotenv()

# ===== Config =====

API_KEY = os.environ.get("GOOGLE_API_KEY")  # set this before running
# Use the Live API WS endpoint (BidiGenerateContent):
WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    f"?key={API_KEY}"
)
# A native-audio dialog model; adjust if needed
MODEL = "models/gemini-2.0-flash-live-001"
# models/gemini-2.0-flash-live-001
# models/gemini-2.5-flash-preview-native-audio-dialog
tools = [{'google_search': {}}]

# # gemini-2.5-flash-exp-native-audio-thinking-dialog
VOICE = "Aoede"      # try "Kore", "Puck", …
INPUT_SR  = 16000    # mic capture rate (PCM16 mono)
OUTPUT_SR = 24000    # Gemini audio out rate (PCM16 mono)
FRAME_MS  = 50       # ~50ms blocks
IN_BLOCK  = int(INPUT_SR  * FRAME_MS / 1000)
OUT_BLOCK = int(OUTPUT_SR * FRAME_MS / 1000)

# Simple buffer to smooth out stuttering without adding latency
AUDIO_BUFFER_SIZE = 2  # Small buffer to smooth playback

# ===== Helpers =====
def pcm16_b64_from_float32(x: np.ndarray) -> str:
    # float32 [-1,1] -> int16 LE -> base64
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    return base64.b64encode(pcm).decode("utf-8")

def b64_to_int16_bytes(b64: str) -> bytes:
    return base64.b64decode(b64)

def make_setup():
    # IMPORTANT: systemInstruction must be a Content object (parts[].text)
    return {
        "setup": {
            "model": MODEL,
            "tools": tools,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}
                },
            },
            # Enable live transcripts (optional)
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "systemInstruction": {
                "parts": [{"text": "You are a helpful assistant. Be concise and respond naturally in conversation. Only respond in complete sentences. Your speech is limited to 10 seconds."}]
            },
        }
    }

async def run():
    if not API_KEY:
        raise SystemExit("Set GOOGLE_API_KEY in your environment.")

    # Queues
    mic_q: asyncio.Queue[str]   = asyncio.Queue(maxsize=16)   # JSON strings for WS
    spk_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=128)  # raw int16 frames
    stop = asyncio.Event()

    try:
        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
            print("✅ Connected. Sending setup…")
            await ws.send(json.dumps(make_setup()))

            # ---- Wait for setupComplete before streaming audio ----
            while True:
                raw = await ws.recv()
                try:
                    msg = json.loads(raw)
                    print(f"Setup response: {json.dumps(msg, indent=2)}")
                except Exception as e:
                    print(f"Failed to parse setup response: {e}")
                    continue
                
                if "setupComplete" in msg:
                    print("✅ Setup complete. Streaming…")
                    break

                # If server sends early content, handle it too:
                server = msg.get("serverContent")
                if server:
                    mt = server.get("modelTurn")
                    if mt:
                        for part in mt.get("parts", []):
                            inline = part.get("inlineData")
                            if inline and "data" in inline:
                                try:
                                    spk_q.put_nowait(b64_to_int16_bytes(inline["data"]))
                                except asyncio.QueueFull:
                                    pass

            # Send a test message to trigger Gemini's response
            print("🧪 Sending test message to trigger response...")
            test_msg = {
                "realtimeInput": {
                    "text": "Hello, can you hear me?"
                }
            }
            await ws.send(json.dumps(test_msg))
            print("✅ Test message sent")

            # ---- Mic capture callback -> enqueue realtimeInput JSON ----
            def mic_callback(indata, frames, t, status):
                if status:
                    print(f"Audio input status: {status}")
                try:
                    b64 = pcm16_b64_from_float32(indata[:, 0])
                    msg = {
                        "realtimeInput": {
                            "audio": {
                                "data": b64,
                                "mimeType": f"audio/pcm;rate={INPUT_SR}",
                            }
                        }
                    }
                    mic_q.put_nowait(json.dumps(msg))
                except asyncio.QueueFull:
                    pass

            async def mic_sender():
                try:
                    with sd.InputStream(channels=1, samplerate=INPUT_SR,
                                        blocksize=IN_BLOCK, dtype="float32",
                                        callback=mic_callback):
                        print("🎤 Microphone active - start speaking!")
                        while not stop.is_set():
                            try:
                                payload = await asyncio.wait_for(mic_q.get(), timeout=0.1)
                                await ws.send(payload)
                            except asyncio.TimeoutError:
                                continue
                            except Exception as e:
                                print(f"Error sending audio: {e}")
                                break
                except Exception as e:
                    print(f"Microphone error: {e}")

            async def speaker_player():
                try:
                    with sd.RawOutputStream(channels=1, samplerate=OUTPUT_SR,
                                            blocksize=OUT_BLOCK, dtype="int16") as spk:
                        print("🔊 Speaker active")
                        audio_count = 0
                        audio_buffer = deque(maxlen=AUDIO_BUFFER_SIZE)
                        
                        while not stop.is_set():
                            try:
                                data = await asyncio.wait_for(spk_q.get(), timeout=0.1)
                                if data and len(data) > 0:
                                    audio_buffer.append(data)
                                    audio_count += 1
                                    
                                    # Play immediately if buffer is ready, otherwise keep buffering
                                    if len(audio_buffer) >= AUDIO_BUFFER_SIZE:
                                        # Play the oldest frame and remove it
                                        frame_to_play = audio_buffer.popleft()
                                        spk.write(frame_to_play)
                                        print(f"🔊 Playing audio frame #{audio_count}, {len(frame_to_play)} bytes, buffer: {len(audio_buffer)}")
                                    else:
                                        print(f"⏳ Buffering audio frame #{audio_count}, buffer: {len(audio_buffer)}/{AUDIO_BUFFER_SIZE}")
                                else:
                                    print("⚠️ Empty audio data received")
                            except asyncio.TimeoutError:
                                continue
                            except Exception as e:
                                print(f"Speaker error: {e}")
                                break
                except Exception as e:
                    print(f"Speaker error: {e}")

            async def recv_loop():
                try:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception as e:
                            print(f"Failed to parse message: {e}")
                            continue

                        # Debug: print the message structure
                     #   print(f"Received message: {json.dumps(msg, indent=2)}")

                        server = msg.get("serverContent")
                        if not server:
                            # other messages (toolCall, sessionResumptionUpdate, etc.)
                            continue

                        # Optional live transcripts
                        it = server.get("inputTranscription")
                        if it and "text" in it:
                            print(f"🎙️ You: {it['text']}")
                        
                        ot = server.get("outputTranscription")
                        if ot and "text" in ot:
                            print(f"🤖 Gemini: {ot['text']}")

                        # Audio frames - check multiple possible locations
                        mt = server.get("modelTurn")
                        if mt:
                        #    print(f"Model turn received: {json.dumps(mt, indent=2)}")
                            for part in mt.get("parts", []):
                                inline = part.get("inlineData")
                                if inline and "data" in inline:
                                    print(f"Audio data found, length: {len(inline['data'])}")
                                    try:
                                        audio_bytes = b64_to_int16_bytes(inline["data"])
                                        print(f"Decoded audio bytes: {len(audio_bytes)} bytes")
                                        spk_q.put_nowait(audio_bytes)
                                    except asyncio.QueueFull:
                                        print("Speaker queue full, dropping audio frame")
                                        pass
                                    except Exception as e:
                                        print(f"Error processing audio data: {e}")
                        
                        # Also check for direct audio in the message
                        if "audio" in msg:
                            print(f"Direct audio message: {json.dumps(msg['audio'], indent=2)}")
                            audio_data = msg["audio"].get("data")
                            if audio_data:
                                try:
                                    audio_bytes = b64_to_int16_bytes(audio_data)
                                    spk_q.put_nowait(audio_bytes)
                                except Exception as e:
                                    print(f"Error processing direct audio: {e}")
                except Exception as e:
                    print(f"Receive loop error: {e}")
                    import traceback
                    traceback.print_exc()

            # Graceful shutdown (Ctrl+C)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop.set)
                except NotImplementedError:
                    pass

            print("🚀 Starting audio streams...")
            await asyncio.gather(mic_sender(), speaker_player(), recv_loop())
            
    except websockets.exceptions.ConnectionClosed as e:
        print(f"WebSocket connection closed: {e}")
    except Exception as e:
        print(f"Connection error: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
    except Exception as e:
        print(f"Fatal error: {e}")
