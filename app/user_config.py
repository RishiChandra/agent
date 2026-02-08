from typing import Optional, TypedDict


class UserConfigData(TypedDict):
    """Data structure for user config parameters."""
    user_info: Optional[dict]
    user_name: str
    current_time_str: str
    current_date_str: str
    timezone: str
