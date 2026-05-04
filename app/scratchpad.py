import time
from typing import Optional, List, Dict, Any


class Scratchpad:
    """Manages conversation scratchpad entries and audio transcription buffers."""

    #: Agent audio committed while Live ``think_and_repeat_output`` is being handled (pre-tool).
    SPEECH_PHASE_PRE_TOOL_ACK = "interstitial_ack"
    
    def __init__(self):
        """Initialize an empty scratchpad with audio buffers."""
        self.entries: List[Dict[str, Any]] = []
        self.audio_buffers = {
            "user": "",
            "agent": ""
        }
        self._start_time: float = time.monotonic()
        self._last_entry_time: float = self._start_time
        self._interstitial_ack_window: bool = False
    
    def begin_interstitial_ack_window(self) -> None:
        """Start tagging the next agent audio commit(s) as pre-tool / interstitial ack.

        Call immediately before flushing audio buffers on a ``think_and_repeat_output`` tool turn.
        """
        self._interstitial_ack_window = True

    def end_interstitial_ack_window(self) -> None:
        """Stop tagging agent commits as interstitial (always call in ``finally`` after tool handling)."""
        self._interstitial_ack_window = False

    def tag_pre_tool_agent_ack_after_last_user(self) -> None:
        """Tag agent text/audio after the latest user row as pre-tool ack.

        Output transcription usually buffers the Live ack, then **input** transcription commits
        the agent buffer (see ``TranscriptionHandler``) **before** the ``think_and_repeat_output``
        tool message is handled, so ``commit_audio_buffer`` misses ``_interstitial_ack_window``.
        Call this once per think turn after the usual pre-tool buffer flushes.
        """
        last_user = -1
        for i, e in enumerate(self.entries):
            if e.get("source") == "user" and e.get("format") in ("text", "audio") and e.get("content"):
                last_user = i
        if last_user < 0:
            return
        for j in range(last_user + 1, len(self.entries)):
            e = self.entries[j]
            if e.get("format") == "function_call" and e.get("source") == "agent":
                break
            if (
                e.get("source") == "agent"
                and e.get("format") in ("text", "audio")
                and e.get("content")
                and not e.get("speech_phase")
            ):
                e["speech_phase"] = self.SPEECH_PHASE_PRE_TOOL_ACK
    
    def add_entry(
        self,
        source: str,
        format: str,
        content: Optional[str] = None,
        name: Optional[str] = None,
        args: Optional[Dict] = None,
        response: Optional[Dict] = None,
        call_id: Optional[str] = None,
        speech_phase: Optional[str] = None,
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
            speech_phase: Optional tag on text/audio rows, e.g. ``interstitial_ack`` for pre-tool agent speech.
        """
        entry = {
            "source": source,
            "format": format
        }
        
        if format in ["text", "audio"]:
            if content:
                entry["content"] = content
            if speech_phase:
                entry["speech_phase"] = speech_phase
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
            now = time.monotonic()
            entry["elapsed_s"] = round(now - self._last_entry_time, 2)
            self._last_entry_time = now
        
        self.entries.append(entry)
    
    def commit_audio_buffer(self, source: str) -> None:
        """Commit buffered audio transcription to scratchpad if it has content.
        
        Args:
            source: "user" or "agent"
        """
        if self.audio_buffers[source]:
            text = self.audio_buffers[source].strip()
            phase: Optional[str] = None
            if source == "agent" and self._interstitial_ack_window:
                phase = self.SPEECH_PHASE_PRE_TOOL_ACK
            self.add_entry(
                source=source,
                format="audio",
                content=text,
                speech_phase=phase,
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
    
    def clear(self) -> None:
        """Clear all scratchpad entries and audio buffers."""
        self.entries = []
        self.audio_buffers = {
            "user": "",
            "agent": ""
        }
        self._start_time = time.monotonic()
        self._last_entry_time = self._start_time
        self._interstitial_ack_window = False
    
    def __repr__(self) -> str:
        """String representation of the scratchpad."""
        return f"Scratchpad(entries={len(self.entries)})"
