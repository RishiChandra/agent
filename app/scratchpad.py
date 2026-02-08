from typing import Optional, List, Dict, Any


class Scratchpad:
    """Manages conversation scratchpad entries and audio transcription buffers."""
    
    def __init__(self):
        """Initialize an empty scratchpad with audio buffers."""
        self.entries: List[Dict[str, Any]] = []
        self.audio_buffers = {
            "user": "",
            "agent": ""
        }
    
    def add_entry(
        self,
        source: str,
        format: str,
        content: Optional[str] = None,
        name: Optional[str] = None,
        args: Optional[Dict] = None,
        response: Optional[Dict] = None,
        call_id: Optional[str] = None
    ) -> None:
        """Add an entry to the scratchpad with standardized format.
        
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
                if self.audio_buffers["user"]:
                    self.commit_audio_buffer("user")
                if self.audio_buffers["agent"]:
                    self.commit_audio_buffer("agent")
        elif format == "function_call":
            if name:
                entry["name"] = name
            if call_id:
                entry["call_id"] = call_id
            if args is not None:
                entry["args"] = args
            if response is not None:
                entry["response"] = response
        
        self.entries.append(entry)
    
    def commit_audio_buffer(self, source: str) -> None:
        """Commit buffered audio transcription to scratchpad if it has content.
        
        Args:
            source: "user" or "agent"
        """
        if self.audio_buffers[source]:
            self.add_entry(
                source=source,
                format="audio",
                content=self.audio_buffers[source].strip()
            )
            self.audio_buffers[source] = ""
    
    def buffer_audio_transcription(self, source: str, text: str) -> None:
        """Add audio transcription text to the buffer for the given source.
        
        Args:
            source: "user" or "agent"
            text: The transcription text to buffer
        """
        if self.audio_buffers[source]:
            self.audio_buffers[source] += " " + text
        else:
            self.audio_buffers[source] = text
    
    def get_entries(self) -> List[Dict[str, Any]]:
        """Get all scratchpad entries.
        
        Returns:
            List of scratchpad entry dictionaries
        """
        return self.entries
    
    def __repr__(self) -> str:
        """String representation of the scratchpad."""
        return f"Scratchpad(entries={len(self.entries)})"
