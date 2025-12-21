# -*- coding: utf-8 -*-
# ==================== IMPORTS ====================
import os
import threading
import asyncio
import logging
import random
import json
import requests
import signal
import sys
import re
from typing import Optional
from datetime import datetime, timedelta
from collections import defaultdict
from urllib.parse import urlparse, urlunparse, quote

import telegram
import psycopg2
from bs4 import BeautifulSoup
from flask import Flask, request, session, g
from fuzzywuzzy import process, fuzz

import google.generativeai as genai
from googleapiclient.discovery import build

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler
)

# Try to import admin views
try:
    import admin_views as admin_views_module
except ImportError:
    admin_views_module = None

try:
    import db_utils
    FIXED_DATABASE_URL = getattr(db_utils, "FIXED_DATABASE_URL", None)
except Exception:
    FIXED_DATABASE_URL = None
    db_utils = None

# ==================== LOGGING SETUP ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== GLOBAL SETS FOR BACKGROUND TASKS ====================
background_tasks = set()

# ==================== CONVERSATION STATES ====================
MAIN_MENU, SEARCHING, REQUESTING, AWAITING_REQUEST_CONFIRM = range(4)

# ==================== ENVIRONMENT VARIABLES ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DATABASE_URL = os.environ.get('DATABASE_URL')
BLOGGER_API_KEY = os.environ.get('BLOGGER_API_KEY')
BLOG_ID = os.environ.get('BLOG_ID')
UPDATE_SECRET_CODE = os.environ.get('UPDATE_SECRET_CODE', 'default_secret_123')
ADMIN_USER_ID = int(os.environ.get('ADMIN_USER_ID', 0))
GROUP_CHAT_ID = os.environ.get('GROUP_CHAT_ID')
ADMIN_CHANNEL_ID = os.environ.get('ADMIN_CHANNEL_ID')
FILMFYBOX_CHANNEL_URL = os.environ.get('FILMFYBOX_CHANNEL_URL', 'http://t.me/filmfybox')
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', '@ownermahi')  # Admin username for display

# Rate limiting dictionary
user_last_request = defaultdict(lambda: datetime.min)

# Request pending tracking (for 2 min timeout)
user_request_pending = {}  # {user_id: {'title': str, 'timestamp': datetime, 'message_id': int, 'chat_id': int}}

# Configurable settings
REQUEST_COOLDOWN_MINUTES = int(os.environ.get('REQUEST_COOLDOWN_MINUTES', '10'))
SIMILARITY_THRESHOLD = int(os.environ.get('SIMILARITY_THRESHOLD', '80'))
MAX_REQUESTS_PER_MINUTE = int(os.environ.get('MAX_REQUESTS_PER_MINUTE', '10'))
AUTO_DELETE_DELAY = int(os.environ.get('AUTO_DELETE_DELAY', '120'))  # 2 minutes default

# Validate required environment variables
if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN environment variable is not set")
    raise ValueError("TELEGRAM_BOT_TOKEN is not set.")

if not DATABASE_URL:
    logger.error("DATABASE_URL environment variable is not set")
    raise ValueError("DATABASE_URL is not set.")


# ==================== AUTO-DELETE MANAGER CLASS ====================
class AutoDeleteManager:
    """Centralized auto-delete manager for all bot messages"""
    
    def __init__(self, default_delay: int = 120):
        self.default_delay = default_delay
        self.pending_deletions = set()
    
    async def send_and_delete(
        self, 
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        text: str = None,
        document: str = None,
        photo: str = None,
        video: str = None,
        animation: str = None,
        caption: str = None,
        reply_markup=None,
        parse_mode: str = 'HTML',
        delay: int = None,
        reply_to_message_id: int = None
    ) -> Optional[telegram.Message]:
        """
        Send any type of message and auto-delete after delay.
        Returns the sent message object.
        """
        delay = delay or self.default_delay
        sent_msg = None
        
        try:
            if document:
                sent_msg = await context.bot.send_document(
                    chat_id=chat_id,
                    document=document,
                    caption=caption or text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id
                )
            elif photo:
                sent_msg = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption or text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id
                )
            elif video:
                sent_msg = await context.bot.send_video(
                    chat_id=chat_id,
                    video=video,
                    caption=caption or text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id
                )
            elif animation:
                sent_msg = await context.bot.send_animation(
                    chat_id=chat_id,
                    animation=animation,
                    caption=caption or text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id
                )
            elif text:
                sent_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id
                )
            
            if sent_msg:
                task = asyncio.create_task(
                    self._delete_after_delay(context, chat_id, sent_msg.message_id, delay)
                )
                self.pending_deletions.add(task)
                task.add_done_callback(self.pending_deletions.discard)
            
            return sent_msg
            
        except Exception as e:
            logger.error(f"AutoDeleteManager send error: {e}")
            return None
    
    async def schedule_delete(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_ids: list,
        delay: int = None
    ):
        """Schedule existing messages for deletion"""
        delay = delay or self.default_delay
        
        for msg_id in message_ids:
            task = asyncio.create_task(
                self._delete_after_delay(context, chat_id, msg_id, delay)
            )
            self.pending_deletions.add(task)
            task.add_done_callback(self.pending_deletions.discard)
    
    async def _delete_after_delay(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int,
        delay: int
    ):
        """Internal method to delete message after delay"""
        try:
            await asyncio.sleep(delay)
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            logger.info(f"✅ Auto-deleted message {message_id} from chat {chat_id}")
        except telegram.error.BadRequest as e:
            if "message to delete not found" not in str(e).lower():
                logger.error(f"Failed to delete message {message_id}: {e}")
        except Exception as e:
            logger.error(f"Auto-delete error for {message_id}: {e}")


# Initialize global auto-delete manager
auto_delete = AutoDeleteManager(default_delay=AUTO_DELETE_DELAY)


# ==================== LANGUAGE & QUALITY DETECTION ====================
def detect_language_from_filename(filename: str) -> str:
    """
    Detect language from filename patterns.
    Returns language string or empty if not detected.
    """
    if not filename:
        return ""
    
    filename_lower = filename.lower()
    
    language_patterns = {
        'Hindi': [r'hindi', r'hin\b', r'_hi_', r'\.hi\.', r'\[hindi\]', r'\(hindi\)'],
        'English': [r'english', r'eng\b', r'_en_', r'\.en\.', r'\[english\]', r'\(english\)', r'engsub'],
        'Tamil': [r'tamil', r'tam\b', r'_ta_', r'\[tamil\]', r'\(tamil\)'],
        'Telugu': [r'telugu', r'tel\b', r'_te_', r'\[telugu\]', r'\(telugu\)'],
        'Malayalam': [r'malayalam', r'mal\b', r'\[malayalam\]', r'\(malayalam\)'],
        'Kannada': [r'kannada', r'kan\b', r'\[kannada\]', r'\(kannada\)'],
        'Bengali': [r'bengali', r'ben\b', r'bangla', r'\[bengali\]', r'\(bengali\)'],
        'Marathi': [r'marathi', r'mar\b', r'\[marathi\]', r'\(marathi\)'],
        'Punjabi': [r'punjabi', r'pun\b', r'\[punjabi\]', r'\(punjabi\)'],
        'Gujarati': [r'gujarati', r'guj\b', r'\[gujarati\]', r'\(gujarati\)'],
        'Korean': [r'korean', r'kor\b', r'_ko_', r'\[korean\]', r'\(korean\)'],
        'Japanese': [r'japanese', r'jap\b', r'_ja_', r'\[japanese\]', r'\(japanese\)'],
        'Chinese': [r'chinese', r'chi\b', r'mandarin', r'\[chinese\]', r'\(chinese\)'],
        'Spanish': [r'spanish', r'spa\b', r'_es_', r'\[spanish\]', r'\(spanish\)'],
        'French': [r'french', r'fre\b', r'_fr_', r'\[french\]', r'\(french\)'],
        'German': [r'german', r'ger\b', r'_de_', r'\[german\]', r'\(german\)'],
        'Dual Audio': [r'dual\s*audio', r'dualaudio', r'dual\-audio', r'dual'],
        'Multi Audio': [r'multi\s*audio', r'multiaudio', r'multi'],
    }
    
    detected = []
    for lang, patterns in language_patterns.items():
        for pattern in patterns:
            if re.search(pattern, filename_lower):
                detected.append(lang)
                break
    
    if detected:
        return " | ".join(detected)
    return ""


def detect_quality_from_filename(filename: str) -> str:
    """Detect video quality from filename"""
    if not filename:
        return ""
    
    filename_lower = filename.lower()
    
    quality_patterns = [
        (r'2160p|4k|uhd', '4K UHD'),
        (r'1080p|fhd|fullhd|full\s*hd', '1080p FHD'),
        (r'720p|hd', '720p HD'),
        (r'480p|sd', '480p SD'),
        (r'360p', '360p'),
        (r'webrip', 'WebRip'),
        (r'bluray|bdrip|brrip', 'BluRay'),
        (r'hdcam|camrip|cam\b', 'CAMRip'),
        (r'dvdrip|dvd', 'DVDRip'),
        (r'hdrip', 'HDRip'),
        (r'webdl|web\-dl', 'WEB-DL'),
    ]
    
    for pattern, quality in quality_patterns:
        if re.search(pattern, filename_lower):
            return quality
    
    return ""


# ==================== UTILITY FUNCTIONS ====================
def preprocess_query(query):
    """Clean and normalize user query"""
    query = re.sub(r'[^\w\s-]', '', query)
    query = ' '.join(query.split())
    stop_words = ['movie', 'film', 'full', 'download', 'watch', 'online', 'free', 'hindi', 'dubbed']
    words = query.lower().split()
    words = [w for w in words if w not in stop_words]
    return ' '.join(words).strip()


async def check_rate_limit(user_id):
    """Check if user is rate limited"""
    now = datetime.now()
    last_request = user_last_request[user_id]

    if now - last_request < timedelta(seconds=2):
        return False

    user_last_request[user_id] = now
    return True


def is_valid_url(url):
    """Check if a URL is valid"""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        return False


def normalize_url(url):
    """Normalize and clean URLs"""
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        if 'blogspot.com' in url and 'import-urlhttpsfonts' in url:
            url = url.replace('import-urlhttpsfonts', 'import-url-https-fonts')

        if '#' in url:
            base, anchor = url.split('#', 1)
            parsed = urlparse(base)
            normalized_base = urlunparse((
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                parsed.query,
                ''
            ))
            url = f"{normalized_base}#{anchor}"
        else:
            parsed = urlparse(url)
            url = urlunparse((
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment
            ))

        return url
    except:
        return url


def _normalize_title_for_match(title: str) -> str:
    """Normalize title for fuzzy matching"""
    if not title:
        return ""
    t = re.sub(r'[^\w\s]', ' ', title)
    t = re.sub(r'\s+', ' ', t).strip()
    return t.lower()


def get_last_similar_request_for_user(user_id: int, title: str, minutes_window: int = None):
    """Check if user recently requested a similar movie"""
    if minutes_window is None:
        minutes_window = REQUEST_COOLDOWN_MINUTES
        
    conn = get_db_connection()
    if not conn:
        return None

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT movie_title, requested_at
            FROM user_requests
            WHERE user_id = %s
            ORDER BY requested_at DESC
            LIMIT 200
        """, (user_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            return None

        now = datetime.now()
        cutoff = now - timedelta(minutes=minutes_window)
        norm_target = _normalize_title_for_match(title)

        for stored_title, requested_at in rows:
            if not stored_title or not requested_at:
                continue
            
            try:
                if isinstance(requested_at, datetime):
                    requested_time = requested_at
                else:
                    requested_time = datetime.strptime(str(requested_at), '%Y-%m-%d %H:%M:%S')
            except Exception:
                requested_time = requested_at

            if requested_time < cutoff:
                break

            norm_stored = _normalize_title_for_match(stored_title)
            score = fuzz.token_sort_ratio(norm_target, norm_stored)
            if score >= SIMILARITY_THRESHOLD:
                return {
                    "stored_title": stored_title,
                    "requested_at": requested_time,
                    "score": score
                }

        return None
    except Exception as e:
        logger.error(f"Error checking last similar request for user {user_id}: {e}")
        try:
            conn.close()
        except:
            pass
        return None


def user_burst_count(user_id: int, window_seconds: int = 60):
    """Count how many requests this user made in the last window_seconds"""
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        since = datetime.now() - timedelta(seconds=window_seconds)
        cur.execute("SELECT COUNT(*) FROM user_requests WHERE user_id = %s AND requested_at >= %s", (user_id, since))
        cnt = cur.fetchone()[0]
        cur.close()
        conn.close()
        return cnt
    except Exception as e:
        logger.error(f"Error counting burst requests for user {user_id}: {e}")
        try:
            conn.close()
        except:
            pass
        return 0


# ==================== DATABASE FUNCTIONS ====================
def setup_database():
    """Setup database tables and indexes"""
    try:
        conn_str = FIXED_DATABASE_URL or DATABASE_URL
        conn = psycopg2.connect(conn_str)
        cur = conn.cursor()
        
        cur.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm;')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS movies (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                file_id TEXT
            )
        ''')

        cur.execute('CREATE TABLE IF NOT EXISTS sync_info (id SERIAL PRIMARY KEY, last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP);')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_requests (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                first_name TEXT,
                movie_title TEXT NOT NULL,
                requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notified BOOLEAN DEFAULT FALSE,
                group_id BIGINT,
                message_id BIGINT
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS movie_aliases (
                id SERIAL PRIMARY KEY,
                movie_id INTEGER REFERENCES movies(id) ON DELETE CASCADE,
                alias TEXT NOT NULL,
                UNIQUE(movie_id, alias)
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS movie_files (
                id SERIAL PRIMARY KEY,
                movie_id INTEGER REFERENCES movies(id) ON DELETE CASCADE,
                quality TEXT,
                url TEXT,
                file_id TEXT,
                file_size TEXT,
                language TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cur.execute('''
            DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'user_requests_unique_constraint') THEN
                ALTER TABLE user_requests ADD CONSTRAINT user_requests_unique_constraint UNIQUE (user_id, movie_title);
            END IF;
            END $$;
        ''')

        try:
            cur.execute("ALTER TABLE movies ADD COLUMN IF NOT EXISTS file_id TEXT;")
        except Exception as e:
            logger.info("file_id column already exists or couldn't be added")

        try:
            cur.execute("ALTER TABLE user_requests ADD COLUMN IF NOT EXISTS message_id BIGINT;")
        except Exception as e:
            logger.info("message_id column already exists or couldn't be added")

        try:
            cur.execute("ALTER TABLE movie_files ADD COLUMN IF NOT EXISTS language TEXT;")
        except Exception as e:
            logger.info("language column already exists or couldn't be added")

        cur.execute('CREATE INDEX IF NOT EXISTS idx_movies_title ON movies (title);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_movies_title_trgm ON movies USING gin (title gin_trgm_ops);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_user_requests_movie_title ON user_requests (movie_title);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_user_requests_user_id ON user_requests (user_id);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_movie_aliases_alias ON movie_aliases (alias);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_movie_files_movie_id ON movie_files (movie_id);')

        conn.commit()
        cur.close()
        conn.close()
        logger.info("Database setup completed successfully")
    except Exception as e:
        logger.error(f"Error setting up database: {e}")
        logger.info("Continuing without database setup...")


def get_db_connection():
    """Get database connection with error handling"""
    try:
        conn_str = FIXED_DATABASE_URL or DATABASE_URL
        if not conn_str:
            logger.error("No database URL configured")
            return None
        return psycopg2.connect(conn_str)
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        return None


def update_movies_in_db():
    """Update movies from Blogger API"""
    logger.info("Starting movie update process...")
    setup_database()

    conn = None
    cur = None
    new_movies_added = 0

    try:
        conn = get_db_connection()
        if not conn:
            return "Database connection failed"

        cur = conn.cursor()

        cur.execute("SELECT last_sync FROM sync_info ORDER BY id DESC LIMIT 1;")
        last_sync_result = cur.fetchone()
        last_sync_time = last_sync_result[0] if last_sync_result else None

        cur.execute("SELECT title FROM movies;")
        existing_movies = {row[0] for row in cur.fetchall()}

        if not BLOGGER_API_KEY or not BLOG_ID:
            return "Blogger API keys not configured"

        service = build('blogger', 'v3', developerKey=BLOGGER_API_KEY)
        all_items = []

        posts_request = service.posts().list(blogId=BLOG_ID, maxResults=500)
        while posts_request is not None:
            posts_response = posts_request.execute()
            all_items.extend(posts_response.get('items', []))
            posts_request = service.posts().list_next(posts_request, posts_response)

        pages_request = service.pages().list(blogId=BLOG_ID)
        pages_response = pages_request.execute()
        all_items.extend(pages_response.get('items', []))

        unique_titles = set()
        for item in all_items:
            title = item.get('title')
            url = item.get('url')

            if last_sync_time and 'published' in item:
                try:
                    published_time = datetime.strptime(item['published'], '%Y-%m-%dT%H:%M:%S.%fZ')
                    if published_time < last_sync_time:
                        continue
                except:
                    pass

            if title and url and title.strip() not in existing_movies and title.strip() not in unique_titles:
                try:
                    cur.execute("INSERT INTO movies (title, url) VALUES (%s, %s);", (title.strip(), url.strip()))
                    new_movies_added += 1
                    unique_titles.add(title.strip())
                except psycopg2.Error as e:
                    logger.error(f"Error inserting movie {title}: {e}")
                    conn.rollback()
                    continue

        cur.execute("INSERT INTO sync_info (last_sync) VALUES (CURRENT_TIMESTAMP);")

        conn.commit()
        return f"Update complete. Added {new_movies_added} new items."

    except Exception as e:
        logger.error(f"Error during movie update: {e}")
        if conn:
            conn.rollback()
        return f"An error occurred during update: {e}"

    finally:
        if cur: cur.close()
        if conn: conn.close()


def get_movies_from_db(user_query, limit=10):
    """Search for movies in database with fuzzy matching"""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return []

        cur = conn.cursor()

        logger.info(f"Searching for: '{user_query}'")

        cur.execute(
            "SELECT id, title, url, file_id FROM movies WHERE LOWER(title) LIKE LOWER(%s) ORDER BY title LIMIT %s",
            (f'%{user_query}%', limit)
        )
        exact_matches = cur.fetchall()

        if exact_matches:
            logger.info(f"Found {len(exact_matches)} exact matches")
            cur.close()
            conn.close()
            return exact_matches

        cur.execute("""
            SELECT DISTINCT m.id, m.title, m.url, m.file_id
            FROM movies m
            JOIN movie_aliases ma ON m.id = ma.movie_id
            WHERE LOWER(ma.alias) LIKE LOWER(%s)
            ORDER BY m.title
            LIMIT %s
        """, (f'%{user_query}%', limit))
        alias_matches = cur.fetchall()

        if alias_matches:
            logger.info(f"Found {len(alias_matches)} alias matches")
            cur.close()
            conn.close()
            return alias_matches

        cur.execute("SELECT id, title, url, file_id FROM movies")
        all_movies = cur.fetchall()

        if not all_movies:
            cur.close()
            conn.close()
            return []

        movie_titles = [movie[1] for movie in all_movies]
        movie_dict = {movie[1]: movie for movie in all_movies}

        matches = process.extract(user_query, movie_titles, scorer=fuzz.token_sort_ratio, limit=limit)

        filtered_movies = []
        for match in matches:
            if len(match) >= 2:
                title, score = match[0], match[1]
                if score >= 65 and title in movie_dict:
                    filtered_movies.append(movie_dict[title])

        logger.info(f"Found {len(filtered_movies)} fuzzy matches")

        cur.close()
        conn.close()
        return filtered_movies[:limit]

    except Exception as e:
        logger.error(f"Database query error: {e}")
        return []
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def store_user_request(user_id, username, first_name, movie_title, group_id=None, message_id=None):
    """Store user request in database"""
    try:
        conn = get_db_connection()
        if not conn:
            return False

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_requests (user_id, username, first_name, movie_title, group_id, message_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT user_requests_unique_constraint DO UPDATE
                SET requested_at = CURRENT_TIMESTAMP
        """, (user_id, username, first_name, movie_title, group_id, message_id))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error storing user request: {e}")
        try:
            conn.rollback()
            conn.close()
        except:
            pass
        return False


def get_all_movie_qualities(movie_id):
    """Fetch all available qualities for a movie"""
    conn = get_db_connection()
    if not conn:
        return []

    try:
        cur = conn.cursor()
        
        cur.execute("""
            SELECT quality, url, file_id, file_size, language
            FROM movie_files
            WHERE movie_id = %s AND (url IS NOT NULL OR file_id IS NOT NULL)
            ORDER BY id DESC
        """, (movie_id,))
        
        quality_results = cur.fetchall()

        cur.execute("SELECT url FROM movies WHERE id = %s", (movie_id,))
        main_res = cur.fetchone()
        
        final_results = []
        
        if main_res and main_res[0] and main_res[0].strip():
            final_results.append(('Stream / Watch Online', main_res[0].strip(), None, None, None))
            
        for row in quality_results:
            quality, url, file_id, file_size, language = row
            final_results.append((quality, url, file_id, file_size, language))
        
        cur.close()
        return final_results
    except Exception as e:
        logger.error(f"Error fetching movie qualities for {movie_id}: {e}")
        return []
    finally:
        if conn:
            conn.close()


# ==================== AI INTENT ANALYSIS ====================
async def analyze_intent(message_text):
    """Analyze if the message is a movie request using AI"""
    if not GEMINI_API_KEY:
        return {"is_request": True, "content_title": message_text}

    try:
        movie_keywords = ["movie", "film", "series", "watch", "download", "see", "चलचित्र", "फिल्म", "सीरीज"]
        if not any(keyword in message_text.lower() for keyword in movie_keywords):
            return {"is_request": False, "content_title": None}

        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(model_name='gemini-1.5-flash')

        prompt = f"""
        You are a 'Request Analyzer' for a Telegram bot named FilmfyBox.
        FilmfyBox's ONLY purpose is to provide MOVIES and WEB SERIES. Nothing else.

        Analyze the user's message below. Your task is to determine ONLY ONE THING:
        Is the user asking for a movie or a web series?

        - If the user IS asking for a movie or web series, respond with a JSON object:
          {{"is_request": true, "content_title": "Name of the Movie/Series"}}

        - If the user is talking about ANYTHING ELSE, respond with:
          {{"is_request": false, "content_title": null}}

        Do not explain yourself. Only provide the JSON.

        User's Message: "{message_text}"
        """

        response = await model.generate_content_async(prompt)
        json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        else:
            return {"is_request": False, "content_title": None}

    except Exception as e:
        logger.error(f"Error in AI intent analysis: {e}")
        return {"is_request": True, "content_title": message_text}


# ==================== KEYBOARD MARKUPS ====================
def get_main_keyboard():
    """Get the main menu keyboard - REQUEST BUTTON REMOVED"""
    keyboard = [
        ['🔍 Search Movies'],
        ['📊 My Stats', '❓ Help']
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def get_request_confirmation_keyboard(movie_title: str):
    """Inline keyboard for request confirmation"""
    safe_title = movie_title[:40]
    keyboard = [
        [
            InlineKeyboardButton("✅ हाँ, Request करें", callback_data=f"confirm_req_{safe_title}"),
            InlineKeyboardButton("❌ नहीं, Cancel", callback_data="cancel_req")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_admin_request_keyboard(user_id, movie_title):
    """Inline keyboard for admin actions on a user request"""
    sanitized_title = movie_title[:30]
    keyboard = [
        [InlineKeyboardButton("✅ FULFILL", callback_data=f"admin_fulfill_{user_id}_{sanitized_title}")],
        [InlineKeyboardButton("❌ IGNORE", callback_data=f"admin_delete_{user_id}_{sanitized_title}")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_movie_options_keyboard(movie_title, url):
    """Get inline keyboard for movie options"""
    keyboard = [
        [InlineKeyboardButton("🎬 Watch Now", url=url)],
        [InlineKeyboardButton("📥 Download", callback_data=f"download_{movie_title[:50]}")],
        [InlineKeyboardButton("🔗 Join Channel", url=FILMFYBOX_CHANNEL_URL)]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_movie_selection_keyboard(movies, page=0, movies_per_page=5):
    """Create inline keyboard with movie selection buttons"""
    start_idx = page * movies_per_page
    end_idx = start_idx + movies_per_page
    current_movies = movies[start_idx:end_idx]

    keyboard = []

    for movie in current_movies:
        movie_id, title, url, file_id = movie
        button_text = title if len(title) <= 40 else title[:37] + "..."
        keyboard.append([InlineKeyboardButton(
            f"🎬 {button_text}",
            callback_data=f"movie_{movie_id}"
        )])

    nav_buttons = []
    total_pages = (len(movies) + movies_per_page - 1) // movies_per_page

    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Previous", callback_data=f"page_{page-1}"))

    if end_idx < len(movies):
        nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"page_{page+1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_selection")])

    return InlineKeyboardMarkup(keyboard)


def create_quality_selection_keyboard(movie_id, title, qualities):
    """Create inline keyboard with quality selection buttons"""
    keyboard = []

    for item in qualities:
        if len(item) == 5:
            quality, url, file_id, file_size, language = item
        else:
            quality, url, file_id, file_size = item
            language = None
            
        callback_data = f"quality_{movie_id}_{quality[:30]}"
        
        if "[" in quality and "]" in quality:
            display_text = quality
        else:
            size_part = f" - {file_size}" if file_size else ""
            lang_part = f" [{language}]" if language else ""
            display_text = f"{quality}{size_part}{lang_part}"
            
        link_type = "📁" if file_id else "🔗"
        button_text = f"{link_type} {display_text}"
        
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])

    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_selection")])

    return InlineKeyboardMarkup(keyboard)


# ==================== NOTIFICATION FUNCTIONS ====================
async def send_admin_notification(context: ContextTypes.DEFAULT_TYPE, user, movie_title, group_info=None):
    """Send notification to admin channel about a new request"""
    if not ADMIN_CHANNEL_ID:
        return

    try:
        user_info = f"User: {user.first_name or 'Unknown'}"
        if user.username:
            user_info += f" (@{user.username})"
        user_info += f" (ID: {user.id})"

        group_info_text = f"From Group: {group_info}" if group_info else "Via Private Message"

        message = f"""
🎬 <b>NEW MOVIE REQUEST!</b> 🎬

<b>Movie:</b> {movie_title}
{user_info}
{group_info_text}
Time: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}
        """

        await context.bot.send_message(
            chat_id=ADMIN_CHANNEL_ID,
            text=message,
            reply_markup=get_admin_request_keyboard(user.id, movie_title),
            parse_mode='HTML'
        )

    except Exception as e:
        logger.error(f"Error sending admin notification: {e}")


async def notify_users_for_movie(context: ContextTypes.DEFAULT_TYPE, movie_title, movie_url_or_file_id):
    """Notify users who requested a movie"""
    logger.info(f"Attempting to notify users for movie: {movie_title}")
    conn = None
    cur = None
    notified_count = 0

    caption_text = (
        f"🎬 <b>{movie_title}</b>\n\n"
        f"🔗 <b>JOIN »</b> <a href='{FILMFYBOX_CHANNEL_URL}'>FilmfyBox</a>\n\n"
        "🔹 Movie का नाम भेजें, मैं जल्द से जल्द ढूंढ दूंगा। 🎬✨\n"
        f"🔹 <a href='https://t.me/Filmfybox002'>FlimfyBox Chat</a>"
    )

    join_channel_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Join Channel", url=FILMFYBOX_CHANNEL_URL)
    ]])

    try:
        conn = get_db_connection()
        if not conn:
            return 0

        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, username, first_name FROM user_requests WHERE movie_title ILIKE %s AND notified = FALSE",
            (f'%{movie_title}%',)
        )
        users_to_notify = cur.fetchall()

        for user_id, username, first_name in users_to_notify:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎉 Hey {first_name or username}! आपकी requested movie '{movie_title}' अब available है!"
                )

                warning_msg = await context.bot.send_message(
                    chat_id=user_id,
                    text="⚠️ <b>यह file 2 minute में auto-delete हो जाएगी!</b>\n👉 कृपया forward कर लें।",
                    parse_mode='HTML'
                )

                sent_msg = None

                if isinstance(movie_url_or_file_id, str) and any(movie_url_or_file_id.startswith(prefix) for prefix in ["BQAC", "BAAC", "CAAC", "AQAC"]):
                    sent_msg = await context.bot.send_document(
                        chat_id=user_id,
                        document=movie_url_or_file_id,
                        caption=caption_text,
                        parse_mode='HTML',
                        reply_markup=join_channel_keyboard
                    )
                elif isinstance(movie_url_or_file_id, str) and movie_url_or_file_id.startswith("https://t.me/c/"):
                    parts = movie_url_or_file_id.split('/')
                    from_chat_id = int("-100" + parts[-2])
                    msg_id = int(parts[-1])
                    sent_msg = await context.bot.copy_message(
                        chat_id=user_id,
                        from_chat_id=from_chat_id,
                        message_id=msg_id,
                        caption=caption_text,
                        parse_mode='HTML',
                        reply_markup=join_channel_keyboard
                    )
                elif isinstance(movie_url_or_file_id, str) and movie_url_or_file_id.startswith("http"):
                    sent_msg = await context.bot.send_message(
                        chat_id=user_id,
                        text=f"🎬 {movie_title} is now available!\n\n{caption_text}",
                        reply_markup=get_movie_options_keyboard(movie_title, movie_url_or_file_id),
                        parse_mode='HTML'
                    )
                else:
                    sent_msg = await context.bot.send_document(
                        chat_id=user_id,
                        document=movie_url_or_file_id,
                        caption=caption_text,
                        parse_mode='HTML',
                        reply_markup=join_channel_keyboard
                    )

                if sent_msg:
                    await auto_delete.schedule_delete(
                        context,
                        user_id,
                        [sent_msg.message_id, warning_msg.message_id],
                        delay=AUTO_DELETE_DELAY
                    )

                cur.execute(
                    "UPDATE user_requests SET notified = TRUE WHERE user_id = %s AND movie_title ILIKE %s",
                    (user_id, f'%{movie_title}%')
                )
                conn.commit()
                notified_count += 1
                await asyncio.sleep(0.1)

            except telegram.error.Forbidden:
                logger.error(f"User {user_id} blocked the bot")
                continue
            except Exception as e:
                logger.error(f"Error notifying user {user_id}: {e}")
                continue

        return notified_count
    except Exception as e:
        logger.error(f"Error in notify_users_for_movie: {e}")
        return 0
    finally:
        if cur: cur.close()
        if conn: conn.close()


async def notify_in_group(context: ContextTypes.DEFAULT_TYPE, movie_title):
    """Notify users in group when a requested movie becomes available"""
    logger.info(f"Attempting to notify users in group for movie: {movie_title}")
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return

        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, username, first_name, group_id, message_id FROM user_requests WHERE movie_title ILIKE %s AND notified = FALSE",
            (f'%{movie_title}%',)
        )
        users_to_notify = cur.fetchall()

        if not users_to_notify:
            return

        groups_to_notify = defaultdict(list)
        for user_id, username, first_name, group_id, message_id in users_to_notify:
            if group_id:
                groups_to_notify[group_id].append((user_id, username, first_name, message_id))

        for group_id, users in groups_to_notify.items():
            try:
                notification_text = "Hey! आपकी requested movie अब आ गई है! 🥳\n\n"
                notified_users_ids = []
                user_mentions = []
                for user_id, username, first_name, message_id in users:
                    mention = f"<a href='tg://user?id={user_id}'>{first_name or username}</a>"
                    user_mentions.append(mention)
                    notified_users_ids.append(user_id)

                notification_text += ", ".join(user_mentions)
                notification_text += f"\n\nआपकी फिल्म '{movie_title}' अब उपलब्ध है! Private chat में मुझसे बात करें।"

                await context.bot.send_message(
                    chat_id=group_id,
                    text=notification_text,
                    parse_mode='HTML'
                )

                for user_id in notified_users_ids:
                    cur.execute(
                        "UPDATE user_requests SET notified = TRUE WHERE user_id = %s AND movie_title ILIKE %s",
                        (user_id, f'%{movie_title}%')
                    )
                conn.commit()

            except Exception as e:
                logger.error(f"Failed to send message to group {group_id}: {e}")
                continue

    except Exception as e:
        logger.error(f"Error in notify_in_group: {e}")
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ==================== REQUEST CLEANUP FUNCTION ====================
async def cleanup_pending_request(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Clean up pending request after timeout (2 minutes)"""
    await asyncio.sleep(AUTO_DELETE_DELAY)
    
    if user_id in user_request_pending:
        pending = user_request_pending.pop(user_id, None)
        if pending and pending.get('message_id') and pending.get('chat_id'):
            try:
                await context.bot.edit_message_text(
                    chat_id=pending['chat_id'],
                    message_id=pending['message_id'],
                    text="⏰ Request timeout हो गई।\n\n🔍 कृपया फिर से search करें।"
                )
            except Exception as e:
                logger.error(f"Error editing timeout message: {e}")
        logger.info(f"Cleaned up pending request for user {user_id}")


# ==================== SEND MOVIE TO USER ====================
async def send_movie_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: int, title: str, url: Optional[str] = None, file_id: Optional[str] = None):
    """Sends movie with auto-delete and language detection"""
    
    if update.callback_query:
        chat_id = update.callback_query.message.chat_id
    elif update.effective_chat:
        chat_id = update.effective_chat.id
    else:
        logger.error("Could not determine chat_id")
        return

    if not url and not file_id:
        qualities = get_all_movie_qualities(movie_id)
        if qualities:
            context.user_data['selected_movie_data'] = {
                'id': movie_id,
                'title': title,
                'qualities': qualities
            }
            keyboard = create_quality_selection_keyboard(movie_id, title, qualities)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ <b>{title}</b> found!\n\n⬇️ <b>Quality चुनें:</b>",
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            return
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ '{title}' के लिए कोई file नहीं मिली।"
            )
            return

    try:
        detected_language = ""
        detected_quality = ""
        
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT quality, file_size, language FROM movie_files 
                    WHERE movie_id = %s AND (file_id = %s OR url = %s)
                    LIMIT 1
                """, (movie_id, file_id, url))
                file_info = cur.fetchone()
                cur.close()
                conn.close()
                
                if file_info:
                    quality_str = file_info[0] or ""
                    db_language = file_info[2] or ""
                    
                    if db_language:
                        detected_language = db_language
                    else:
                        detected_language = detect_language_from_filename(quality_str)
                    detected_quality = detect_quality_from_filename(quality_str)
            except Exception as e:
                logger.error(f"Error getting file info: {e}")
        
        if not detected_language:
            detected_language = detect_language_from_filename(title)
        if not detected_language and file_id:
            detected_language = detect_language_from_filename(file_id)
        
        language_line = f"🗣️ <b>Language:</b> {detected_language}\n" if detected_language else ""
        quality_line = f"📺 <b>Quality:</b> {detected_quality}\n" if detected_quality else ""
        
        caption_text = (
            f"🎬 <b>{title}</b>\n\n"
            f"{language_line}"
            f"{quality_line}"
            f"🔗 <b>JOIN »</b> <a href='{FILMFYBOX_CHANNEL_URL}'>FilmfyBox</a>\n\n"
            f"🔹 Movie का नाम भेजें, मैं जल्द से जल्द ढूंढ दूंगा। 🎬✨\n"
            f"🔹 <a href='https://t.me/Filmfybox002'>FlimfyBox Chat</a>"
        )
        
        join_keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔗 Join Channel", url=FILMFYBOX_CHANNEL_URL)
        ]])

        warning_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ <b>यह file 2 minute में auto-delete हो जाएगी!</b>\n\n👉 कृपया इसे किसी दूसरे chat में forward कर लें।",
            parse_mode='HTML'
        )

        sent_msg = None

        if file_id:
            sent_msg = await context.bot.send_document(
                chat_id=chat_id,
                document=file_id,
                caption=caption_text,
                parse_mode='HTML',
                reply_markup=join_keyboard
            )

        elif url and url.startswith("https://t.me/c/"):
            try:
                parts = url.rstrip('/').split('/')
                from_chat_id = int("-100" + parts[-2])
                message_id = int(parts[-1])
                sent_msg = await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                    caption=caption_text,
                    parse_mode='HTML',
                    reply_markup=join_keyboard
                )
            except Exception as e:
                logger.error(f"Copy private link failed: {e}")
                sent_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🎬 <b>{title}</b>\n\n{caption_text}",
                    reply_markup=get_movie_options_keyboard(title, url),
                    parse_mode='HTML'
                )

        elif url and url.startswith("https://t.me/") and "/c/" not in url:
            try:
                parts = url.rstrip('/').split('/')
                username = parts[-2].lstrip("@")
                message_id = int(parts[-1])
                sent_msg = await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=f"@{username}",
                    message_id=message_id,
                    caption=caption_text,
                    parse_mode='HTML',
                    reply_markup=join_keyboard
                )
            except Exception as e:
                logger.error(f"Copy public link failed: {e}")
                sent_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🎬 <b>{title}</b>\n\n{caption_text}",
                    reply_markup=get_movie_options_keyboard(title, url),
                    parse_mode='HTML'
                )

        elif url and url.startswith("http"):
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"🎉 <b>{title}</b> available!\n\n{caption_text}",
                reply_markup=get_movie_options_keyboard(title, url),
                parse_mode='HTML'
            )

        else:
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ '{title}' found but no valid file attached."
            )

        if sent_msg:
            await auto_delete.schedule_delete(
                context,
                chat_id,
                [warning_msg.message_id, sent_msg.message_id],
                delay=AUTO_DELETE_DELAY
            )

    except Exception as e:
        logger.error(f"Error sending movie: {e}")
        try:
            await context.bot.send_message(
                chat_id=chat_id, 
                text="❌ File भेजने में error। कृपया Admin को report करें।"
            )
        except:
            pass


async def deliver_movie_on_start(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: int):
    """Background task to fetch and send movie via Deep Link"""
    chat_id = update.effective_chat.id
    
    status_msg = None
    try:
        status_msg = await context.bot.send_message(chat_id, "⚡ Finding your movie...")
    except:
        pass

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            if status_msg: 
                try: await status_msg.delete()
                except: pass
            await context.bot.send_message(chat_id, "❌ Database connection failed.")
            return

        cur = conn.cursor()
        cur.execute("SELECT title, url, file_id FROM movies WHERE id = %s", (movie_id,))
        movie_data = cur.fetchone()
        cur.close()
        conn.close()

        if status_msg:
            try: await status_msg.delete()
            except: pass

        if movie_data:
            title, url, file_id = movie_data
            await send_movie_to_user(update, context, movie_id, title, url, file_id)
        else:
            await context.bot.send_message(chat_id, "❌ Movie not found (Link Expired or Deleted).")

    except Exception as e:
        logger.error(f"Error in deliver_movie_on_start: {e}")
        if status_msg:
            try: await status_msg.delete()
            except: pass
        await context.bot.send_message(chat_id, "❌ Error sending movie.")


# ==================== COMMAND HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    try:
        user_id = update.effective_user.id
        
        if user_id in user_request_pending:
            del user_request_pending[user_id]
        
        context.user_data.clear()

        if context.args and len(context.args) > 0 and context.args[0]:
            payload = context.args[0]
            
            if payload.startswith("movie_"):
                try:
                    movie_id = int(payload.split('_')[1])
                    task = asyncio.create_task(deliver_movie_on_start(update, context, movie_id))
                    background_tasks.add(task)
                    task.add_done_callback(background_tasks.discard)
                    return MAIN_MENU
                except (IndexError, ValueError) as e:
                    logger.error(f"Error processing movie link: {e}")
                    await update.message.reply_text("❌ Invalid movie link.")
                    return MAIN_MENU
            
            elif payload.startswith("q_"):
                try:
                    query_text = payload.replace("q_", "", 1).replace("_", " ").strip()
                    
                    if not query_text:
                        await update.message.reply_text("❌ Invalid search query.")
                        return MAIN_MENU
                    
                    movies_found = get_movies_from_db(query_text, limit=10)
                    
                    if not movies_found:
                        keyboard = InlineKeyboardMarkup([
                            [InlineKeyboardButton(f"📩 '{query_text[:30]}' Request करें", callback_data=f"ask_request_{query_text[:40]}")]
                        ])
                        await update.message.reply_text(
                            f"😕 '{query_text}' नहीं मिली।\n\nRequest करना चाहेंगे?",
                            reply_markup=keyboard
                        )
                        return MAIN_MENU
                    
                    elif len(movies_found) == 1:
                        movie_id, title, url, file_id = movies_found[0]
                        await send_movie_to_user(update, context, movie_id, title, url, file_id)
                        return MAIN_MENU
                    
                    else:
                        context.user_data['search_results'] = movies_found
                        context.user_data['search_query'] = query_text
                        keyboard = create_movie_selection_keyboard(movies_found, page=0)
                        await update.message.reply_text(
                            f"🎬 <b>{len(movies_found)} results for '{query_text}'</b>\n\nSelect:",
                            reply_markup=keyboard,
                            parse_mode='HTML'
                        )
                        return MAIN_MENU
                    
                except Exception as e:
                    logger.error(f"Error in q_ deep link: {e}")
                    await update.message.reply_text("❌ Search error.")
                    return MAIN_MENU

    except Exception as e:
        logger.error(f"Error in start: {e}")

    welcome_text = """
📨 <b>Movie या Series का नाम भेजें</b> (Google जैसी spelling में)

⚠️ <b>Example:</b>
👉 <code>Jailer 2023</code>
👉 <code>Stranger Things S02</code>

❌ Emoji और symbols use न करें!
"""
    await update.message.reply_text(welcome_text, reply_markup=get_main_keyboard(), parse_mode='HTML')
    return MAIN_MENU


async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main menu options"""
    try:
        query = update.message.text
        user_id = update.effective_user.id
        
        if user_id in user_request_pending:
            del user_request_pending[user_id]

        if query == '🔍 Search Movies':
            await update.message.reply_text("🎬 Movie का नाम बताएं जो search करनी है:")
            return SEARCHING

        elif query == '📊 My Stats':
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT COUNT(*) FROM user_requests WHERE user_id = %s", (user_id,))
                    request_count = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM user_requests WHERE user_id = %s AND notified = TRUE", (user_id,))
                    fulfilled_count = cur.fetchone()[0]
                    cur.close()
                    conn.close()

                    stats_text = f"""
📊 <b>Your Stats:</b>

📝 Total Requests: {request_count}
✅ Fulfilled: {fulfilled_count}
⏳ Pending: {request_count - fulfilled_count}
"""
                    await update.message.reply_text(stats_text, parse_mode='HTML')
                except Exception as e:
                    logger.error(f"Error getting stats: {e}")
                    await update.message.reply_text("❌ Stats load करने में error.")
            else:
                await update.message.reply_text("❌ Database error.")
            return MAIN_MENU

        elif query == '❓ Help':
            help_text = """
🤖 <b>FilmfyBox Bot Help</b>

🔍 <b>Search Movies:</b> Movie का नाम type करें
📊 <b>My Stats:</b> अपनी requests देखें

<b>Tips:</b>
• सही spelling use करें
• साल भी लिखें (जैसे: KGF 2022)
• Emoji/symbols न use करें
"""
            await update.message.reply_text(help_text, parse_mode='HTML')
            return MAIN_MENU
            
        else:
            return await search_movies(update, context)

    except Exception as e:
        logger.error(f"Error in main menu: {e}")
        return MAIN_MENU


async def search_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle movie search"""
    try:
        if not await check_rate_limit(update.effective_user.id):
            await update.message.reply_text("⚠️ Please wait a moment before searching again.")
            return SEARCHING

        user_message = update.message.text.strip()
        user_id = update.effective_user.id
        
        if user_id in user_request_pending:
            del user_request_pending[user_id]
        
        processed_query = preprocess_query(user_message) if user_message else user_message
        search_query = processed_query if processed_query else user_message

        movies_found = get_movies_from_db(search_query, limit=10)

        if not movies_found:
            if update.effective_chat.type != "private":
                return MAIN_MENU

            context.user_data['potential_request'] = user_message
            
            not_found_text = (
                "😕 माफ़ करें, मुझे कोई मिलती-जुलती फ़िल्म नहीं मिली\n\n"
                "<a href='https://www.google.com/'>𝗚𝗼𝗼𝗴𝗹𝗲</a> ☜ सर्च करें..!!\n\n"
                "मूवी की स्पेलिंग गूगल पर सर्च करके, कॉपी करे, उसके बाद यहां टाइप करें।✔️\n\n"
                "बस मूवी का नाम + वर्ष लिखें, उसके आगे पीछे कुछ भी ना लिखे..।♻️\n\n"
                "✐ᝰ <b>𝗘𝘅𝗮𝗺𝗽𝗹𝗲</b>\n\n"
                "─────────────────────\n"
                "𝑲𝒈𝒇 𝟐 ✔️  |  𝑲𝒈𝒇 𝟐 𝑴𝒐𝒗𝒊𝒆 ❌\n"
                "─────────────────────\n"
                "𝑨𝒔𝒖𝒓 𝑺𝟎𝟏 𝑬𝟎𝟑 ✔️  |  𝑨𝒔𝒖𝒓 𝑺𝒆𝒂𝒔𝒐𝒏𝟑 ❌\n"
                "─────────────────────\n\n"
                "अगर फिर भी न मिले तो नीचे request करें 👇"
            )
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"📩 '{user_message[:25]}...' Request करें" if len(user_message) > 25 else f"📩 '{user_message}' Request करें", 
                    callback_data=f"ask_request_{user_message[:40]}"
                )]
            ])
            
            try:
                await auto_delete.send_and_delete(
                    context,
                    update.effective_chat.id,
                    animation='CgACAgQAAxkBAAECz0ppEaLwgDbNfPPFl5lgtFjjmztKKgAC5wIAAmaoDVMH7bkdAqNVnDYE',
                    caption="🎬 <b>Movie Search Tips</b> 🔍",
                    parse_mode='HTML',
                    delay=AUTO_DELETE_DELAY
                )
            except Exception as e:
                logger.error(f"Failed to send GIF: {e}")
            
            await auto_delete.send_and_delete(
                context,
                update.effective_chat.id,
                text=not_found_text,
                reply_markup=keyboard,
                parse_mode='HTML',
                delay=AUTO_DELETE_DELAY
            )
            
            return MAIN_MENU

        elif len(movies_found) == 1:
            movie_id, title, url, file_id = movies_found[0]
            
            qualities = get_all_movie_qualities(movie_id)
            
            if len(qualities) > 1:
                context.user_data['selected_movie_data'] = {
                    'id': movie_id,
                    'title': title,
                    'qualities': qualities
                }
                selection_text = f"✅ <b>{title}</b> found!\n\n⬇️ <b>Quality चुनें:</b>"
                keyboard = create_quality_selection_keyboard(movie_id, title, qualities)
                await update.message.reply_text(selection_text, reply_markup=keyboard, parse_mode='HTML')
            else:
                await send_movie_to_user(update, context, movie_id, title, url, file_id)

        else:
            context.user_data['search_results'] = movies_found
            context.user_data['search_query'] = user_message

            selection_text = f"🎬 <b>Found {len(movies_found)} movies matching '{user_message}'</b>\n\nPlease select:"
            keyboard = create_movie_selection_keyboard(movies_found, page=0)

            await update.message.reply_text(selection_text, reply_markup=keyboard, parse_mode='HTML')

        return MAIN_MENU

    except Exception as e:
        logger.error(f"Error in search movies: {e}")
        if update.effective_chat.type == "private":
            await update.message.reply_text("Sorry, something went wrong. Please try again.")
        return MAIN_MENU


async def request_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle direct movie request from conversation state"""
    try:
        user_message = (update.message.text or "").strip()
        user = update.effective_user

        if not user_message:
            await update.message.reply_text("कृपया मूवी का नाम भेजें।")
            return REQUESTING

        burst = user_burst_count(user.id, window_seconds=60)
        if burst >= MAX_REQUESTS_PER_MINUTE:
            await update.message.reply_text(
                "🛑 आप बहुत जल्दी-जल्दी requests भेज रहे हो। कुछ देर रुकें।"
            )
            return REQUESTING

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"📩 '{user_message[:25]}' Request करें", 
                callback_data=f"ask_request_{user_message[:40]}"
            )]
        ])
        
        await update.message.reply_text(
            f"क्या आप <b>'{user_message}'</b> को request करना चाहते हैं?",
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        
        return MAIN_MENU

    except Exception as e:
        logger.error(f"Error in request_movie: {e}")
        await update.message.reply_text("Sorry, an error occurred.")
        return REQUESTING


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel - clears all pending states"""
    user_id = update.effective_user.id
    
    if user_id in user_request_pending:
        del user_request_pending[user_id]
    
    context.user_data.clear()
    
    await update.message.reply_text(
        "❌ Cancelled। आप फिर से movie search कर सकते हैं।", 
        reply_markup=get_main_keyboard()
    )
    return MAIN_MENU


# ==================== GROUP MESSAGE HANDLER ====================
async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Listen to group messages and suggest movies"""
    if not update.message or not update.message.text or update.message.from_user.is_bot:
        return

    message_text = update.message.text.strip()
    user = update.effective_user

    if len(message_text) < 4 or message_text.startswith('/'):
        return

    movies_found = get_movies_from_db(message_text, limit=1)

    if movies_found:
        match_title = movies_found[0][1]
        score = fuzz.token_sort_ratio(_normalize_title_for_match(message_text), _normalize_title_for_match(match_title))

        if score > 85:
            movie_id, title, _, _ = movies_found[0]

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, get this movie", callback_data=f"group_get_{movie_id}_{user.id}")
            ]])

            reply_msg = await update.message.reply_text(
                text=f"Hey {user.mention_html()}, are you looking for <b>{title}</b>? Click the button to get it.",
                reply_markup=keyboard,
                parse_mode='HTML'
            )

            await auto_delete.schedule_delete(
                context, 
                update.effective_chat.id, 
                [reply_msg.message_id], 
                delay=AUTO_DELETE_DELAY
            )


# ==================== BUTTON CALLBACK HANDLER ====================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all inline button callbacks"""
    try:
        query = update.callback_query
        await query.answer()
        user = query.from_user
        user_id = user.id

        # ==================== REQUEST FLOW ====================
        
        if query.data.startswith("ask_request_"):
            movie_title = query.data.replace("ask_request_", "")
            
            if user_id in user_request_pending:
                await query.answer("पहले से एक request pending है!", show_alert=True)
                return
            
            confirm_text = (
                f"🎬 <b>Request Confirmation</b>\n\n"
                f"क्या आप <b>'{movie_title}'</b> को request करना चाहते हैं?\n\n"
                f"⚠️ <i>यह message 2 minute में expire हो जाएगा जल्दी करे</i>"
            )
            
            keyboard = get_request_confirmation_keyboard(movie_title)
            
            msg = await query.edit_message_text(
                text=confirm_text,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
            user_request_pending[user_id] = {
                'title': movie_title,
                'timestamp': datetime.now(),
                'message_id': msg.message_id,
                'chat_id': query.message.chat_id
            }
            
            task = asyncio.create_task(cleanup_pending_request(context, user_id))
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
            
            return

        elif query.data.startswith("confirm_req_"):
            movie_title = query.data.replace("confirm_req_", "")
            
            pending = user_request_pending.pop(user_id, None)
            if pending:
                movie_title = pending.get('title', movie_title)
            
            similar = get_last_similar_request_for_user(user_id, movie_title)
            if similar:
                elapsed = datetime.now() - similar['requested_at']
                minutes_left = max(0, REQUEST_COOLDOWN_MINUTES - int(elapsed.total_seconds() / 60))
                if minutes_left > 0:
                    await query.edit_message_text(
                        f"🛑 आपने हाल ही में इसी movie की request की थी।\n\n"
                        f"Similar request: \"{similar['stored_title']}\"\n"
                        f"कृपया {minutes_left} minute बाद try करें।"
                    )
                    return
            
            stored = store_user_request(
                user_id,
                user.username,
                user.first_name,
                movie_title,
                query.message.chat_id if query.message.chat.type != "private" else None,
                query.message.message_id
            )
            
            if stored:
                await send_admin_notification(context, user, movie_title)
                
                success_text = (
                    f"✅ <b>Request सफलतापूर्वक दर्ज!</b>\n\n"
                    f"🎬 Movie: <b>{movie_title}</b>\n\n"
                    f"📨 आपकी request Admin {ADMIN_USERNAME} को भेज दी गई है।\n"
                    f"⏳ जैसे ही यह उपलब्ध होगी, Admin आपको तुरंत सूचित कर देंगे।\n\n"
                    f"💡 <i>Tip: सही spelling से search करें तो जल्दी मिल सकती है!</i>"
                )
                
                await query.edit_message_text(text=success_text, parse_mode='HTML')
                
                await auto_delete.schedule_delete(
                    context, 
                    query.message.chat_id, 
                    [query.message.message_id],
                    delay=AUTO_DELETE_DELAY
                )
            else:
                await query.edit_message_text("❌ Request save करने में error। कृपया फिर से try करें।")
            
            return

        elif query.data == "cancel_req":
            user_request_pending.pop(user_id, None)
            
            await query.edit_message_text(
                "❌ Request cancel कर दी गई।\n\n"
                "🔍 आप नई movie search कर सकते हैं।"
            )
            return

        # ==================== GROUP GET ====================
        
        if query.data.startswith("group_get_"):
            try:
                parts = query.data.split('_')
                if len(parts) != 4:
                    raise ValueError("Invalid callback data format")
                
                movie_id = int(parts[2])
                original_user_id = int(parts[3])

            except (ValueError, IndexError) as e:
                logger.error(f"Error parsing group_get callback: {e}")
                await query.edit_message_text("❌ Error: Invalid button data.")
                return

            if query.from_user.id != original_user_id:
                await query.answer("This button is not for you.", show_alert=True)
                return

            try:
                conn = get_db_connection()
                if not conn:
                    await query.edit_message_text("❌ Database error. Please try again.")
                    return
                    
                cur = conn.cursor()
                cur.execute("SELECT title, url, file_id FROM movies WHERE id = %s", (movie_id,))
                movie_data = cur.fetchone()
                cur.close()
                conn.close()

                if movie_data:
                    title, url, file_id = movie_data
                    
                    dummy_update = Update(
                        update_id=0, 
                        message=telegram.Message(
                            message_id=0, 
                            date=datetime.now(), 
                            chat=telegram.Chat(id=original_user_id, type='private')
                        )
                    )
                    
                    await send_movie_to_user(dummy_update, context, movie_id, title, url, file_id)
                    await query.edit_message_text(f"✅ '{title}' आपको private chat में भेज दी गई!")
                
                else:
                    await query.edit_message_text("❌ Movie data नहीं मिला।")
            
            except telegram.error.Forbidden:
                bot_username = (await context.bot.get_me()).username
                deep_link = f"https://t.me/{bot_username}?start=movie_{movie_id}" 
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🤖 1. Start Chat", url=deep_link),
                    InlineKeyboardButton("🔄 2. Try Again", callback_data=query.data)
                ]])
                
                await query.edit_message_text(
                    "❌ <b>मैं आपको message नहीं भेज सकता!</b>\n\n"
                    "पहले मुझसे private chat में बात करें (Button 1), फिर Button 2 दबाएं।",
                    reply_markup=keyboard,
                    parse_mode='HTML'
                )
            
            except Exception as e:
                logger.error(f"Error in group_get: {e}")
                await query.edit_message_text("❌ Error sending file.")
            
            return

        # ==================== MOVIE SELECTION ====================
        
        if query.data.startswith("movie_"):
            movie_id = int(query.data.replace("movie_", ""))
            
            conn = get_db_connection()
            if not conn:
                await query.edit_message_text("❌ Database error.")
                return
                
            cur = conn.cursor()
            cur.execute("SELECT title FROM movies WHERE id = %s", (movie_id,))
            res = cur.fetchone()
            cur.close()
            conn.close()
            
            title = res[0] if res else "Movie"

            qualities = get_all_movie_qualities(movie_id)

            if not qualities:
                await query.edit_message_text("❌ इस movie के लिए कोई file नहीं मिली।")
                return

            if len(qualities) == 1:
                item = qualities[0]
                if len(item) == 5:
                    quality_name, url, file_id, _, _ = item
                else:
                    quality_name, url, file_id, _ = item
                await query.edit_message_text(f"✅ <b>{title}</b>\n\nSending {quality_name}...", parse_mode='HTML')
                await send_movie_to_user(update, context, movie_id, title, url, file_id)
                return

            context.user_data['selected_movie_data'] = {
                'id': movie_id,
                'title': title,
                'qualities': qualities
            }

            selection_text = f"✅ <b>{title}</b>\n\n⬇️ <b>Quality चुनें:</b>"
            keyboard = create_quality_selection_keyboard(movie_id, title, qualities)
            await query.edit_message_text(selection_text, reply_markup=keyboard, parse_mode='HTML')
            return

        # ==================== QUALITY SELECTION ====================
        
        if query.data.startswith("quality_"):
            parts = query.data.split('_', 2)
            movie_id = int(parts[1])
            selected_quality = parts[2] if len(parts) > 2 else ""

            movie_data = context.user_data.get('selected_movie_data')

            if not movie_data or movie_data.get('id') != movie_id:
                qualities = get_all_movie_qualities(movie_id)
                movie_data = {'id': movie_id, 'title': 'Movie', 'qualities': qualities}

            if not movie_data or 'qualities' not in movie_data:
                await query.edit_message_text("❌ Error: Please search again.")
                return

            chosen_file = None
            for item in movie_data['qualities']:
                if len(item) == 5:
                    quality, url, file_id, file_size, language = item
                else:
                    quality, url, file_id, file_size = item
                    
                if quality == selected_quality or quality.startswith(selected_quality):
                    chosen_file = {'url': url, 'file_id': file_id}
                    break

            if not chosen_file:
                await query.edit_message_text("❌ File नहीं मिली।")
                return

            title = movie_data['title']
            await query.edit_message_text(f"📤 Sending <b>{title}</b>...", parse_mode='HTML')

            await send_movie_to_user(update, context, movie_id, title, chosen_file['url'], chosen_file['file_id'])

            context.user_data.pop('selected_movie_data', None)
            return

        # ==================== PAGE NAVIGATION ====================
        
        if query.data.startswith("page_"):
            page = int(query.data.replace("page_", ""))
            if 'search_results' not in context.user_data:
                await query.edit_message_text("❌ Search expired. Please search again.")
                return

            movies = context.user_data['search_results']
            search_query = context.user_data.get('search_query', 'your search')
            selection_text = f"🎬 <b>Found {len(movies)} movies for '{search_query}'</b>\n\nSelect:"
            keyboard = create_movie_selection_keyboard(movies, page=page)
            await query.edit_message_text(selection_text, reply_markup=keyboard, parse_mode='HTML')
            return

        # ==================== CANCEL SELECTION ====================
        
        if query.data == "cancel_selection":
            await query.edit_message_text("❌ Selection cancelled.")
            context.user_data.pop('search_results', None)
            context.user_data.pop('search_query', None)
            context.user_data.pop('selected_movie_data', None)
            return

        # ==================== DOWNLOAD ====================
        
        if query.data.startswith("download_"):
            movie_title = query.data.replace("download_", "")
            conn = get_db_connection()
            if not conn:
                await query.answer("❌ Database error.", show_alert=True)
                return
            cur = conn.cursor()
            cur.execute("SELECT id, title, url, file_id FROM movies WHERE title ILIKE %s LIMIT 1", (f'%{movie_title}%',))
            movie = cur.fetchone()
            cur.close()
            conn.close()

            if movie:
                movie_id, title, url, file_id = movie
                
                qualities = get_all_movie_qualities(movie_id)
                if len(qualities) > 1:
                    context.user_data['selected_movie_data'] = {'id': movie_id, 'title': title, 'qualities': qualities}
                    selection_text = f"✅ <b>{title}</b>\n\n⬇️ <b>Quality चुनें:</b>"
                    keyboard = create_quality_selection_keyboard(movie_id, title, qualities)
                    await query.edit_message_text(selection_text, reply_markup=keyboard, parse_mode='HTML')
                else:
                    await send_movie_to_user(update, context, movie_id, title, url, file_id)
            else:
                await query.answer("❌ Movie नहीं मिली।", show_alert=True)
            return

        # ==================== ADMIN HANDLERS ====================
        
        if query.data.startswith("admin_fulfill_"):
            parts = query.data.split('_', 3)
            if len(parts) >= 4:
                target_user_id = int(parts[2])
                movie_title = parts[3]

                conn = get_db_connection()
                if conn:
                    cur = conn.cursor()
                    cur.execute("SELECT id, url, file_id FROM movies WHERE title ILIKE %s LIMIT 1", (f'%{movie_title}%',))
                    movie_data = cur.fetchone()

                    if movie_data:
                        _, url, file_id = movie_data
                        value_to_send = file_id if file_id else url
                        num_notified = await notify_users_for_movie(context, movie_title, value_to_send)
                        await notify_in_group(context, movie_title)
                        await query.edit_message_text(f"✅ FULFILLED: '{movie_title}'\n\n{num_notified} users को notify किया।")
                    else:
                        await query.edit_message_text(f"❌ Movie '{movie_title}' database में नहीं मिली।")
                    cur.close()
                    conn.close()
                else:
                    await query.edit_message_text("❌ Database error.")
            return

        if query.data.startswith("admin_delete_"):
            parts = query.data.split('_', 3)
            if len(parts) >= 4:
                target_user_id = int(parts[2])
                movie_title = parts[3]

                conn = get_db_connection()
                if conn:
                    cur = conn.cursor()
                    cur.execute("DELETE FROM user_requests WHERE user_id = %s AND movie_title ILIKE %s", (target_user_id, f'%{movie_title}%'))
                    conn.commit()
                    cur.close()
                    conn.close()
                    await query.edit_message_text(f"❌ DELETED: Request for '{movie_title}' removed.")
                else:
                    await query.edit_message_text("❌ Database error.")
            return

    except Exception as e:
        logger.error(f"Error in button callback: {e}")
        try:
            await query.answer(f"❌ Error occurred", show_alert=True)
        except:
            pass


# ==================== ADMIN COMMANDS ====================
async def add_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to add a movie"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    conn = None
    try:
        parts = context.args
        if len(parts) < 2:
            await update.message.reply_text("Format: /addmovie Title [File ID या Link]")
            return

        value = parts[-1]
        title = " ".join(parts[:-1])

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()

        if any(value.startswith(prefix) for prefix in ["BQAC", "BAAC", "CAAC", "AQAC"]):
            cur.execute(
                "INSERT INTO movies (title, url, file_id) VALUES (%s, %s, %s) ON CONFLICT (title) DO UPDATE SET url = EXCLUDED.url, file_id = EXCLUDED.file_id",
                (title.strip(), "", value.strip())
            )
            message = f"✅ '{title}' added with file ID."

        elif "http" in value or "." in value:
            if not value.startswith(('http://', 'https://')):
                await update.message.reply_text("❌ URL must start with http:// or https://")
                return

            cur.execute(
                "INSERT INTO movies (title, url, file_id) VALUES (%s, %s, NULL) ON CONFLICT (title) DO UPDATE SET url = EXCLUDED.url, file_id = NULL",
                (title.strip(), value.strip())
            )
            message = f"✅ '{title}' added with URL."

        else:
            await update.message.reply_text("❌ Invalid format.")
            return

        conn.commit()
        await update.message.reply_text(message)

        cur.execute("SELECT id, title, url, file_id FROM movies WHERE title = %s", (title.strip(),))
        movie_found = cur.fetchone()

        if movie_found:
            movie_id, title, url, file_id = movie_found
            value_to_send = file_id if file_id else url
            num_notified = await notify_users_for_movie(context, title, value_to_send)
            await notify_in_group(context, title)
            await update.message.reply_text(f"{num_notified} users को notify किया गया।")

    except Exception as e:
        logger.error(f"Error in add_movie: {e}")
        await update.message.reply_text(f"Error: {e}")
    finally:
        if conn:
            conn.close()


async def bulk_add_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add multiple movies at once"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    try:
        full_text = update.message.text
        lines = full_text.split('\n')

        if len(lines) <= 1 and not context.args:
            await update.message.reply_text("""
Format:
/bulkadd
Movie1 https://link1.com
Movie2 file_id_here
""")
            return

        success_count = 0
        failed_count = 0
        results = []

        for line in lines:
            line = line.strip()
            if not line or line.startswith('/bulkadd'):
                continue

            parts = line.split()
            if len(parts) < 2:
                failed_count += 1
                continue

            url_or_id = parts[-1]
            title = ' '.join(parts[:-1])

            try:
                conn = get_db_connection()
                if not conn:
                    failed_count += 1
                    continue

                cur = conn.cursor()

                if any(url_or_id.startswith(prefix) for prefix in ["BQAC", "BAAC", "CAAC", "AQAC"]):
                    cur.execute(
                        "INSERT INTO movies (title, url, file_id) VALUES (%s, %s, %s) ON CONFLICT (title) DO UPDATE SET url = EXCLUDED.url, file_id = EXCLUDED.file_id",
                        (title.strip(), "", url_or_id.strip())
                    )
                else:
                    normalized_url = normalize_url(url_or_id)
                    cur.execute(
                        "INSERT INTO movies (title, url, file_id) VALUES (%s, %s, NULL) ON CONFLICT (title) DO UPDATE SET url = EXCLUDED.url, file_id = NULL",
                        (title.strip(), normalized_url.strip())
                    )

                conn.commit()
                conn.close()
                success_count += 1
                results.append(f"✅ {title}")
            except Exception as 
            except Exception as e:
                failed_count += 1
                results.append(f"❌ {title} - {str(e)}")

        result_message = f"""
📊 Bulk Add Results:

✅ Success: {success_count}
❌ Failed: {failed_count}

Details:
""" + "\n".join(results[:15])

        if len(results) > 15:
            result_message += f"\n\n... और {len(results) - 15} more items"

        await update.message.reply_text(result_message)

    except Exception as e:
        logger.error(f"Error in bulk_add_movies: {e}")
        await update.message.reply_text(f"Error: {e}")


async def add_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add an alias for an existing movie"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    conn = None
    try:
        if not context.args or len(context.args) < 2:
            await update.message.reply_text("Format: /addalias MovieTitle alias_name")
            return

        parts = context.args
        alias = parts[-1]
        movie_title = " ".join(parts[:-1])

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()

        cur.execute("SELECT id FROM movies WHERE title = %s", (movie_title,))
        movie = cur.fetchone()

        if not movie:
            await update.message.reply_text(f"❌ '{movie_title}' not found in database.")
            return

        movie_id = movie[0]

        cur.execute(
            "INSERT INTO movie_aliases (movie_id, alias) VALUES (%s, %s) ON CONFLICT (movie_id, alias) DO NOTHING",
            (movie_id, alias.lower())
        )

        conn.commit()
        await update.message.reply_text(f"✅ Alias '{alias}' added for '{movie_title}'")

    except Exception as e:
        logger.error(f"Error adding alias: {e}")
        await update.message.reply_text(f"Error: {e}")
    finally:
        if conn:
            conn.close()


async def list_aliases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all aliases for a movie"""
    conn = None
    try:
        if not context.args:
            await update.message.reply_text("Format: /aliases MovieTitle")
            return

        movie_title = " ".join(context.args)

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()

        cur.execute("""
            SELECT m.title, COALESCE(array_agg(ma.alias), '{}'::text[])
            FROM movies m
            LEFT JOIN movie_aliases ma ON m.id = ma.movie_id
            WHERE m.title ILIKE %s
            GROUP BY m.title
        """, (f'%{movie_title}%',))

        result = cur.fetchone()

        if not result:
            await update.message.reply_text(f"'{movie_title}' not found.")
            return

        title, aliases = result
        aliases = [a for a in aliases if a]
        aliases_list = "\n".join(f"• {alias}" for alias in aliases) if aliases else "No aliases found"

        await update.message.reply_text(f"🎬 <b>{title}</b>\n\n<b>Aliases:</b>\n{aliases_list}", parse_mode='HTML')

    except Exception as e:
        logger.error(f"Error listing aliases: {e}")
        await update.message.reply_text(f"Error: {e}")
    finally:
        if conn:
            conn.close()


async def bulk_add_aliases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add multiple aliases at once"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    conn = None
    try:
        full_text = update.message.text
        lines = full_text.split('\n')

        if len(lines) <= 1:
            await update.message.reply_text("""
Format:
/aliasbulk
Movie1: alias1, alias2, alias3
Movie2: alias4, alias5
""")
            return

        success_count = 0
        failed_count = 0

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()

        for line in lines:
            line = line.strip()
            if not line or line.startswith('/aliasbulk'):
                continue

            if ':' not in line:
                continue

            movie_title, aliases_str = line.split(':', 1)
            movie_title = movie_title.strip()
            aliases = [alias.strip() for alias in aliases_str.split(',') if alias.strip()]

            cur.execute("SELECT id FROM movies WHERE title ILIKE %s", (f'%{movie_title}%',))
            movie = cur.fetchone()

            if not movie:
                failed_count += len(aliases)
                continue

            movie_id = movie[0]

            for alias in aliases:
                try:
                    cur.execute(
                        "INSERT INTO movie_aliases (movie_id, alias) VALUES (%s, %s) ON CONFLICT (movie_id, alias) DO NOTHING",
                        (movie_id, alias.lower())
                    )
                    success_count += 1
                except:
                    failed_count += 1

        conn.commit()

        await update.message.reply_text(f"""
📊 Alias Bulk Add Results:

✅ Success: {success_count}
❌ Failed: {failed_count}
""")

    except Exception as e:
        logger.error(f"Error in bulk alias add: {e}")
        await update.message.reply_text(f"Error: {e}")
    finally:
        if conn:
            conn.close()


async def notify_manually(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually notify users about a movie"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    try:
        if not context.args:
            await update.message.reply_text("Usage: /notify <movie_title>")
            return

        movie_title = " ".join(context.args)

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()
        cur.execute("SELECT id, title, url, file_id FROM movies WHERE title ILIKE %s LIMIT 1", (f'%{movie_title}%',))
        movie_found = cur.fetchone()
        cur.close()
        conn.close()

        if movie_found:
            movie_id, title, url, file_id = movie_found
            value_to_send = file_id if file_id else url
            num_notified = await notify_users_for_movie(context, title, value_to_send)
            await notify_in_group(context, title)
            await update.message.reply_text(f"✅ {num_notified} users को '{title}' के लिए notify किया।")
        else:
            await update.message.reply_text(f"❌ '{movie_title}' database में नहीं मिली।")
    except Exception as e:
        logger.error(f"Error in notify_manually: {e}")
        await update.message.reply_text(f"Error: {e}")


async def notify_user_by_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send text notification to specific user"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    try:
        if not context.args or len(context.args) < 2:
            await update.message.reply_text("Usage: /notifyuser @username Your message here")
            return

        target_username = context.args[0].replace('@', '')
        message_text = ' '.join(context.args[1:])

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT user_id, first_name FROM user_requests WHERE username ILIKE %s LIMIT 1",
            (target_username,)
        )
        user = cur.fetchone()

        if not user:
            await update.message.reply_text(f"❌ User @{target_username} not found.")
            cur.close()
            conn.close()
            return

        user_id, first_name = user

        notification_text = f"📬 <b>Message from Admin</b>\n\n{message_text}"

        await context.bot.send_message(
            chat_id=user_id,
            text=notification_text,
            parse_mode='HTML'
        )

        await update.message.reply_text(f"✅ Message sent to @{target_username} ({first_name})")

        cur.close()
        conn.close()

    except telegram.error.Forbidden:
        await update.message.reply_text("❌ User blocked the bot.")
    except Exception as e:
        logger.error(f"Error in notify_user_by_username: {e}")
        await update.message.reply_text(f"Error: {e}")


async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast text message to all users"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    try:
        if not context.args:
            await update.message.reply_text("Usage: /broadcast Your message here")
            return

        message_text = ' '.join(context.args)

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()
        cur.execute("SELECT DISTINCT user_id, first_name, username FROM user_requests")
        all_users = cur.fetchall()

        if not all_users:
            await update.message.reply_text("No users found.")
            cur.close()
            conn.close()
            return

        status_msg = await update.message.reply_text(
            f"📤 Broadcasting to {len(all_users)} users...\n⏳ Please wait..."
        )

        success_count = 0
        failed_count = 0

        broadcast_text = f"📢 <b>Broadcast Message</b>\n\n{message_text}"

        for user_id, first_name, username in all_users:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=broadcast_text,
                    parse_mode='HTML'
                )
                success_count += 1
                await asyncio.sleep(0.05)
            except telegram.error.Forbidden:
                failed_count += 1
            except Exception as e:
                failed_count += 1
                logger.error(f"Failed broadcast to {user_id}: {e}")

        await status_msg.edit_text(
            f"📊 <b>Broadcast Complete</b>\n\n"
            f"✅ Sent: {success_count}\n"
            f"❌ Failed: {failed_count}\n"
            f"📝 Total: {len(all_users)}",
            parse_mode='HTML'
        )

        cur.close()
        conn.close()

    except Exception as e:
        logger.error(f"Error in broadcast_message: {e}")
        await update.message.reply_text(f"Error: {e}")


async def schedule_notification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Schedule a notification for later"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    try:
        if not context.args or len(context.args) < 3:
            await update.message.reply_text(
                "Usage: /schedulenotify <minutes> <@username> <message>\n"
                "Example: /schedulenotify 30 @john New movie arriving soon!"
            )
            return

        delay_minutes = int(context.args[0])
        target_username = context.args[1].replace('@', '')
        message_text = ' '.join(context.args[2:])

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT user_id, first_name FROM user_requests WHERE username ILIKE %s LIMIT 1",
            (target_username,)
        )
        user = cur.fetchone()

        if not user:
            await update.message.reply_text(f"❌ User @{target_username} not found.")
            cur.close()
            conn.close()
            return

        user_id, first_name = user
        cur.close()
        conn.close()

        async def send_scheduled_notification():
            await asyncio.sleep(delay_minutes * 60)
            try:
                notification_text = f"⏰ <b>Scheduled Message</b>\n\n{message_text}"
                await context.bot.send_message(
                    chat_id=user_id,
                    text=notification_text,
                    parse_mode='HTML'
                )
                logger.info(f"Scheduled notification sent to {user_id}")
            except Exception as e:
                logger.error(f"Failed to send scheduled notification to {user_id}: {e}")

        task = asyncio.create_task(send_scheduled_notification())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

        await update.message.reply_text(
            f"⏰ Notification scheduled!\n\n"
            f"To: @{target_username} ({first_name})\n"
            f"Delay: {delay_minutes} minutes\n"
            f"Message: {message_text[:50]}..."
        )

    except ValueError:
        await update.message.reply_text("❌ Invalid delay. Please provide number of minutes.")
    except Exception as e:
        logger.error(f"Error in schedule_notification: {e}")
        await update.message.reply_text(f"Error: {e}")


async def notify_user_with_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Notify user with media by replying to a message"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    try:
        if not update.message.reply_to_message:
            await update.message.reply_text(
                "❌ Reply to a media message with:\n"
                "/notifyuserwithmedia @username [Optional message]"
            )
            return

        if not context.args:
            await update.message.reply_text(
                "Usage: /notifyuserwithmedia @username [optional message]"
            )
            return

        target_username = context.args[0].replace('@', '')
        optional_message = ' '.join(context.args[1:]) if len(context.args) > 1 else None

        replied_message = update.message.reply_to_message

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT user_id, first_name FROM user_requests WHERE username ILIKE %s LIMIT 1",
            (target_username,)
        )
        user = cur.fetchone()

        if not user:
            await update.message.reply_text(f"❌ User @{target_username} not found.")
            cur.close()
            conn.close()
            return

        user_id, first_name = user
        cur.close()
        conn.close()

        notification_header = f"📬 <b>Message from Admin</b>\n"
        if optional_message:
            notification_header += f"\n{optional_message}\n"

        warning_msg = await context.bot.send_message(
            chat_id=user_id,
            text=notification_header + "\n⚠️ यह file 2 minute में delete हो जाएगी। Forward कर लें।",
            parse_mode='HTML'
        )

        sent_msg = None
        media_type = "unknown"

        if replied_message.document:
            media_type = "file"
            sent_msg = await context.bot.send_document(
                chat_id=user_id,
                document=replied_message.document.file_id,
                caption=optional_message if optional_message else None
            )
        elif replied_message.video:
            media_type = "video"
            sent_msg = await context.bot.send_video(
                chat_id=user_id,
                video=replied_message.video.file_id,
                caption=optional_message if optional_message else None
            )
        elif replied_message.audio:
            media_type = "audio"
            sent_msg = await context.bot.send_audio(
                chat_id=user_id,
                audio=replied_message.audio.file_id,
                caption=optional_message if optional_message else None
            )
        elif replied_message.photo:
            media_type = "photo"
            photo = replied_message.photo[-1]
            sent_msg = await context.bot.send_photo(
                chat_id=user_id,
                photo=photo.file_id,
                caption=optional_message if optional_message else None
            )
        elif replied_message.text:
            media_type = "text"
            text_to_send = replied_message.text
            if optional_message:
                text_to_send = f"{optional_message}\n\n{text_to_send}"
            sent_msg = await context.bot.send_message(
                chat_id=user_id,
                text=text_to_send
            )
        else:
            await update.message.reply_text("❌ Unsupported media type.")
            return

        if sent_msg and media_type != "text":
            await auto_delete.schedule_delete(
                context,
                user_id,
                [sent_msg.message_id, warning_msg.message_id],
                delay=AUTO_DELETE_DELAY
            )

        await update.message.reply_text(
            f"✅ <b>Notification Sent!</b>\n\n"
            f"To: @{target_username} ({first_name})\n"
            f"Media Type: {media_type.capitalize()}",
            parse_mode='HTML'
        )

    except telegram.error.Forbidden:
        await update.message.reply_text("❌ User blocked the bot.")
    except Exception as e:
        logger.error(f"Error in notify_user_with_media: {e}")
        await update.message.reply_text(f"Error: {e}")


async def broadcast_with_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast media to all users"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    replied_message = update.message.reply_to_message
    if not replied_message:
        await update.message.reply_text("❌ Reply to a media message to broadcast it.")
        return

    try:
        optional_message = ' '.join(context.args) if context.args else None

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()
        cur.execute("SELECT DISTINCT user_id, first_name, username FROM user_requests")
        all_users = cur.fetchall()

        if not all_users:
            await update.message.reply_text("No users found.")
            cur.close()
            conn.close()
            return

        status_msg = await update.message.reply_text(
            f"📤 Broadcasting media to {len(all_users)} users...\n⏳ Please wait..."
        )

        success_count = 0
        failed_count = 0

        for user_id, first_name, username in all_users:
            try:
                header = "📢 <b>Broadcast from Admin</b>\n"
                if optional_message:
                    header += f"\n{optional_message}\n"

                await context.bot.send_message(
                    chat_id=user_id,
                    text=header,
                    parse_mode='HTML'
                )

                if replied_message.document:
                    await context.bot.send_document(
                        chat_id=user_id,
                        document=replied_message.document.file_id
                    )
                elif replied_message.video:
                    await context.bot.send_video(
                        chat_id=user_id,
                        video=replied_message.video.file_id
                    )
                elif replied_message.audio:
                    await context.bot.send_audio(
                        chat_id=user_id,
                        audio=replied_message.audio.file_id
                    )
                elif replied_message.photo:
                    photo = replied_message.photo[-1]
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=photo.file_id
                    )

                success_count += 1
                await asyncio.sleep(0.1)

            except telegram.error.Forbidden:
                failed_count += 1
            except Exception as e:
                failed_count += 1
                logger.error(f"Failed broadcast to {user_id}: {e}")

        await status_msg.edit_text(
            f"📊 <b>Broadcast Complete</b>\n\n"
            f"✅ Sent: {success_count}\n"
            f"❌ Failed: {failed_count}\n"
            f"📝 Total: {len(all_users)}",
            parse_mode='HTML'
        )

        cur.close()
        conn.close()

    except Exception as e:
        logger.error(f"Error in broadcast_with_media: {e}")
        await update.message.reply_text(f"Error: {e}")


async def quick_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick notify - sends media to specific requesters"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    replied_message = update.message.reply_to_message
    if not replied_message:
        await update.message.reply_text("❌ Reply to a media message first!")
        return

    if not context.args:
        await update.message.reply_text("Usage: /qnotify <@username | MovieTitle>")
        return

    try:
        query = ' '.join(context.args)

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()

        target_users = []

        if query.startswith('@'):
            username = query.replace('@', '')
            cur.execute(
                "SELECT DISTINCT user_id, first_name, username FROM user_requests WHERE username ILIKE %s",
                (username,)
            )
            target_users = cur.fetchall()
        else:
            cur.execute(
                "SELECT DISTINCT user_id, first_name, username FROM user_requests WHERE movie_title ILIKE %s AND notified = FALSE",
                (f'%{query}%',)
            )
            target_users = cur.fetchall()

        if not target_users:
            await update.message.reply_text(f"❌ No users found for '{query}'")
            cur.close()
            conn.close()
            return

        success_count = 0
        failed_count = 0

        for user_id, first_name, username in target_users:
            try:
                caption = f"🎬 {query}" if not query.startswith('@') else None
                if replied_message.document:
                    await context.bot.send_document(
                        chat_id=user_id,
                        document=replied_message.document.file_id,
                        caption=caption
                    )
                elif replied_message.video:
                    await context.bot.send_video(
                        chat_id=user_id,
                        video=replied_message.video.file_id,
                        caption=caption
                    )
                elif replied_message.photo:
                    photo = replied_message.photo[-1]
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=photo.file_id,
                        caption=caption
                    )

                success_count += 1

                if not query.startswith('@'):
                    cur.execute(
                        "UPDATE user_requests SET notified = TRUE WHERE user_id = %s AND movie_title ILIKE %s",
                        (user_id, f'%{query}%')
                    )
                    conn.commit()

                await asyncio.sleep(0.1)

            except Exception as e:
                failed_count += 1
                logger.error(f"Failed to send to {user_id}: {e}")

        await update.message.reply_text(
            f"✅ Sent to {success_count} user(s)\n"
            f"❌ Failed for {failed_count} user(s)\n"
            f"Query: {query}"
        )

        cur.close()
        conn.close()

    except Exception as e:
        logger.error(f"Error in quick_notify: {e}")
        await update.message.reply_text(f"Error: {e}")


async def forward_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward message to user"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    replied_message = update.message.reply_to_message
    if not replied_message:
        await update.message.reply_text("❌ Reply to a message first!")
        return

    if not context.args:
        await update.message.reply_text("Usage: /forwardto @username")
        return

    try:
        target_username = context.args[0].replace('@', '')

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT user_id, first_name FROM user_requests WHERE username ILIKE %s LIMIT 1",
            (target_username,)
        )
        user = cur.fetchone()

        if not user:
            await update.message.reply_text(f"❌ User @{target_username} not found.")
            cur.close()
            conn.close()
            return

        user_id, first_name = user
        cur.close()
        conn.close()

        await replied_message.forward(chat_id=user_id)

        await update.message.reply_text(f"✅ Forwarded to @{target_username} ({first_name})")

    except Exception as e:
        logger.error(f"Error in forward_to_user: {e}")
        await update.message.reply_text(f"Error: {e}")


async def get_user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get user information"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /userinfo @username")
        return

    try:
        target_username = context.args[0].replace('@', '')

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()

        cur.execute("""
            SELECT
                user_id,
                username,
                first_name,
                COUNT(*) as total_requests,
                SUM(CASE WHEN notified = TRUE THEN 1 ELSE 0 END) as fulfilled,
                MAX(requested_at) as last_request
            FROM user_requests
            WHERE username ILIKE %s
            GROUP BY user_id, username, first_name
        """, (target_username,))

        user_info = cur.fetchone()

        if not user_info:
            await update.message.reply_text(f"❌ No data found for @{target_username}")
            cur.close()
            conn.close()
            return

        user_id, username, first_name, total, fulfilled, last_request = user_info
        fulfilled = fulfilled or 0

        cur.execute("""
            SELECT movie_title, requested_at, notified
            FROM user_requests
            WHERE user_id = %s
            ORDER BY requested_at DESC
            LIMIT 5
        """, (user_id,))
        recent_requests = cur.fetchall()
        cur.close()
        conn.close()

        username_str = f"@{username}" if username else "N/A"

        info_text = f"""
👤 <b>User Information</b>

<b>Basic Info:</b>
• Name: {first_name}
• Username: {username_str}
• User ID: <code>{user_id}</code>

<b>Statistics:</b>
• Total Requests: {total}
• Fulfilled: {fulfilled}
• Pending: {total - fulfilled}
• Last Request: {last_request.strftime('%Y-%m-%d %H:%M') if last_request else 'N/A'}

<b>Recent Requests:</b>
"""

        if recent_requests:
            for movie, req_time, notified in recent_requests:
                status = "✅" if notified else "⏳"
                info_text += f"{status} {movie[:30]} - {req_time.strftime('%m/%d %H:%M')}\n"
        else:
            info_text += "No recent requests."

        await update.message.reply_text(info_text, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Error in get_user_info: {e}")
        await update.message.reply_text(f"Error: {e}")


async def list_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all bot users"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    try:
        page = 1
        if context.args and context.args[0].isdigit():
            page = int(context.args[0])

        per_page = 10
        offset = (page - 1) * per_page

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()

        cur.execute("SELECT COUNT(DISTINCT user_id) FROM user_requests")
        total_users = cur.fetchone()[0]

        cur.execute("""
            SELECT
                user_id,
                username,
                first_name,
                COUNT(*) as requests,
                MAX(requested_at) as last_seen
            FROM user_requests
            GROUP BY user_id, username, first_name
            ORDER BY MAX(requested_at) DESC
            LIMIT %s OFFSET %s
        """, (per_page, offset))

        users = cur.fetchall()
        cur.close()
        conn.close()

        total_pages = (total_users + per_page - 1) // per_page if total_users > 0 else 1

        users_text = f"👥 <b>Bot Users</b> (Page {page}/{total_pages})\n\n"

        if not users:
            users_text += "No users found."
        else:
            for idx, (user_id, username, first_name, req_count, last_seen) in enumerate(users, start=offset+1):
                username_str = f"@{username}" if username else "N/A"
                users_text += f"{idx}. {first_name} ({username_str})\n"
                users_text += f"   ID: <code>{user_id}</code> | Requests: {req_count}\n"
                users_text += f"   Last: {last_seen.strftime('%Y-%m-%d %H:%M')}\n\n"

        users_text += f"\n📊 Total Users: {total_users}"

        await update.message.reply_text(users_text, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Error in list_all_users: {e}")
        await update.message.reply_text(f"Error: {e}")


async def get_bot_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get comprehensive bot statistics"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    try:
        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM movies")
        total_movies = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT user_id) FROM user_requests")
        total_users = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM user_requests")
        total_requests = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM user_requests WHERE notified = TRUE")
        fulfilled = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM user_requests WHERE DATE(requested_at) = CURRENT_DATE")
        today_requests = cur.fetchone()[0]

        cur.execute("""
            SELECT first_name, username, COUNT(*) as req_count
            FROM user_requests
            GROUP BY user_id, first_name, username
            ORDER BY req_count DESC
            LIMIT 5
        """)
        top_users = cur.fetchall()

        cur.close()
        conn.close()

        fulfillment_rate = (fulfilled / total_requests * 100) if total_requests > 0 else 0

        stats_text = f"""
📊 <b>Bot Statistics</b>

<b>Database:</b>
• Movies: {total_movies}
• Users: {total_users}
• Total Requests: {total_requests}
• Fulfilled: {fulfilled}
• Pending: {total_requests - fulfilled}

<b>Activity:</b>
• Today's Requests: {today_requests}
• Fulfillment Rate: {fulfillment_rate:.1f}%

<b>Top Requesters:</b>
"""

        if top_users:
            for name, username, count in top_users:
                username_str = f"@{username}" if username else "N/A"
                stats_text += f"• {name} ({username_str}): {count}\n"
        else:
            stats_text += "No data available."

        await update.message.reply_text(stats_text, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Error in get_bot_stats: {e}")
        await update.message.reply_text(f"Error: {e}")


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin commands help"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    help_text = """
👑 <b>Admin Commands Guide</b>

<b>Media Notifications:</b>
• /notifyuserwithmedia @user [msg] - Reply to media + send
• /qnotify &lt;@user|MovieTitle&gt; - Quick notify (reply to media)
• /forwardto @user - Forward message
• /broadcastmedia [msg] - Broadcast media to all

<b>Text Notifications:</b>
• /notifyuser @user &lt;msg&gt; - Send text message
• /broadcast &lt;msg&gt; - Text broadcast to all
• /schedulenotify &lt;min&gt; @user &lt;msg&gt; - Schedule notification

<b>User Management:</b>
• /userinfo @username - Get user stats
• /listusers [page] - List all users

<b>Movie Management:</b>
• /addmovie &lt;Title&gt; &lt;URL|FileID&gt; - Add movie
• /bulkadd - Bulk add movies
• /addalias &lt;Title&gt; &lt;alias&gt; - Add alias
• /aliasbulk - Bulk add aliases
• /aliases &lt;MovieTitle&gt; - List aliases
• /notify &lt;MovieTitle&gt; - Notify requesters

<b>Stats & Help:</b>
• /stats - Bot statistics
• /adminhelp - This help message
"""

    await update.message.reply_text(help_text, parse_mode='HTML')


# ==================== ERROR HANDLER ====================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log errors and handle them gracefully"""
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Sorry, something went wrong. Please try again later.",
                reply_markup=get_main_keyboard()
            )
        except Exception as e:
            logger.error(f"Failed to send error message: {e}")


# ==================== FLASK APP ====================
flask_app = Flask(__name__)


@flask_app.route('/')
def home():
    return "🎬 FilmfyBox Bot is running!"


@flask_app.route('/health')
def health():
    return "OK", 200


@flask_app.route(f'/{UPDATE_SECRET_CODE}')
def trigger_update():
    result = update_movies_in_db()
    return result


def run_flask():
    """Run Flask server"""
    port = int(os.environ.get('PORT', 8080))
    flask_app.secret_key = os.environ.get('FLASK_SECRET_KEY', None) or os.urandom(24)

    try:
        from admin_views import admin as admin_blueprint
        flask_app.register_blueprint(admin_blueprint)
        logger.info("Admin blueprint registered successfully.")
    except Exception as e:
        logger.error(f"Failed to register admin blueprint: {e}")

    flask_app.run(host='0.0.0.0', port=port)


# ==================== MAIN BOT FUNCTION ====================
def main():
    """Run the Telegram bot"""
    logger.info("🎬 FilmfyBox Bot is starting...")

    if not TELEGRAM_BOT_TOKEN:
        logger.error("No Telegram bot token found. Exiting.")
        return

    try:
        setup_database()
    except Exception as e:
        logger.error(f"Database setup failed but continuing: {e}")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).read_timeout(30).write_timeout(30).build()

    # Conversation handler for private chats
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start, filters=filters.ChatType.PRIVATE)],
        states={
            MAIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, main_menu)],
            SEARCHING: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, search_movies)],
            REQUESTING: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, request_movie)],
            AWAITING_REQUEST_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, main_menu)],
        },
        fallbacks=[CommandHandler('cancel', cancel, filters=filters.ChatType.PRIVATE)],
        per_message=False,
        per_chat=True,
        allow_reentry=True
    )

    # Register handlers
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
        group_message_handler
    ))
    application.add_handler(conv_handler)

    # Admin commands
    application.add_handler(CommandHandler("addmovie", add_movie))
    application.add_handler(CommandHandler("bulkadd", bulk_add_movies))
    application.add_handler(CommandHandler("addalias", add_alias))
    application.add_handler(CommandHandler("aliases", list_aliases))
    application.add_handler(CommandHandler("aliasbulk", bulk_add_aliases))
    application.add_handler(CommandHandler("notify", notify_manually))
    application.add_handler(CommandHandler("notifyuser", notify_user_by_username))
    application.add_handler(CommandHandler("broadcast", broadcast_message))
    application.add_handler(CommandHandler("schedulenotify", schedule_notification))
    application.add_handler(CommandHandler("notifyuserwithmedia", notify_user_with_media))
    application.add_handler(CommandHandler("broadcastmedia", broadcast_with_media))
    application.add_handler(CommandHandler("qnotify", quick_notify))
    application.add_handler(CommandHandler("forwardto", forward_to_user))
    application.add_handler(CommandHandler("userinfo", get_user_info))
    application.add_handler(CommandHandler("listusers", list_all_users))
    application.add_handler(CommandHandler("stats", get_bot_stats))
    application.add_handler(CommandHandler("adminhelp", admin_help))

    # Error handler
    application.add_error_handler(error_handler)

    # Start Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    logger.info("Flask server started in background thread.")

    # Run the bot
    logger.info("Starting bot polling...")
    application.run_polling()


if __name__ == '__main__':
    main()
