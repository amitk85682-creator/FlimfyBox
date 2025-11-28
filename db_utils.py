import os
import logging
from urllib.parse import urlparse, quote
import psycopg2
from psycopg2.extras import RealDictCursor
from typing import Optional, Dict, Any, List

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
        conn = psycopg2.connect(FIXED_DATABASE_URL)
        # Ensure tables exist when we connect (lazy migration)
        ensure_tables_exist(conn)
        return conn
    except Exception as e:
        logger.error(f"DB connection error: {e}")
        return None

def ensure_tables_exist(conn):
    """Ensure necessary tables and columns exist."""
    try:
        cur = conn.cursor()
        
        # Create movie_files table if not exists
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movie_files (
                id SERIAL PRIMARY KEY,
                movie_id INTEGER REFERENCES movies(id) ON DELETE CASCADE,
                quality TEXT NOT NULL,
                file_id TEXT,
                url TEXT,
                file_size TEXT,
                UNIQUE(movie_id, quality)
            );
        """)
        
        # Check if file_size column exists in movie_files, if not add it
        cur.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name='movie_files' AND column_name='file_size';
        """)
        if not cur.fetchone():
            cur.execute("ALTER TABLE movie_files ADD COLUMN IF NOT EXISTS file_size TEXT;")
            logger.info("Added file_size column to movie_files table.")

        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        logger.error(f"Error ensuring tables exist: {e}")

def upsert_movie_and_files(conn, title: str, description: str, qualities: Dict[str, Any], aliases_str: str, movie_id: Optional[int] = None) -> Optional[int]:
    """
    Insert or update movie, its multiple quality links/sizes, and aliases.
    accepts qualities as: {'Quality': {'url': '...', 'size': '...'}, ...}
    Returns movie_id or None on error.
    """
    if not title:
        return None
    
    cur = conn.cursor()
    try:
        current_movie_id = movie_id

        # 1. Insert or Update Movie Record
        if current_movie_id:
            # Update existing
            cur.execute("""
                UPDATE movies 
                SET title = %s, description = %s 
                WHERE id = %s
            """, (title.strip(), description, current_movie_id))
        else:
            # Insert new or update description on conflict
            cur.execute("""
                INSERT INTO movies (title, url, file_id, description)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (title) DO UPDATE SET description = EXCLUDED.description
                RETURNING id
            """, (title.strip(), "", None, description))
            current_movie_id = cur.fetchone()[0]

        # 2. Upsert Qualities (Files/Links + Sizes)
        if qualities:
            for quality, data in qualities.items():
                link = ""
                size = ""

                # Handle data format (Dict or String)
                if isinstance(data, dict):
                    link = data.get('url', '').strip()
                    size = data.get('size', '').strip()
                else:
                    link = str(data).strip() if data else ""
                
                if not link:
                    continue

                # Determine if it's a File ID (BQAC...) or URL
                if any(link.startswith(prefix) for prefix in ("BQAC", "BAAC", "CAAC", "AQAC")):
                    cur.execute("""
                        INSERT INTO movie_files (movie_id, quality, file_id, url, file_size)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (movie_id, quality) 
                        DO UPDATE SET file_id = EXCLUDED.file_id, url = NULL, file_size = EXCLUDED.file_size
                    """, (current_movie_id, quality, link, None, size))
                else:
                    cur.execute("""
                        INSERT INTO movie_files (movie_id, quality, url, file_id, file_size)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (movie_id, quality) 
                        DO UPDATE SET url = EXCLUDED.url, file_id = NULL, file_size = EXCLUDED.file_size
                    """, (current_movie_id, quality, link, None, size))

        # 3. Add Aliases
        if aliases_str:
            aliases = [a.strip() for a in aliases_str.split(',') if a.strip()]
            for alias in aliases:
                cur.execute("""
                    INSERT INTO movie_aliases (movie_id, alias)
                    VALUES (%s, %s)
                    ON CONFLICT (movie_id, alias) DO NOTHING
                """, (current_movie_id, alias.lower()))

        conn.commit()
        return current_movie_id

    except Exception as e:
        conn.rollback()
        logger.error(f"Error upserting movie '{title}': {e}")
        return None
    finally:
        cur.close()

def get_all_movies(conn) -> List[Dict]:
    """Fetch all movies for admin list with file count."""
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # 👇 Updated Query: Counts files/links for each movie for the dashboard status
        cur.execute("""
            SELECT m.id, m.title, m.description,
                   (SELECT COUNT(*) FROM movie_files mf WHERE mf.movie_id = m.id) as file_count,
                   m.url, m.file_id
            FROM movies m 
            ORDER BY m.id DESC
        """)
        movies = cur.fetchall()
        cur.close()
        return movies
    except Exception as e:
        logger.error(f"Error fetching movies: {e}")
        return []

def get_movie_by_id(conn, movie_id: int) -> Optional[Dict]:
    """Fetch full movie details including qualities and aliases."""
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Get basic info
        cur.execute("SELECT * FROM movies WHERE id = %s", (movie_id,))
        movie = cur.fetchone()
        if not movie:
            return None

        # Get qualities/files
        cur.execute("SELECT quality, url, file_id, file_size FROM movie_files WHERE movie_id = %s", (movie_id,))
        files = cur.fetchall()
        
        # Reconstruct qualities dictionary for the form
        qualities_dict = {
            'Low Quality': {'url': '', 'size': ''},
            'SD Quality': {'url': '', 'size': ''},
            'Standard Quality': {'url': '', 'size': ''},
            'HD Quality': {'url': '', 'size': ''},
            '4K': {'url': '', 'size': ''}
        }
        
        for f in files:
            q_name = f['quality']
            # Determine value (File ID or URL)
            val = f['file_id'] if f['file_id'] else f['url']
            size = f['file_size'] if f['file_size'] else ''
            
            if q_name in qualities_dict:
                qualities_dict[q_name] = {'url': val, 'size': size}

        # Get aliases
        cur.execute("SELECT alias FROM movie_aliases WHERE movie_id = %s", (movie_id,))
        aliases_rows = cur.fetchall()
        aliases_str = ", ".join([row['alias'] for row in aliases_rows])

        # Convert RealDictRow to standard dict and add extras
        movie_data = dict(movie)
        movie_data['qualities'] = qualities_dict
        movie_data['aliases'] = aliases_str

        cur.close()
        return movie_data

    except Exception as e:
        logger.error(f"Error fetching movie {movie_id}: {e}")
        return None
