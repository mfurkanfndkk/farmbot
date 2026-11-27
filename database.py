import os
import datetime
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    import psycopg2
    from psycopg2.extras import RealDictCursor

    def get_connection():
        # Neon / Supabase postgres:// fix if provided as postgres:// instead of postgresql://
        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(url, cursor_factory=RealDictCursor)

    def init_db():
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS plots (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        duration_seconds INTEGER NOT NULL,
                        last_harvested TEXT,
                        next_harvest TEXT,
                        alerted INTEGER DEFAULT 0
                    )
                """)
            conn.commit()

    def add_plot(name: str, duration_seconds: int) -> int:
        now = datetime.datetime.now(datetime.timezone.utc)
        next_harvest = now + datetime.timedelta(seconds=duration_seconds)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO plots (name, duration_seconds, last_harvested, next_harvest, alerted)
                    VALUES (%s, %s, %s, %s, 0)
                    RETURNING id
                """, (name, duration_seconds, now.isoformat(), next_harvest.isoformat()))
                new_id = cur.fetchone()["id"]
            conn.commit()
            return new_id

    def record_harvest(plot_id: int) -> Optional[Dict[str, Any]]:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM plots WHERE id = %s", (plot_id,))
                row = cur.fetchone()
                if not row:
                    return None
                
                now = datetime.datetime.now(datetime.timezone.utc)
                duration = row["duration_seconds"]
                next_harvest = now + datetime.timedelta(seconds=duration)
                
                cur.execute("""
                    UPDATE plots
                    SET last_harvested = %s, next_harvest = %s, alerted = 0
                    WHERE id = %s
                    RETURNING *
                """, (now.isoformat(), next_harvest.isoformat(), plot_id))
                updated = cur.fetchone()
            conn.commit()
            return dict(updated) if updated else None

    def get_all_plots() -> List[Dict[str, Any]]:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM plots ORDER BY id ASC")
                return [dict(row) for row in cur.fetchall()]

    def get_due_plots() -> List[Dict[str, Any]]:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT * FROM plots
                    WHERE next_harvest <= %s AND alerted = 0
                """, (now,))
                return [dict(row) for row in cur.fetchall()]

    def mark_alerted(plot_id: int):
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE plots SET alerted = 1 WHERE id = %s", (plot_id,))
            conn.commit()

    def delete_plot(plot_id: int) -> bool:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM plots WHERE id = %s", (plot_id,))
                rowcount = cur.rowcount
            conn.commit()
            return rowcount > 0

else:
    import sqlite3
    DB_PATH = "farmbot.db"

    def get_connection():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db():
        with get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS plots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    last_harvested TEXT,
                    next_harvest TEXT,
                    alerted INTEGER DEFAULT 0
                )
            """)
            conn.commit()

    def add_plot(name: str, duration_seconds: int) -> int:
        now = datetime.datetime.now(datetime.timezone.utc)
        next_harvest = now + datetime.timedelta(seconds=duration_seconds)
        with get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO plots (name, duration_seconds, last_harvested, next_harvest, alerted)
                VALUES (?, ?, ?, ?, 0)
            """, (name, duration_seconds, now.isoformat(), next_harvest.isoformat()))
            conn.commit()
            return cursor.lastrowid

    def record_harvest(plot_id: int) -> Optional[Dict[str, Any]]:
        with get_connection() as conn:
            cursor = conn.execute("SELECT * FROM plots WHERE id = ?", (plot_id,))
            row = cursor.fetchone()
            if not row:
                return None
            
            now = datetime.datetime.now(datetime.timezone.utc)
            duration = row["duration_seconds"]
            next_harvest = now + datetime.timedelta(seconds=duration)
            
            conn.execute("""
                UPDATE plots
                SET last_harvested = ?, next_harvest = ?, alerted = 0
                WHERE id = ?
            """, (now.isoformat(), next_harvest.isoformat(), plot_id))
            conn.commit()
            
            cursor = conn.execute("SELECT * FROM plots WHERE id = ?", (plot_id,))
            return dict(cursor.fetchone())

    def get_all_plots() -> List[Dict[str, Any]]:
        with get_connection() as conn:
            cursor = conn.execute("SELECT * FROM plots ORDER BY id ASC")
            return [dict(row) for row in cursor.fetchall()]

    def get_due_plots() -> List[Dict[str, Any]]:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM plots
                WHERE next_harvest <= ? AND alerted = 0
            """, (now,))
            return [dict(row) for row in cursor.fetchall()]

    def mark_alerted(plot_id: int):
        with get_connection() as conn:
            conn.execute("UPDATE plots SET alerted = 1 WHERE id = ?", (plot_id,))
            conn.commit()

    def delete_plot(plot_id: int) -> bool:
        with get_connection() as conn:
            cursor = conn.execute("DELETE FROM plots WHERE id = ?", (plot_id,))
            conn.commit()
            return cursor.rowcount > 0
