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
        
        # Create movies table if not exists (Basic check, usually created externally but good to have)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id SERIAL PRIMARY KEY,
                title TEXT UNIQUE NOT NULL,
                description TEXT,
                url TEXT,
                file_id TEXT,
                file_size TEXT
            );
        """)

        # Ensure file_size column exists in 'movies' table (for Default URL size)
        cur.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name='movies' AND column_name='file_size';
        """)
        if not cur.fetchone():
            cur.execute("ALTER TABLE movies ADD COLUMN IF NOT EXISTS file_size TEXT;")
            logger.info("Added file_size column to movies table.")
        
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
        
        # Check if file_size column exists in 'movie_files', if not add it
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

        # Extract Default 'Url' info from qualities if present (to save in movies table)
        default_data = qualities.pop('Url', {}) if qualities else {}
        default_link = ""
        default_size = ""
        default_file_id = None
        default_url_val = None

        if isinstance(default_data, dict):
            default_link = default_data.get('url', '').strip()
            default_size = default_data.get('size', '').strip()
        else:
            default_link = str(default_data).strip()

        # Determine if default link is File ID or URL
        if default_link:
            if any(default_link.startswith(prefix) for prefix in ("BQAC", "BAAC", "CAAC", "AQAC")):
                default_file_id = default_link
            else:
                default_url_val = default_link

        # 1. Insert or Update Movie Record (Added file_size handling)
        if current_movie_id:
            # Update existing
            # Note: We update url/file_id/size only if they are provided in the form (logic can be adjusted)
            # Here assuming we overwrite if provided, or if specifically updating via form
            cur.execute("""
                UPDATE movies 
                SET title = %s, description = %s, url = %s, file_id = %s, file_size = %s
                WHERE id = %s
            """, (title.strip(), description, default_url_val, default_file_id, default_size, current_movie_id))
        else:
            # Insert new
            cur.execute("""
                INSERT INTO movies (title, url, file_id, file_size, description)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (title) DO UPDATE SET 
                    description = EXCLUDED.description,
                    url = COALESCE(EXCLUDED.url, movies.url),
                    file_id = COALESCE(EXCLUDED.file_id, movies.file_id),
                    file_size = COALESCE(EXCLUDED.file_size, movies.file_size)
                RETURNING id
            """, (title.strip(), default_url_val, default_file_id, default_size, description))
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
                    # If link is empty, maybe we should skip, OR better: 
                    # check if we need to clear existing entry? For now, skipping empty submissions.
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
        # 👇 Updated Query: Fetches basic info + file_size of default url + file count
        cur.execute("""
            SELECT m.id, m.title, m.description,
                   (SELECT COUNT(*) FROM movie_files mf WHERE mf.movie_id = m.id) as file_count,
                   m.url, m.file_id, m.file_size
            FROM movies m 
            ORDER BY m.id DESC
        """)
        movies = cur.fetchall()
        cur.close()
        
        # Mapping for template convenience if needed (though template mostly checks existence)
        # Note: 'm.file_size' corresponds to the Default URL size
        return movies
    except Exception as e:
        logger.error(f"Error fetching movies: {e}")
        return []

def get_movie_by_id(conn, movie_id: int) -> Optional[Dict]:
    """Fetch full movie details including qualities and aliases."""
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Get basic info (including default file_size)
        cur.execute("SELECT * FROM movies WHERE id = %s", (movie_id,))
        movie = cur.fetchone()
        if not movie:
            return None

        # Get qualities/files
        cur.execute("SELECT quality, url, file_id, file_size FROM movie_files WHERE movie_id = %s", (movie_id,))
        files = cur.fetchall()
        
        # Reconstruct qualities dictionary for the form
        qualities_dict = {
            'Url': {'url': '', 'size': ''}, # Added Default Url holder
            'Low Quality': {'url': '', 'size': ''},
            'SD Quality': {'url': '', 'size': ''},
            'Standard Quality': {'url': '', 'size': ''},
            'HD Quality': {'url': '', 'size': ''},
            '4K': {'url': '', 'size': ''}
        }
        
        # Populate Default Url (from movies table)
        def_val = movie['file_id'] if movie['file_id'] else movie['url']
        def_size = movie['file_size'] if movie['file_size'] else ''
        qualities_dict['Url'] = {'url': def_val, 'size': def_size}

        # Populate other qualities (from movie_files table)
        for f in files:
            q_name = f['quality']
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
        
        # Add flat keys for template convenience (e.g., movie.q_360, movie.s_360)
        # This matches what the edit form expects: {{ movie.q_360 }} etc.
        movie_data['s_url'] = def_size # specific key for default size
        
        mapping = {
            'Low Quality': '360',
            'SD Quality': '480',
            'Standard Quality': '720',
            'HD Quality': '1080',
            '4K': '2160'
        }
        
        for q_key, suffix in mapping.items():
            data = qualities_dict.get(q_key, {})
            movie_data[f'q_{suffix}'] = data.get('url', '')
            movie_data[f's_{suffix}'] = data.get('size', '')

        movie_data['qualities'] = qualities_dict
        movie_data['aliases'] = aliases_str

        cur.close()
        return movie_data

    except Exception as e:
        logger.error(f"Error fetching movie {movie_id}: {e}")
        return None
