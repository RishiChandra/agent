import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

def get_db_connection():
    """Get a connection to the PostgreSQL database from environment variables."""
    try:
        conn = psycopg2.connect(
            host=os.environ["DB_HOST"],
            port=os.environ.get("DB_PORT", "5432"),
            database=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"]
        )
        return conn
    except psycopg2.Error as e:
        print(f"Error connecting to database: {e}")
        raise

def execute_query(query, params=None):
    """
    Execute a SELECT query and return the results as a list of dictionaries.
    
    Args:
        query: SQL query string
        params: Optional tuple of parameters for the query
        
    Returns:
        List of dictionaries containing the query results
    """
    conn = None
    try:
        conn = get_db_connection()
        print(f"Connected to database: {conn}")

        cursor = conn.cursor(cursor_factory=RealDictCursor)
        print(f"Cursor: {cursor}")
        
        print(f"Executing query: {query} {params}")

        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        print("Executed query")

        results = cursor.fetchall()
        print(f"Results: {results}")
        # Convert rows to dictionaries
        return [dict(row) for row in results]
    
    except psycopg2.Error as e:
        print(f"Error executing query: {e}")
        raise
    finally:
        if conn:
            if 'cursor' in locals():
                cursor.close()
            conn.close()

def execute_update(query, params=None):
    """
    Execute an INSERT, UPDATE, or DELETE query and commit the changes.
    
    Args:
        query: SQL query string
        params: Optional tuple of parameters for the query
        
    Returns:
        Number of rows affected
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        
        rows_affected = cursor.rowcount
        conn.commit()
        return rows_affected
    
    except psycopg2.Error as e:
        if conn:
            conn.rollback()
        print(f"Error executing update: {e}")
        raise
    finally:
        if conn:
            if 'cursor' in locals():
                cursor.close()
            conn.close()


def get_user_timezone(user_id: str) -> str:
    """
    Get the timezone for a user.
    
    Args:
        user_id: The user ID to fetch timezone for
        
    Returns:
        The user's timezone string (e.g., 'America/New_York'), or 'UTC' if not found
    """
    query = "SELECT timezone FROM users WHERE user_id = %s"
    results = execute_query(query, (user_id,))
    
    if results and len(results) > 0:
        return results[0].get("timezone", "UTC")
    
    return "UTC"


def update_task_enqueue_sequence_id(task_id: str, sequence_id) -> int:
    """
    Update the enqueue_sequence_id for a task (e.g. after enqueueing to Service Bus).

    Args:
        task_id: The task ID to update
        sequence_id: The sequence ID from the queue (e.g. Service Bus)

    Returns:
        Number of rows affected
    """
    return execute_update(
        "UPDATE tasks SET enqueue_sequence_id = %s WHERE task_id = %s",
        (sequence_id, task_id),
    )


def get_user_by_id(user_id: str) -> dict:
    """
    Get full user profile by user_id.
    
    Args:
        user_id: The user ID to fetch
        
    Returns:
        Dictionary with user profile data, or None if not found
    """
    query = """
        SELECT user_id, first_name, last_name, firebase_uid, username, timezone, device_prefix
        FROM users
        WHERE user_id = %s
    """
    results = execute_query(query, (user_id,))
    
    if results and len(results) > 0:
        return results[0]
    
    return None
