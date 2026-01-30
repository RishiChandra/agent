"""
Shared test utilities and mock data for agent tests.
"""
import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Constants
DEFAULT_USER_ID = "2ba330c0-a999-46f8-ba2c-855880bdcf5b"
DEFAULT_TIMEZONE = "UTC"

# Environment variable checks
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_SERVICEBUS_CONNECTION_STRING = os.getenv("AZURE_SERVICEBUS_CONNECTION_STRING")


def are_openai_credentials_configured():
    """Check if Azure OpenAI credentials are configured."""
    return bool(AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY)


def get_today_date_strings():
    """
    Get today's date in multiple formats for test context.
    
    Returns:
        tuple: (today, today_readable) where:
            - today: ISO format date string (YYYY-MM-DD)
            - today_readable: Human-readable date string (Month DD, YYYY)
    """
    today = datetime.now().strftime("%Y-%m-%d")
    today_readable = datetime.now().strftime("%B %d, %Y")
    return today, today_readable


def create_mock_enqueue_result(task_id=None, scheduled_time=None, success=True):
    """
    Create a mock enqueue result dictionary.
    
    Args:
        task_id: Optional task ID (if None, will need to be set later)
        scheduled_time: Optional scheduled time (if None, will need to be set later)
        success: Whether the enqueue was successful (default: True)
    
    Returns:
        dict: Mock enqueue result
    """
    return {
        "success": success,
        "task_id": task_id,
        "scheduled_time": scheduled_time,
        "message": "Task scheduled successfully" if success else "Task scheduling failed"
    }


def create_enqueue_side_effect():
    """
    Create a side effect function for mocking enqueue_task.
    
    Returns:
        function: Side effect function that takes (task_id, user_id, task_info, time_to_execute)
                  and returns a mock enqueue result
    """
    def enqueue_side_effect(task_id, user_id, task_info, time_to_execute):
        return create_mock_enqueue_result(
            task_id=task_id,
            scheduled_time=time_to_execute
        )
    return enqueue_side_effect


def create_mock_task(
    task_id="test-task-1",
    user_id=None,
    task_info=None,
    status="pending",
    time_to_execute=None,
    days_offset=1,
    hours_offset=0
):
    """
    Create a mock task dictionary for testing.
    
    Args:
        task_id: Task ID string
        user_id: User ID string (defaults to DEFAULT_USER_ID)
        task_info: Task info dict (defaults to {"info": "Test task"})
        status: Task status (default: "pending")
        time_to_execute: Datetime object (if None, creates one based on offsets)
        days_offset: Days offset from now for time_to_execute (default: 1)
        hours_offset: Hours offset from now for time_to_execute (default: 0)
    
    Returns:
        dict: Mock task dictionary
    """
    if user_id is None:
        user_id = DEFAULT_USER_ID
    
    if task_info is None:
        task_info = {"info": "Test task"}
    
    if time_to_execute is None:
        time_to_execute = datetime.now(timezone.utc) + timedelta(days=days_offset, hours=hours_offset)
    
    return {
        "task_id": task_id,
        "user_id": user_id,
        "task_info": task_info,
        "status": status,
        "time_to_execute": time_to_execute
    }


def create_mock_tasks(count=2, user_id=None, base_task_id="test-task"):
    """
    Create a list of mock tasks for testing.
    
    Args:
        count: Number of tasks to create (default: 2)
        user_id: User ID string (defaults to DEFAULT_USER_ID)
        base_task_id: Base task ID prefix (default: "test-task")
    
    Returns:
        list: List of mock task dictionaries
    """
    if user_id is None:
        user_id = DEFAULT_USER_ID
    
    tasks = []
    for i in range(1, count + 1):
        task = create_mock_task(
            task_id=f"{base_task_id}-{i}",
            user_id=user_id,
            task_info={"info": f"Test task {i}"},
            days_offset=1,
            hours_offset=i * 2  # Stagger tasks by 2 hours
        )
        tasks.append(task)
    
    return tasks


def get_default_user_config(user_id=None, timezone=None, include_user_info=True):
    """
    Create a default user config dictionary for testing.
    
    Args:
        user_id: User ID string (defaults to DEFAULT_USER_ID)
        timezone: Timezone string (defaults to DEFAULT_TIMEZONE)
        include_user_info: Whether to include user_info in config (default: True)
    
    Returns:
        dict: User config dictionary
    """
    if user_id is None:
        user_id = DEFAULT_USER_ID
    
    if timezone is None:
        timezone = DEFAULT_TIMEZONE
    
    config = {
        "timezone": timezone
    }
    
    if include_user_info:
        config["user_info"] = {
            "user_id": user_id
        }
    
    return config


def create_chat_history_with_date(user_message, include_date=True):
    """
    Create a chat history with today's date context.
    
    Args:
        user_message: The user message content
        include_date: Whether to include date context in the message (default: True)
    
    Returns:
        list: Chat history with user message
    """
    if include_date:
        today, today_readable = get_today_date_strings()
        full_message = f"Today is {today_readable} ({today}). {user_message}"
    else:
        full_message = user_message
    
    return [
        {"role": "user", "content": full_message}
    ]


def create_tasks_in_time_range(start_time, end_time, count=3, user_id=None, base_task_id="range-task"):
    """
    Create mock tasks distributed within a specific time range.
    
    Args:
        start_time: Start datetime for the range
        end_time: End datetime for the range
        count: Number of tasks to create (default: 3)
        user_id: User ID string (defaults to DEFAULT_USER_ID)
        base_task_id: Base task ID prefix (default: "range-task")
    
    Returns:
        list: List of mock task dictionaries within the time range
    """
    if user_id is None:
        user_id = DEFAULT_USER_ID
    
    tasks = []
    time_range = (end_time - start_time).total_seconds()
    
    for i in range(count):
        # Distribute tasks evenly across the time range
        offset_seconds = (time_range / (count + 1)) * (i + 1)
        task_time = start_time + timedelta(seconds=offset_seconds)
        
        task = create_mock_task(
            task_id=f"{base_task_id}-{i+1}",
            user_id=user_id,
            task_info={"info": f"Task in range {i+1}"},
            time_to_execute=task_time
        )
        tasks.append(task)
    
    return tasks


def create_tasks_outside_time_range(before_start=True, after_end=True, user_id=None):
    """
    Create mock tasks that are outside a time range (before start and/or after end).
    
    Args:
        before_start: Whether to create tasks before the start time (default: True)
        after_end: Whether to create tasks after the end time (default: True)
        user_id: User ID string (defaults to DEFAULT_USER_ID)
    
    Returns:
        list: List of mock task dictionaries outside the time range
    """
    if user_id is None:
        user_id = DEFAULT_USER_ID
    
    tasks = []
    now = datetime.now(timezone.utc)
    
    if before_start:
        # Create tasks 2 days before
        task = create_mock_task(
            task_id="task-before-range",
            user_id=user_id,
            task_info={"info": "Task before range"},
            time_to_execute=now - timedelta(days=2)
        )
        tasks.append(task)
    
    if after_end:
        # Create tasks 7 days after
        task = create_mock_task(
            task_id="task-after-range",
            user_id=user_id,
            task_info={"info": "Task after range"},
            time_to_execute=now + timedelta(days=7)
        )
        tasks.append(task)
    
    return tasks


def create_tasks_in_time_range(start_time, end_time, count=3, user_id=None, base_task_id="range-task"):
    """
    Create mock tasks distributed within a specific time range.
    
    Args:
        start_time: Start datetime for the range
        end_time: End datetime for the range
        count: Number of tasks to create (default: 3)
        user_id: User ID string (defaults to DEFAULT_USER_ID)
        base_task_id: Base task ID prefix (default: "range-task")
    
    Returns:
        list: List of mock task dictionaries within the time range
    """
    if user_id is None:
        user_id = DEFAULT_USER_ID
    
    tasks = []
    time_range = (end_time - start_time).total_seconds()
    
    for i in range(count):
        # Distribute tasks evenly across the time range
        offset_seconds = (time_range / (count + 1)) * (i + 1)
        task_time = start_time + timedelta(seconds=offset_seconds)
        
        task = create_mock_task(
            task_id=f"{base_task_id}-{i+1}",
            user_id=user_id,
            task_info={"info": f"Task in range {i+1}"},
            time_to_execute=task_time
        )
        tasks.append(task)
    
    return tasks


def create_tasks_outside_time_range(before_start=True, after_end=True, user_id=None):
    """
    Create mock tasks that are outside a time range (before start and/or after end).
    
    Args:
        before_start: Whether to create tasks before the start time (default: True)
        after_end: Whether to create tasks after the end time (default: True)
        user_id: User ID string (defaults to DEFAULT_USER_ID)
    
    Returns:
        list: List of mock task dictionaries outside the time range
    """
    if user_id is None:
        user_id = DEFAULT_USER_ID
    
    tasks = []
    now = datetime.now(timezone.utc)
    
    if before_start:
        # Create tasks 2 days before
        task = create_mock_task(
            task_id="task-before-range",
            user_id=user_id,
            task_info={"info": "Task before range"},
            time_to_execute=now - timedelta(days=2)
        )
        tasks.append(task)
    
    if after_end:
        # Create tasks 7 days after
        task = create_mock_task(
            task_id="task-after-range",
            user_id=user_id,
            task_info={"info": "Task after range"},
            time_to_execute=now + timedelta(days=7)
        )
        tasks.append(task)
    
    return tasks
