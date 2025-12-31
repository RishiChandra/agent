from database import execute_query, execute_update

def get_session(user_id):
    query = """
        SELECT * FROM sessions WHERE user_id = %s
    """
    results = execute_query(query, (user_id,))
    return results[0] if results else None

def create_session(user_id):
    query = """
        INSERT INTO sessions (user_id, is_active)
        VALUES (%s, %s)
    """
    execute_update(query, (user_id, True))

def update_session_status(user_id, is_active):
    if is_active:
        query = """
            UPDATE sessions 
            SET is_active = %s 
            WHERE user_id = %s
        """
        execute_update(query, (is_active, user_id))
    else:
        query = """
            UPDATE sessions 
            SET is_active = %s, scratchpad = '' 
            WHERE user_id = %s
        """
        execute_update(query, (is_active, user_id))