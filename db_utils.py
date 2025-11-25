import os
import logging
from urllib.parse import urlparse, quote
import psycopg2
from typing import Optional, Dict, List, Any

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

def upsert_movie_and_files(conn, title: str, description: str, qualities: Dict[str, str], aliases_str: str, movie_id: Optional[int] = None) -> Optional[int]:
    """
    Insert or update movie, its multiple quality links (movie_files), and aliases.
    Args:
        movie_id: If provided, updates the specific movie (Edit mode). If None, inserts or updates based on Title match.
    Returns movie_id or None on error.
    """
    if not title:
        return None
    
    # Extract the main generic URL if it exists in qualities
    # We use .get() then delete it from the dict copy so it doesn't get inserted into movie_files table
    qualities_copy = qualities.copy() if qualities else {}
    main_url = qualities_copy.pop('Url', '').strip()

    cur = conn.cursor()
    try:
        current_movie_id = movie_id

        if current_movie_id:
            # --- UPDATE MODE (Edit by ID) ---
            cur.execute("""
                UPDATE movies 
                SET title = %s, description = %s, url = %s 
                WHERE id = %s
            """, (title.strip(), description, main_url, current_movie_id))
        else:
            # --- INSERT/UPSERT MODE (By Title) ---
            # Now we also update 'url' on conflict
            cur.execute("""
                INSERT INTO movies (title, url, file_id, description)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (title) DO UPDATE SET 
                    description = EXCLUDED.description,
                    url = EXCLUDED.url
                RETURNING id
            """, (title.strip(), main_url, None, description))
            current_movie_id = cur.fetchone()[0]

        # Upsert qualities into movie_files table
        for quality, link in qualities_copy.items():
            link = (link or '').strip()
            if not link:
                # If link is empty, we might want to delete the entry or just skip. 
                # For now, skipping, but ideally in edit mode we might want to clear old links if cleared in UI.
                continue
            
            # Determine if it is a File ID (telegram) or a URL
            file_id_val = None
            url_val = None
            
            # Simple heuristic for Telegram File IDs (usually start with specific prefixes or are long alphanumeric)
            # You can adjust this logic based on your file ID format
            if any(link.startswith(prefix) for prefix in ("BQAC", "BAAC", "CAAC", "AQAC")):
                file_id_val = link
            else:
                url_val = link

            cur.execute("""
                INSERT INTO movie_files (movie_id, quality, file_id, url)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (movie_id, quality) DO UPDATE 
                SET file_id = EXCLUDED.file_id, url = EXCLUDED.url
            """, (current_movie_id, quality, file_id_val, url_val))

        # Add aliases
        if aliases_str:
            # First, clean up old aliases if in edit mode (optional, but good for consistency)
            if movie_id:
                 cur.execute("DELETE FROM movie_aliases WHERE movie_id = %s", (current_movie_id,))

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

def get_all_movies(conn) -> List[Dict[str, Any]]:
    """Fetch all movies for the manage page."""
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT m.id, m.title, m.url, m.description, 
                   string_agg(DISTINCT ma.alias, ', ') as aliases,
                   -- Check if qualities exist
                   MAX(CASE WHEN mf.quality = 'Low Quality' THEN mf.url || mf.file_id ELSE NULL END) as q_360,
                   MAX(CASE WHEN mf.quality = 'SD Quality' THEN mf.url || mf.file_id ELSE NULL END) as q_480,
                   MAX(CASE WHEN mf.quality = 'Standard Quality' THEN mf.url || mf.file_id ELSE NULL END) as q_720,
                   MAX(CASE WHEN mf.quality = 'HD Quality' THEN mf.url || mf.file_id ELSE NULL END) as q_1080,
                   MAX(CASE WHEN mf.quality = '4K' THEN mf.url || mf.file_id ELSE NULL END) as q_2160
            FROM movies m
            LEFT JOIN movie_aliases ma ON m.id = ma.movie_id
            LEFT JOIN movie_files mf ON m.id = mf.movie_id
            GROUP BY m.id
            ORDER BY m.id DESC
        """)
        columns = [desc[0] for desc in cur.description]
        results = []
        for row in cur.fetchall():
            results.append(dict(zip(columns, row)))
        return results
    except Exception as e:
        logger.error(f"Error fetching movies: {e}")
        return []
    finally:
        cur.close()

def get_movie_by_id(conn, movie_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single movie with its qualities mapped to template keys."""
    cur = conn.cursor()
    try:
        # Fetch basic info
        cur.execute("SELECT id, title, description, url FROM movies WHERE id = %s", (movie_id,))
        movie_row = cur.fetchone()
        if not movie_row:
            return None
        
        movie = {
            'id': movie_row[0], 
            'title': movie_row[1], 
            'description': movie_row[2], 
            'url': movie_row[3]
        }

        # Fetch aliases
        cur.execute("SELECT alias FROM movie_aliases WHERE movie_id = %s", (movie_id,))
        movie['aliases'] = ", ".join([r[0] for r in cur.fetchall()])

        # Fetch files and map to form keys (q_360, etc.)
        cur.execute("SELECT quality, file_id, url FROM movie_files WHERE movie_id = %s", (movie_id,))
        rows = cur.fetchall()
        
        # Mapping DB quality names to Form input names
        quality_map = {
            'Low Quality': 'q_360',
            'SD Quality': 'q_480',
            'Standard Quality': 'q_720',
            'HD Quality': 'q_1080',
            '4K': 'q_2160'
        }

        for q_name, f_id, f_url in rows:
            # Prefer URL if exists, else File ID, else empty
            val = f_url if f_url else f_id
            key = quality_map.get(q_name)
            if key:
                movie[key] = val

        return movie
    except Exception as e:
        logger.error(f"Error fetching movie {movie_id}: {e}")
        return None
    finally:
        cur.close()
