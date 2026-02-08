import json
from collections import deque
from fastapi import WebSocket


class TranscriptionHandler:
    """Handles input and output transcriptions from Gemini, including echo filtering."""
    
    def __init__(self, scratchpad, websocket: WebSocket):
        """Initialize the transcription handler.
        
        Args:
            scratchpad: The scratchpad instance for storing transcriptions
            websocket: The WebSocket connection to send transcriptions to the client
        """
        self.scratchpad = scratchpad
        self.websocket = websocket
        # Track recent output transcriptions to filter out echo/feedback
        self.recent_outputs = deque(maxlen=10)  # Keep last 10 output transcriptions
    
    async def handle_output_transcription(self, output_transcription) -> None:
        """Handle output transcription (agent speech).
        
        Args:
            output_transcription: The output transcription from Gemini response
        """
        if not output_transcription or not output_transcription.text:
            return
        
        # Commit user audio buffer when agent starts responding
        self.scratchpad.commit_audio_buffer("user")
        
        output_text = output_transcription.text.strip()
        self.recent_outputs.append(output_text.lower())  # Store lowercase for comparison
        
        # Buffer audio transcription chunks instead of adding immediately
        self.scratchpad.buffer_audio_transcription("agent", output_text)
        
        # Send to client for display
        await self.websocket.send_text(json.dumps({"output_text": output_text}))
    
    async def handle_input_transcription(self, input_transcription) -> bool:
        """Handle input transcription (user speech) with echo filtering.
        
        Args:
            input_transcription: The input transcription from Gemini response
            
        Returns:
            True if transcription was processed, False if it was filtered as echo
        """
        if not input_transcription or not input_transcription.text:
            return False
        
        # Commit agent audio buffer when user starts speaking
        self.scratchpad.commit_audio_buffer("agent")
        
        input_text = input_transcription.text.strip()
        
        # Filter out input transcriptions that match recent output (prevent echo/feedback)
        if self._is_echo(input_text):
            print(f"🚫 Filtered echo input transcription: '{input_text}' (matches recent output)")
            return False
        
        # Buffer audio transcription chunks instead of adding immediately
        self.scratchpad.buffer_audio_transcription("user", input_text)
        
        # Send to client for display
        await self.websocket.send_text(json.dumps({"input_text": input_text}))
        return True
    
    def _is_echo(self, input_text: str) -> bool:
        """Check if input text matches recent output (echo/feedback detection).
        
        Args:
            input_text: The input transcription text to check
            
        Returns:
            True if the input appears to be an echo of recent output
        """
        input_lower = input_text.lower()
        isEcho = False
        
        # Check if input matches any recent output (exact, substring, or significant word overlap)
        for recent_output in self.recent_outputs:
            # Check for exact match or substring match
            if input_lower == recent_output or input_lower in recent_output or recent_output in input_lower:
                isEcho = True
                break
            
            # Check for significant word overlap (more than 50% of words match)
            input_words = set(input_lower.split())
            output_words = set(recent_output.split())
            if len(input_words) > 0 and len(output_words) > 0:
                overlap = len(input_words & output_words) / max(len(input_words), len(output_words))
                if overlap > 0.5:
                    isEcho = True
                    break
        if isEcho:
            print(f"🚫 Filtered echo input transcription: '{input_text}' (matches recent output)")
        
        return isEcho
