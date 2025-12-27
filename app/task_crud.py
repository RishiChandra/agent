"""
Task CRUD operations module.
Handles all database operations for tasks.
"""
import json
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime
from database import execute_query, execute_update, get_db_connection
import psycopg2

# Import enqueue function (with safe import to avoid circular dependencies)
try:
    from task_enqueue import enqueue_task_safe
except ImportError:
    enqueue_task_safe = None


def get_tasks_by_user_id(user_id: str) -> List[Dict[str, Any]]:
    """
    Get all tasks for a specific user.
    
    Args:
        user_id: The user ID to fetch tasks for
        
    Returns:
        List of task dictionaries
    """
    try:
        query = """
            SELECT task_id, user_id, task_info, status, time_to_execute
            FROM tasks
            WHERE user_id = %s
            ORDER BY time_to_execute ASC NULLS LAST, task_id DESC
        """
        results = execute_query(query, (user_id,))
        
        # Convert results to proper format
        tasks = []
        for row in results:
            task = {
                "task_id": row["task_id"],
                "user_id": row["user_id"],
                "task_info": row["task_info"] if row["task_info"] else None,
                "status": row["status"],
                "time_to_execute": row["time_to_execute"].isoformat() if row["time_to_execute"] else None
            }
            tasks.append(task)
        
        return tasks
    except Exception as e:
        print(f"Error fetching tasks: {e}")
        raise


def get_task_by_id(task_id: str) -> Optional[Dict[str, Any]]:
    """
    Get a single task by ID.
    
    Args:
        task_id: The task ID to fetch
        
    Returns:
        Task dictionary or None if not found
    """
    try:
        query = """
            SELECT task_id, user_id, task_info, status, time_to_execute
            FROM tasks
            WHERE task_id = %s
        """
        results = execute_query(query, (task_id,))
        
        if not results:
            return None
        
        row = results[0]
        return {
            "task_id": row["task_id"],
            "user_id": row["user_id"],
            "task_info": row["task_info"] if row["task_info"] else None,
            "status": row["status"],
            "time_to_execute": row["time_to_execute"].isoformat() if row["time_to_execute"] else None
        }
    except Exception as e:
        print(f"Error fetching task: {e}")
        raise


def create_task(
    user_id: str,
    task_info: Optional[Dict[str, Any]] = None,
    status: Optional[str] = None,
    time_to_execute: Optional[str] = None,
    enqueue: bool = False
) -> Dict[str, Any]:
    """
    Create a new task.
    
    Args:
        user_id: The user ID who owns the task
        task_info: Optional task information dictionary
        status: Optional task status (defaults to 'pending')
        time_to_execute: Optional ISO 8601 datetime string for when to execute the task
        enqueue: Whether to enqueue the task to Service Bus after creating (default: False)
        
    Returns:
        Created task dictionary (may include enqueue_result or enqueue_warning if enqueue=True)
    """
    try:
        task_id = str(uuid.uuid4())
        task_status = status or "pending"
        
        # Parse time_to_execute if provided
        time_to_execute_dt = None
        if time_to_execute:
            try:
                time_to_execute_dt = datetime.fromisoformat(time_to_execute.replace('Z', '+00:00'))
            except ValueError:
                raise ValueError(f"Invalid time_to_execute format: {time_to_execute}. Expected ISO 8601 format.")
        
        # Convert task_info to JSON string if provided
        task_info_json = None
        if task_info:
            task_info_json = json.dumps(task_info)
        
        query = """
            INSERT INTO tasks (task_id, user_id, task_info, status, time_to_execute)
            VALUES (%s, %s, %s::jsonb, %s, %s)
        """
        execute_update(query, (task_id, user_id, task_info_json, task_status, time_to_execute_dt))
        
        task = {
            "task_id": task_id,
            "user_id": user_id,
            "task_info": task_info,
            "status": task_status,
            "time_to_execute": time_to_execute
        }
        
        # Enqueue to Service Bus if requested
        if enqueue and enqueue_task_safe is not None:
            try:
                enqueue_result = enqueue_task_safe(
                    task_id=task_id,
                    user_id=user_id,
                    task_info=task_info,
                    time_to_execute=time_to_execute
                )
                if enqueue_result:
                    task["enqueue_result"] = enqueue_result
                else:
                    task["enqueue_warning"] = "Task created but enqueue failed (see server logs)"
            except Exception as e:
                # Log error but don't fail the request - task is already created
                print(f"Warning: Failed to enqueue task to Service Bus: {e}")
                task["enqueue_warning"] = f"Task created but enqueue failed: {str(e)}"
        elif enqueue and enqueue_task_safe is None:
            task["enqueue_warning"] = "Enqueue requested but task_enqueue module not available"
        
        return task
    except psycopg2.Error as e:
        print(f"Database error creating task: {e}")
        raise
    except Exception as e:
        print(f"Error creating task: {e}")
        raise


def update_task(
    task_id: str,
    task_info: Optional[Dict[str, Any]] = None,
    status: Optional[str] = None,
    time_to_execute: Optional[str] = None
) -> Dict[str, Any]:
    """
    Update an existing task.
    
    Args:
        task_id: The task ID to update
        task_info: Optional task information dictionary to update
        status: Optional task status to update
        time_to_execute: Optional ISO 8601 datetime string to update
        
    Returns:
        Updated task dictionary
        
    Raises:
        ValueError: If task not found
    """
    try:
        # Build dynamic update query
        updates = []
        params = []
        
        if task_info is not None:
            updates.append("task_info = %s::jsonb")
            params.append(json.dumps(task_info))
        
        if status is not None:
            updates.append("status = %s")
            params.append(status)
        
        if time_to_execute is not None:
            try:
                time_to_execute_dt = datetime.fromisoformat(time_to_execute.replace('Z', '+00:00'))
                updates.append("time_to_execute = %s")
                params.append(time_to_execute_dt)
            except ValueError:
                raise ValueError(f"Invalid time_to_execute format: {time_to_execute}. Expected ISO 8601 format.")
        
        if not updates:
            # No updates, just return the existing task
            task = get_task_by_id(task_id)
            if task is None:
                raise ValueError("Task not found")
            return task
        
        # Add task_id to params
        params.append(task_id)
        
        query = f"""
            UPDATE tasks
            SET {', '.join(updates)}
            WHERE task_id = %s
        """
        execute_update(query, tuple(params))
        
        # Fetch and return updated task
        updated_task = get_task_by_id(task_id)
        if updated_task is None:
            raise ValueError("Task not found after update")
        
        return updated_task
    except psycopg2.Error as e:
        print(f"Database error updating task: {e}")
        raise
    except Exception as e:
        print(f"Error updating task: {e}")
        raise


def delete_task(task_id: str) -> bool:
    """
    Delete a task by ID.
    
    Args:
        task_id: The task ID to delete
        
    Returns:
        True if task was deleted, False if not found
    """
    try:
        query = """
            DELETE FROM tasks
            WHERE task_id = %s
        """
        rows_affected = execute_update(query, (task_id,))
        return rows_affected > 0
    except psycopg2.Error as e:
        print(f"Database error deleting task: {e}")
        raise
    except Exception as e:
        print(f"Error deleting task: {e}")
        raise

