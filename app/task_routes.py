import traceback
from datetime import datetime, UTC
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from task_crud import (
    get_tasks_by_user_id,
    get_task_by_id,
    create_task,
    update_task,
    delete_task
)
from task_enqueue import enqueue_task as enqueue_task_to_service_bus

# Create router for all endpoints
router = APIRouter()

# ===== Task API Models =====
class TaskCreateRequest(BaseModel):
    user_id: str
    task_info: Optional[dict] = None
    status: Optional[str] = None
    time_to_execute: Optional[str] = None  # ISO 8601 format datetime string
    timezone: Optional[str] = None  # Timezone name (e.g., "PST", "EST")
    timezone_offset: Optional[float] = None  # Timezone offset in hours (e.g., -8.0 for PST)
    enqueue: Optional[bool] = True  # Whether to enqueue to Service Bus after creating


class TaskUpdateRequest(BaseModel):
    task_info: Optional[dict] = None
    status: Optional[str] = None
    time_to_execute: Optional[str] = None  # ISO 8601 format datetime string
    timezone: Optional[str] = None  # Timezone name (e.g., "PST", "EST")
    timezone_offset: Optional[float] = None  # Timezone offset in hours (e.g., -8.0 for PST)


class TaskEnqueueRequest(BaseModel):
    task_id: str
    user_id: str
    task_info: Optional[dict] = None
    status: Optional[str] = None
    time_to_execute: Optional[str] = None  # ISO 8601 format datetime string


# ===== Health Check Endpoint =====

@router.get("/healthz")
def healthz():
    print("/healthz called and accepted")
    return {"ok": True, "last_updated": "Dec 27 4:53 PST"}


# ===== Task CRUD Endpoints =====

@router.get("/tasks/{user_id}")
async def get_tasks(user_id: str):
    """
    Get all tasks for a specific user.
    
    Args:
        user_id: The user ID to fetch tasks for
        
    Returns:
        List of tasks
    """
    try:
        tasks = get_tasks_by_user_id(user_id)
        return {"tasks": tasks, "count": len(tasks)}
    except Exception as e:
        print(f"Error fetching tasks: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error fetching tasks: {str(e)}")


@router.get("/tasks/{user_id}/{task_id}")
async def get_task(user_id: str, task_id: str):
    """
    Get a single task by ID.
    
    Args:
        user_id: The user ID (for validation)
        task_id: The task ID to fetch
        
    Returns:
        Task dictionary
    """
    try:
        task = get_task_by_id(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        
        # Verify the task belongs to the user
        if task["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Task does not belong to this user")
        
        return task
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching task: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error fetching task: {str(e)}")


@router.post("/tasks")
async def create_task_endpoint(request: TaskCreateRequest):
    """
    Create a new task.
    
    If enqueue is True (default), the task will also be enqueued to Service Bus.
    
    Returns:
        Created task dictionary
    """
    try:
        # Ensure time_to_execute has the correct timezone (user's timezone, not UTC)
        time_to_execute_final = request.time_to_execute
        if request.time_to_execute and request.timezone_offset is not None:
            try:
                from datetime import timezone, timedelta
                # Parse the datetime string (it may not have timezone info)
                dt_str = request.time_to_execute.replace('Z', '+00:00')
                dt = datetime.fromisoformat(dt_str)
                # If datetime doesn't have timezone info, assume it's in the provided timezone
                if dt.tzinfo is None:
                    # Create timezone from offset
                    tz = timezone(timedelta(hours=request.timezone_offset))
                    dt = dt.replace(tzinfo=tz)
                # If it's in UTC, convert to user's timezone (don't store in UTC)
                elif dt.tzinfo == UTC or str(dt.tzinfo) == "UTC":
                    # Convert from UTC to user's timezone
                    user_tz = timezone(timedelta(hours=request.timezone_offset))
                    dt = dt.astimezone(user_tz)
                # Keep the timezone as-is (respect user's timezone)
                time_to_execute_final = dt.isoformat()
            except Exception as e:
                print(f"Warning: Failed to set timezone for time_to_execute: {e}")
                # Fall back to original value
        
        # Create task in database (with optional enqueue)
        task = create_task(
            user_id=request.user_id,
            task_info=request.task_info,
            status=request.status,
            time_to_execute=time_to_execute_final,
            enqueue=request.enqueue if request.enqueue is not None else True
        )
        return task
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"Error creating task: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error creating task: {str(e)}")


@router.put("/tasks/{user_id}/{task_id}")
async def update_task_endpoint(user_id: str, task_id: str, request: TaskUpdateRequest):
    """
    Update an existing task.
    
    Args:
        user_id: The user ID (for validation)
        task_id: The task ID to update
        
    Returns:
        Updated task dictionary
    """
    try:
        # Verify the task exists and belongs to the user
        existing_task = get_task_by_id(task_id)
        if existing_task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        
        if existing_task["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Task does not belong to this user")
        
        # Ensure time_to_execute has the correct timezone (user's timezone, not UTC)
        time_to_execute_final = request.time_to_execute
        if request.time_to_execute and request.timezone_offset is not None:
            try:
                from datetime import timezone, timedelta
                # Parse the datetime string (it may not have timezone info)
                dt_str = request.time_to_execute.replace('Z', '+00:00')
                dt = datetime.fromisoformat(dt_str)
                # If datetime doesn't have timezone info, assume it's in the provided timezone
                if dt.tzinfo is None:
                    # Create timezone from offset
                    tz = timezone(timedelta(hours=request.timezone_offset))
                    dt = dt.replace(tzinfo=tz)
                # If it's in UTC, convert to user's timezone (don't store in UTC)
                elif dt.tzinfo == UTC or str(dt.tzinfo) == "UTC":
                    # Convert from UTC to user's timezone
                    user_tz = timezone(timedelta(hours=request.timezone_offset))
                    dt = dt.astimezone(user_tz)
                # Keep the timezone as-is (respect user's timezone)
                time_to_execute_final = dt.isoformat()
            except Exception as e:
                print(f"Warning: Failed to set timezone for time_to_execute: {e}")
                # Fall back to original value
        
        # Update task
        task = update_task(
            task_id=task_id,
            task_info=request.task_info,
            status=request.status,
            time_to_execute=time_to_execute_final
        )
        
        return task
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"Error updating task: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error updating task: {str(e)}")


@router.delete("/tasks/{user_id}/{task_id}")
async def delete_task_endpoint(user_id: str, task_id: str):
    """
    Delete a task by ID.
    
    Args:
        user_id: The user ID (for validation)
        task_id: The task ID to delete
        
    Returns:
        Success message
    """
    try:
        # Verify the task exists and belongs to the user
        existing_task = get_task_by_id(task_id)
        if existing_task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        
        if existing_task["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Task does not belong to this user")
        
        # Delete task
        deleted = delete_task(task_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Task not found")
        
        return {"success": True, "message": "Task deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error deleting task: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error deleting task: {str(e)}")


# ===== Task Enqueue Endpoint (standalone) =====

@router.post("/enqueue-task")
async def enqueue_task_endpoint(request: TaskEnqueueRequest):
    """
    Enqueue an existing task to Azure Service Bus queue.
    
    This endpoint is for enqueueing tasks that already exist in the database.
    If time_to_execute is provided, the message will be scheduled for that time.
    Otherwise, it will be sent immediately.
    """
    try:
        result = enqueue_task_to_service_bus(
            task_id=request.task_id,
            user_id=request.user_id,
            task_info=request.task_info,
            time_to_execute=request.time_to_execute
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"Error enqueueing task: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error enqueueing task to Service Bus: {str(e)}")
