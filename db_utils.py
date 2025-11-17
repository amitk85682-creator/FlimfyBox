import os
import logging
from urllib.parse import urlparse, quote
import psycopg2
from typing import Optional, Dict

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL')

def fix_database_url(url: Optional[str]) -> Optional[str]:
    """Fix database URL by encoding special characters in password."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.password and any(c in parsed.password for c in ['*', '!', '@', '#', '$', '%', '^', '&', '(', ')', '=', '+', '?']):
            encoded_password = quote(parsed.password)
            fixed_url = f"postgresql://{parsed.username}:{encoded_password}@{parsed.hostname}:{parsed.port}{parsed.path}"
            return fixed_url
        return url
    except Exception as e:
        logger.error(f"Error fixing DB URL: {e}")
        return url

FIXED_DATABASE_URL = fix_database_url(DATABASE_URL)

def get_db_connection():
    """Get a psycopg2 connection or None on failure."""
    if not FIXED_DATABASE_URL:
        logger.error("DATABASE_URL not set.")
        return None
    try:
        return psycopg2.connect(FIXED_DATABASE_URL)
    except Exception as e:
        logger.error(f"DB connection error: {e}")
        return None

def upsert_movie_and_files(conn, title: str, description: str, qualities: Dict[str, str], aliases_str: str) -> Optional[int]:
    """
    Insert or update movie, its multiple quality links (movie_files), and aliases.
    Returns movie_id or None on error.
    """
    if not title:
        return None
    cur = conn.cursor()
    try:
        # Upsert movie record (we keep movies.url/file_id empty because we store qualities separately)
        cur.execute("""
            INSERT INTO movies (title, url, file_id, description)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (title) DO UPDATE SET description = EXCLUDED.description
            RETURNING id
        """, (title.strip(), "", None, description))
        movie_id = cur.fetchone()[0]

        # Upsert qualities
        for quality, link in (qualities or {}).items():
            if not link:
                continue
            link = link.strip()
            if not link:
                continue
            if any(link.startswith(prefix) for prefix in ("BQAC", "BAAC", "CAAC", "AQAC")):
                cur.execute("""
                    INSERT INTO movie_files (movie_id, quality, file_id, url)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (movie_id, quality) DO UPDATE SET file_id = EXCLUDED.file_id, url = NULL
                """, (movie_id, quality, link, None))
            else:
                cur.execute("""
                    INSERT INTO movie_files (movie_id, quality, url, file_id)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (movie_id, quality) DO UPDATE SET url = EXCLUDED.url, file_id = NULL
                """, (movie_id, quality, link, None))

        # Add aliases
        aliases = [a.strip() for a in (aliases_str or "").split(',') if a.strip()]
        for alias in aliases:
            cur.execute("""
                INSERT INTO movie_aliases (movie_id, alias)
                VALUES (%s, %s)
                ON CONFLICT (movie_id, alias) DO NOTHING
            """, (movie_id, alias.lower()))

        conn.commit()
        return movie_id
    except Exception as e:
        conn.rollback()
        logger.error(f"Error upserting movie '{title}': {e}")
        return None
    finally:
        cur.close()
