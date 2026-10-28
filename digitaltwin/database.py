import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

# sanitize_input lives in helpers so the serverless handler shares it; re-exported here for existing callers
from . import PROJECT_ROOT
from .helpers import (
    sanitize_input, clean_contact, contact_saved_reply, INVALID_EMAIL_REPLY, CONTACT_LIMIT_REPLY,
    MAX_CONTACTS_PER_HOUR, LOG_RETENTION_DAYS,
)

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# Stored in DATA_DIR when set (e.g. a mounted volume on AWS), otherwise at the repository root
# so the same log file is used regardless of working directory
SQL_DB_FILE = os.path.join(os.getenv("DATA_DIR") or PROJECT_ROOT, "conversation_logs.db")

def _connect():
    """Opens a connection that is always closed (closing) and committed or rolled back (inner with)."""
    return closing(sqlite3.connect(SQL_DB_FILE))

def init_sql_db():
    """Creates the SQLite database and logging table safely."""
    with _connect() as conn, conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                user_question TEXT,
                bot_answer TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS contact_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                name TEXT,
                email TEXT,
                message TEXT
            )
        """)
        # Retention: conversation logs older than LOG_RETENTION_DAYS are purged on every app start
        # (timestamps are fixed-width, so string comparison orders them chronologically)
        cutoff = (datetime.now() - timedelta(days=LOG_RETENTION_DAYS)).strftime(TIMESTAMP_FORMAT)
        cursor.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff,))

def log_conversation(question, answer):
    """
    Logs a user question and model response using parameterized queries.
    This structure inherently prevents 100% of SQL Injection attacks.
    """
    # 1. Sanitize incoming inputs
    clean_question = sanitize_input(question, max_length=1000)
    clean_answer = sanitize_input(answer, max_length=4000) # LLM responses can be longer
    
    timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)

    # 2. Secure Parameterization: The '?' markers treat inputs strictly as values, 
    # ensuring malicious SQL fragments (e.g., "UNION SELECT") are processed as plain text.
    with _connect() as conn, conn:
        conn.execute(
            "INSERT INTO logs (timestamp, user_question, bot_answer) VALUES (?, ?, ?)",
            (timestamp, clean_question, clean_answer)
        )


# New tool function: Log structured recruiter info
def log_contact_request(name, email, message):
    """Saves a structured contact request directly into the database."""
    contact = clean_contact(name, email, message)
    if contact is None:
        return INVALID_EMAIL_REPLY
    now = datetime.now()
    timestamp = now.strftime(TIMESTAMP_FORMAT)
    hour_ago = (now - timedelta(hours=1)).strftime(TIMESTAMP_FORMAT)
    with _connect() as conn, conn:
        # Global hourly cap: the tool is reachable by anyone, so it must not be usable to fill the disk
        (recent,) = conn.execute("SELECT COUNT(*) FROM contact_requests WHERE timestamp >= ?", (hour_ago,)).fetchone()
        if recent >= MAX_CONTACTS_PER_HOUR:
            return CONTACT_LIMIT_REPLY
        conn.execute(
            "INSERT INTO contact_requests (timestamp, name, email, message) VALUES (?, ?, ?, ?)",
            (timestamp, *contact)
        )
    return contact_saved_reply(contact[0])
