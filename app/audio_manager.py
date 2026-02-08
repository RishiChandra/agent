import json
import base64
import asyncio
from collections import deque
from fastapi import WebSocket


class AudioManager:
    """Manages audio queues and state for the websocket connection."""
    
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        # Input audio queue: client → Gemini (for user speech)
        self.audio_queue = asyncio.Queue()
        # Output audio queue: Gemini → client (for agent speech)
        self.audio_playback_queue = deque()
        self.playback_task = None
    
    def add_audio(self, audio_data):
        """Add audio data to the playback queue."""
        self.audio_playback_queue.append(audio_data)
        
        if self.playback_task is None or self.playback_task.done():
            self.playback_task = asyncio.create_task(self._play_audio())
    
    async def _play_audio(self):
        """Play all queued audio data."""
        while self.audio_playback_queue:
            try:
                # Check if we've been interrupted
                if self.playback_task is None or self.playback_task.done():
                    break
                    
                audio_data = self.audio_playback_queue.popleft()
                # Send audio to client
                await self.websocket.send_text(json.dumps({
                    "audio": base64.b64encode(audio_data).decode("utf-8")
                }))
            except Exception as e:
                print(f"Error playing audio: {e}")
                break
    
    async def interrupt(self):
        """Handle interruption by stopping playback and clearing queue."""
        print("🛑 Interrupting audio playback...")
        
        # Clear the audio queue immediately
        self.audio_playback_queue.clear()
        
        # Cancel the playback task if it's running
        if self.playback_task and not self.playback_task.done():
            self.playback_task.cancel()
            try:
                await self.playback_task
            except asyncio.CancelledError:
                pass
        
        # Reset playback task to None so a new one can be created
        self.playback_task = None
        
        # Send interrupt signal to client
        try:
            await self.websocket.send_text(json.dumps({"interrupt": True}))
        except Exception as e:
            print(f"Error sending interrupt signal: {e}")
        
        print("✅ Audio playback interrupted and cleared")
    
    def is_playing(self):
        """Check if audio is currently playing or queued."""
        return bool(self.audio_playback_queue) or (self.playback_task and not self.playback_task.done())
