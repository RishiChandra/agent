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
            cursor.close()
            conn.close()

