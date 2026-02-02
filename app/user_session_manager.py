from datetime import datetime, UTC
from typing import Tuple
from zoneinfo import ZoneInfo
from session_management_utils import get_session, create_session, update_session_status
from database import get_user_by_id
from gemini_config import get_live_config, UserConfigData
from google.genai.types import LiveConnectConfig


def update_user_session_status(user_id: str, is_active: bool) -> None:
    """Helper function to update session status (for use in exception handlers)."""
    update_session_status(user_id, is_active)


class UserSessionManager:
    """Helper class to manage user sessions and configuration for WebSocket connections."""
    
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.db_session = None
        self.user_info = None
        self.user_config = None
        self.live_config = None
    
    def initialize_session(self) -> None:
        """Initialize or retrieve the database session for the user."""
        self.db_session = get_session(self.user_id)
        print(f"🔄 DB SESSION: {self.db_session}")
        
        if not self.db_session:
            self.db_session = create_session(self.user_id)
        else:
            print(f"🔄 SESSION FOUND FOR USER {self.user_id}")
            print(f"🔄 SESSION: {self.db_session}")
            update_session_status(self.user_id, True)
    
    def load_user_info(self) -> None:
        """Load user profile information from the database."""
        self.user_info = get_user_by_id(self.user_id)
        print(f"👤 User info: {self.user_info}")
    
    def _extract_user_name(self) -> str:
        """Extract and format the user's name from user_info."""
        if not self.user_info:
            return "the user"
        
        first_name = self.user_info.get("first_name", "")
        last_name = self.user_info.get("last_name", "")
        user_name = f"{first_name} {last_name}".strip()
        
        return user_name if user_name else "the user"
    
    def _get_timezone(self) -> str:
        """Get the user's timezone, defaulting to UTC if not available."""
        if not self.user_info:
            return "UTC"
        return self.user_info.get("timezone", "UTC")
    
    def _get_current_time_strings(self, timezone: str) -> Tuple[str, str]:
        """Get formatted current time and date strings in the user's timezone.
        
        Args:
            timezone: The user's timezone string
            
        Returns:
            Tuple of (current_time_str, current_date_str)
        """
        try:
            user_tz = ZoneInfo(timezone)
            current_time = datetime.now(user_tz)
            current_time_str = current_time.strftime(f"%A, %B %d, %Y at %I:%M %p ({timezone})")
            current_date_str = current_time.strftime("%A, %B %d, %Y")
            return current_time_str, current_date_str
        except Exception:
            # Fallback to UTC if timezone is invalid
            current_time = datetime.now(UTC)
            current_time_str = current_time.strftime("%A, %B %d, %Y at %I:%M %p (UTC)")
            current_date_str = current_time.strftime("%A, %B %d, %Y")
            return current_time_str, current_date_str
    
    def build_user_config(self) -> UserConfigData:
        """Build the UserConfigData structure from user information.
        
        Returns:
            UserConfigData dictionary with user info, name, time strings, and timezone
        """
        user_name = self._extract_user_name()
        timezone = self._get_timezone()
        current_time_str, current_date_str = self._get_current_time_strings(timezone)
        
        self.user_config: UserConfigData = {
            "user_info": self.user_info,
            "user_name": user_name,
            "current_time_str": current_time_str,
            "current_date_str": current_date_str,
            "timezone": timezone
        }
        
        return self.user_config
    
    def get_live_config(self) -> LiveConnectConfig:
        """Get the LiveConnectConfig for Gemini based on user configuration.
        
        Returns:
            LiveConnectConfig object configured for the user
        """
        if not self.user_config:
            self.build_user_config()
        
        self.live_config = get_live_config(self.user_config)
        print(f"🔄 User config: {self.user_config}")
        
        return self.live_config
    
    def setup(self) -> Tuple[dict, LiveConnectConfig]:
        """Complete setup: initialize session, load user info, and get live config.
        
        Returns:
            Tuple of (user_config dict, live_config LiveConnectConfig)
        """
        self.initialize_session()
        self.load_user_info()
        user_config = self.build_user_config()
        live_config = self.get_live_config()
        
        return user_config, live_config
