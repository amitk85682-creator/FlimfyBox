# Add this snippet near the top of main.py, after your imports:
try:
    # prefer db_utils' fixed URL if it exists
    import db_utils
    FIXED_DATABASE_URL = getattr(db_utils, "FIXED_DATABASE_URL", None)
except Exception:
    FIXED_DATABASE_URL = None
# -*- coding: utf-8 -*-
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
from bs4 import BeautifulSoup
import telegram
import psycopg2
from typing import Optional
from flask import Flask, request, session, g
import google.generativeai as genai
import admin_views as admin_views_module
import db_utils
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
from datetime import datetime, timedelta
from fuzzywuzzy import process, fuzz
from urllib.parse import urlparse, urlunparse, quote
from collections import defaultdict

# ==================== LOGGING SETUP ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== CONVERSATION STATES ====================
MAIN_MENU, SEARCHING, REQUESTING = range(3)

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

# Rate limiting dictionary
user_last_request = defaultdict(lambda: datetime.min)

# ===== Configurable rate-limiting and fuzzy settings =====
REQUEST_COOLDOWN_MINUTES = int(os.environ.get('REQUEST_COOLDOWN_MINUTES', '10'))
SIMILARITY_THRESHOLD = int(os.environ.get('SIMILARITY_THRESHOLD', '80'))
MAX_REQUESTS_PER_MINUTE = int(os.environ.get('MAX_REQUESTS_PER_MINUTE', '10'))

# Validate required environment variables
if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN environment variable is not set")
    raise ValueError("TELEGRAM_BOT_TOKEN is not set.")

if not DATABASE_URL:
    logger.error("DATABASE_URL environment variable is not set")
    raise ValueError("DATABASE_URL is not set.")

# ==================== UTILITY FUNCTIONS ====================
def preprocess_query(query):
    """Clean and normalize user query"""
    query = re.sub(r'[^\w\s-]', '', query)
    query = ' '.join(query.split())
    stop_words = ['movie', 'film', 'full', 'download', 'watch', 'online', 'free']
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

# ===== Helper functions for matching and duplicate checks =====
def _normalize_title_for_match(title: str) -> str:
    """Normalize title for fuzzy matching"""
    if not title:
        return ""
    t = re.sub(r'[^\w\s]', ' ', title)
    t = re.sub(r'\s+', ' ', t).strip()
    return t.lower()

def get_last_similar_request_for_user(user_id: int, title: str, minutes_window: int = REQUEST_COOLDOWN_MINUTES):
    """Look up the user's most recent similar request"""
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

        cur.execute('CREATE INDEX IF NOT EXISTS idx_movies_title ON movies (title);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_movies_title_trgm ON movies USING gin (title gin_trgm_ops);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_user_requests_movie_title ON user_requests (movie_title);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_user_requests_user_id ON user_requests (user_id);')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_movie_aliases_alias ON movie_aliases (alias);')

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
    """Search for MULTIPLE movies in database with fuzzy matching"""
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
        filtered_movies = [movie_dict[title] for title, score, index in matches if score >= 65]

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
                SET requested_at = EXCLUDED.requested_at
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
        Analyze if this is a movie/series request. Respond with JSON: {{"is_request": true/false, "content_title": "title"}}
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

# ==================== NOTIFICATION FUNCTIONS ====================
async def send_admin_notification(context: ContextTypes.DEFAULT_TYPE, user, movie_title, group_info=None):
    """Send notification to admin channel about a new request"""
    if not ADMIN_CHANNEL_ID:
        return

    try:
        user_info = f"User: {user.first_name or 'Unknown'}"
        if user.username:
            user_info += f" (`@{user.username}`)"
        user_info += f" (ID: {user.id})"

        group_info_text = f"From Group: {group_info}" if group_info else "Via Private Message"

        message = f"""
🎬 **NEW MOVIE REQUEST**

**Movie:** {movie_title}
{user_info}
{group_info_text}
Time: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}
        """

        await context.bot.send_message(
            chat_id=ADMIN_CHANNEL_ID,
            text=message,
            reply_markup=get_admin_request_keyboard(user.id, movie_title),
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Error sending admin notification: {e}")

async def delete_messages_after_delay(context, chat_id, message_ids, delay=60):
    """Delete messages after specified delay"""
    try:
        await asyncio.sleep(delay)
        for msg_id in message_ids:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                logger.info(f"✅ Deleted message {msg_id} from chat {chat_id}")
            except Exception as e:
                logger.error(f"Failed to delete message {msg_id}: {e}")
    except Exception as e:
        logger.error(f"Error in delete_messages_after_delay: {e}")

async def notify_users_for_movie(context: ContextTypes.DEFAULT_TYPE, movie_title, movie_url_or_file_id):
    """Notify users who requested a movie"""
    logger.info(f"Attempting to notify users for movie: {movie_title}")
    conn = None
    cur = None
    notified_count = 0

    caption_text = f"🎬 <b>{movie_title}</b>"

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
                    text=f"🎉 Hey {first_name or username}! Your requested movie '{movie_title}' is now available!"
                )

                warning_msg = await context.bot.send_message(
                    chat_id=user_id,
                    text="⚠️ This file will automatically delete after 1 minute. Please forward it to another chat.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("Join Channel", url="http://t.me/filmfybox")
                    ]])
                )

                sent_msg = None

                if isinstance(movie_url_or_file_id, str) and any(movie_url_or_file_id.startswith(prefix) for prefix in ["BQAC", "BAAC", "CAAC", "AQAC"]):
                    sent_msg = await context.bot.send_document(
                        chat_id=user_id,
                        document=movie_url_or_file_id,
                        caption=caption_text,
                        parse_mode='HTML'
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
                        parse_mode='HTML'
                    )
                elif isinstance(movie_url_or_file_id, str) and movie_url_or_file_id.startswith("http"):
                    await context.bot.send_message(
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
                        parse_mode='HTML'
                    )

                if sent_msg:
                    asyncio.create_task(
                        delete_messages_after_delay(
                            context,
                            user_id,
                            [sent_msg.message_id, warning_msg.message_id],
                            60
                        )
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

# ==================== KEYBOARD MARKUPS ====================
def get_main_keyboard():
    """Get the main menu keyboard"""
    keyboard = [
        ['🔍 Search Movies', '🙋 Request Movie'],
        ['📊 My Stats', '❓ Help']
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def get_admin_request_keyboard(user_id, movie_title):
    """Inline keyboard for admin actions"""
    sanitized_title = movie_title[:30]
    keyboard = [
        [InlineKeyboardButton("✅ FULFILL MOVIE", callback_data=f"admin_fulfill_{user_id}_{sanitized_title}")],
        [InlineKeyboardButton("❌ IGNORE/DELETE", callback_data=f"admin_delete_{user_id}_{sanitized_title}")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_movie_options_keyboard(movie_title, url):
    """Get inline keyboard for movie options"""
    keyboard = [
        [InlineKeyboardButton("🎬 Watch Now", url=url)],
        [InlineKeyboardButton("📥 Download", callback_data=f"download_{movie_title[:50]}")],
        [InlineKeyboardButton("Join Channel", url="http://t.me/filmfybox")]
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

def get_all_movie_qualities(movie_id):
    """Fetch all available qualities for a given movie ID"""
    conn = get_db_connection()
    if not conn:
        return []

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT quality, url, file_id
            FROM movie_files
            WHERE movie_id = %s AND (url IS NOT NULL OR file_id IS NOT NULL)
            ORDER BY CASE quality
                WHEN '4K' THEN 1
                WHEN 'HD Quality' THEN 2
                WHEN 'Standart Quality'  THEN 3
                WHEN 'Low Quality'  THEN 4
                ELSE 5
            END DESC
        """, (movie_id,))
        results = cur.fetchall()
        cur.close()
        return results
    except Exception as e:
        logger.error(f"Error fetching movie qualities for {movie_id}: {e}")
        return []
    finally:
        if conn:
            conn.close()

def create_quality_selection_keyboard(movie_id, title, qualities):
    """Create inline keyboard with quality selection buttons"""
    keyboard = []

    for quality, url, file_id in qualities:
        callback_data = f"quality_{movie_id}_{quality}"
        button_text = f"🎬 {quality} ({'File' if file_id else 'Link'})"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])

    keyboard.append([InlineKeyboardButton("❌ Cancel Selection", callback_data="cancel_selection")])

    return InlineKeyboardMarkup(keyboard)

# ==================== MOVIE SENDING FUNCTION ====================
async def send_movie_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: int, title: str, url: Optional[str] = None, file_id: Optional[str] = None):
    """Sends the movie file/link to the user with a warning and caption"""
    chat_id = update.effective_chat.id

    if not url and not file_id:
        qualities = get_all_movie_qualities(movie_id)
        if qualities:
            context.user_data['selected_movie_data'] = {
                'id': movie_id,
                'title': title,
                'qualities': qualities
            }
            selection_text = f"✅ We found **{title}** in multiple qualities.\n\n⬇️ **Please choose the file quality:**"
            keyboard = create_quality_selection_keyboard(movie_id, title, qualities)
            await context.bot.send_message(
                chat_id=chat_id,
                text=selection_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            return

    try:
        warning_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ This file will automatically delete after 1 minute. Please forward it to another chat.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Join Channel", url="http://t.me/filmfybox")
            ]])
        )

        sent_msg = None
        caption_text = f"🎬 <b>{title}</b>"

        if file_id:
            sent_msg = await context.bot.send_document(
                chat_id=chat_id,
                document=file_id,
                caption=caption_text,
                parse_mode='HTML'
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
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Copy private link failed {url}: {e}")
                sent_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🎬 Found: {title}\n\n{caption_text}",
                    reply_markup=get_movie_options_keyboard(title, url),
                    parse_mode='HTML'
                )
        elif url and url.startswith("https://t.me/") and "/c/" not in url:
            try:
                parts = url.rstrip('/').split('/')
                username = parts[-2].lstrip("@")
                message_id = int(parts[-1])
                from_chat_id = f"@{username}"
                sent_msg = await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                    caption=caption_text,
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Copy public link failed {url}: {e}")
                sent_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🎬 Found: {title}\n\n{caption_text}",
                    reply_markup=get_movie_options_keyboard(title, url),
                    parse_mode='HTML'
                )
        elif url and url.startswith("http"):
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"🎉 Found it! '{title}' is available!\n\n{caption_text}",
                reply_markup=get_movie_options_keyboard(title, url),
                parse_mode='HTML'
            )
        else:
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Sorry, '{title}' found but no valid file or link is attached in the database."
            )

        if sent_msg:
            message_ids_to_delete = [warning_msg.message_id, sent_msg.message_id]
            asyncio.create_task(
                delete_messages_after_delay(
                    context,
                    chat_id,
                    message_ids_to_delete,
                    60
                )
            )

    except Exception as e:
        logger.error(f"Error sending movie to user: {e}")
        try:
            await context.bot.send_message(chat_id=chat_id, text="❌ Server failed to send file. Please report to Admin.")
        except Exception as e2:
            logger.error(f"Secondary send error: {e2}")

# ==================== GROUP MESSAGE HANDLER ====================
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle messages in groups - listen for movie names and offer to send in private"""
    if update.effective_chat.type not in ['group', 'supergroup']:
        return

    try:
        user_message = update.message.text.strip()
        if not user_message or len(user_message) < 2:
            return

        # Check if message contains a movie name from database
        movies_found = get_movies_from_db(user_message, limit=5)
        
        if movies_found:
            movie_id, title, url, file_id = movies_found[0]
            
            # Send offer message with button to get movie in private
            offer_text = f"🎬 I found \"{title}\" in our database! Would you like me to send it to you in private chat?"
            
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📩 Get Movie in Private", url=f"https://t.me/{(await context.bot.get_me()).username}?start=group_{movie_id}")
            ]])
            
            offer_msg = await update.message.reply_text(
                offer_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            
            # Auto-delete offer after 2 minutes
            asyncio.create_task(
                delete_messages_after_delay(
                    context,
                    update.effective_chat.id,
                    [offer_msg.message_id],
                    120
                )
            )

    except Exception as e:
        logger.error(f"Error handling group message: {e}")

# ==================== TELEGRAM BOT HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler - now handles group movie requests"""
    try:
        # Check if start command comes from group movie button
        if context.args and context.args[0].startswith('group_'):
            movie_id = int(context.args[0].replace('group_', ''))
            
            # Get movie details
            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                cur.execute("SELECT id, title, url, file_id FROM movies WHERE id = %s", (movie_id,))
                movie = cur.fetchone()
                cur.close()
                conn.close()
                
                if movie:
                    movie_id, title, url, file_id = movie
                    await send_movie_to_user(update, context, movie_id, title, url, file_id)
                    return

        welcome_text = """
📨 Send Movie or Series Name and Year As Per Google Spelling..!! 👍

⚠️ Example For Movie 👇

👉 Jailer
👉 Jailer 2023

⚠️ Example For WebSeries 👇

👉 Stranger Things
👉 Stranger Things S02 E04

⚠️ Don't add emojis and symbols in movie name, use letters only..!! ❌
"""
        await update.message.reply_text(welcome_text, reply_markup=get_main_keyboard())
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error in start command: {e}")

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main menu options"""
    try:
        query = update.message.text

        if query == '🔍 Search Movies':
            await update.message.reply_text("Great! Tell me the name of the movie you want to search for.")
            return SEARCHING

        elif query == '🙋 Request Movie':
            await update.message.reply_text("Okay, you've chosen to request a new movie. Please tell me the name of the movie you want me to add.")
            return REQUESTING

        elif query == '📊 My Stats':
            user_id = update.effective_user.id
            conn = None
            try:
                conn = get_db_connection()
                if conn:
                    cur = conn.cursor()
                    cur.execute("SELECT COUNT(*) FROM user_requests WHERE user_id = %s", (user_id,))
                    request_count = cur.fetchone()[0]

                    cur.execute("SELECT COUNT(*) FROM user_requests WHERE user_id = %s AND notified = TRUE", (user_id,))
                    fulfilled_count = cur.fetchone()[0]

                    stats_text = f"""
📊 Your Stats:
- Total Requests: {request_count}
- Fulfilled Requests: {fulfilled_count}
                    """
                    await update.message.reply_text(stats_text)
                else:
                    await update.message.reply_text("Sorry, database connection failed.")
            except Exception as e:
                logger.error(f"Error getting stats: {e}")
                await update.message.reply_text("Sorry, couldn't retrieve your stats at the moment.")
            finally:
                if conn: conn.close()

            return MAIN_MENU

        elif query == '❓ Help':
            help_text = """
🤖 How to use FlimfyBox Bot:

🔍 Search Movies: Find movies in our collection
🙋 Request Movie: Request a new movie to be added
📊 My Stats: View your request statistics

Just use the buttons below to navigate!
            """
            await update.message.reply_text(help_text)
            return MAIN_MENU
        else:
            return await search_movies(update, context)

    except Exception as e:
        logger.error(f"Error in main menu: {e}")
        return MAIN_MENU

async def search_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle movie search with multiple results support"""
    try:
        if not await check_rate_limit(update.effective_user.id):
            await update.message.reply_text("⚠️ Please wait a moment before searching again.")
            return SEARCHING

        user_message = update.message.text.strip()
        processed_query = preprocess_query(user_message) if user_message else user_message
        search_query = processed_query if processed_query else user_message

        movies_found = get_movies_from_db(search_query, limit=10)

        if not movies_found:
            user = update.effective_user
            store_user_request(
                user.id,
                user.username,
                user.first_name,
                user_message,
                update.effective_chat.id if update.effective_chat.type != "private" else None,
                update.message.message_id
            )

            messages_to_delete = []

            # Simple text message instead of GIF
            not_found_msg = await update.message.reply_text(
                f"😔 Sorry, '{user_message}' is not in my collection right now. Would you like to request it?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Yes, Request It", callback_data=f"request_{user_message[:50]}")
                ]])
            )
            messages_to_delete.append(not_found_msg.message_id)

            search_tip_msg = await update.message.reply_text(
                "🔍 **Search Tips:**\n\n"
                "• Check spelling on Google and copy exactly\n"
                "• Use movie name + year format\n"
                "• Don't add extra words like 'movie' or 'download'",
                parse_mode='Markdown'
            )
            messages_to_delete.append(search_tip_msg.message_id)

            if messages_to_delete:
                asyncio.create_task(
                    delete_messages_after_delay(
                        context,
                        update.effective_chat.id,
                        messages_to_delete,
                        60
                    )
                )

        elif len(movies_found) == 1:
            movie_id, title, url, file_id = movies_found[0]
            await send_movie_to_user(update, context, movie_id, title, url, file_id)

        else:
            context.user_data['search_results'] = movies_found
            context.user_data['search_query'] = user_message

            selection_text = f"🎬 **Found {len(movies_found)} movies matching '{user_message}'**\n\nPlease select the movie you want:"
            keyboard = create_movie_selection_keyboard(movies_found, page=0)

            await update.message.reply_text(
                selection_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )

        await update.message.reply_text("What would you like to do next?", reply_markup=get_main_keyboard())
        return MAIN_MENU

    except Exception as e:
        logger.error(f"Error in search movies: {e}")
        await update.message.reply_text("Sorry, something went wrong. Please try again.")
        return MAIN_MENU

async def request_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle movie requests with duplicate detection"""
    try:
        user_message = (update.message.text or "").strip()
        user = update.effective_user

        if not user_message:
            await update.message.reply_text("Please send the movie name.")
            return REQUESTING

        burst = user_burst_count(user.id, window_seconds=60)
        if burst >= MAX_REQUESTS_PER_MINUTE:
            await update.message.reply_text(
                "🛑 You're sending requests too quickly. Please wait a few minutes and try again."
            )
            return REQUESTING

        intent = await analyze_intent(user_message)
        if not intent["is_request"]:
            await update.message.reply_text("This doesn't look like a movie/series name. Please send the correct name.")
            return REQUESTING

        movie_title = intent["content_title"] or user_message

        similar = get_last_similar_request_for_user(user.id, movie_title, minutes_window=REQUEST_COOLDOWN_MINUTES)
        if similar:
            last_time = similar.get("requested_at")
            elapsed = datetime.now() - last_time
            minutes_passed = int(elapsed.total_seconds() / 60)
            minutes_left = max(0, REQUEST_COOLDOWN_MINUTES - minutes_passed)
            if minutes_left > 0:
                strict_text = (
                    "🛑 Please wait! You recently requested this movie.\n\n"
                    f"Similar previous request: \"{similar.get('stored_title')}\"\n"
                    f"Please try again in {minutes_left} minutes. 🙏"
                )
                await update.message.reply_text(strict_text)
                return REQUESTING

        stored = store_user_request(
            user.id,
            user.username,
            user.first_name,
            movie_title,
            update.effective_chat.id if update.effective_chat.type != "private" else None,
            update.message.message_id
        )
        if not stored:
            logger.error("Failed to store user request in DB.")
            await update.message.reply_text("Sorry, your request couldn't be stored. Please try again later.")
            return REQUESTING

        group_info = update.effective_chat.title if update.effective_chat.type != "private" else None
        await send_admin_notification(context, user, movie_title, group_info)

        await update.message.reply_text(
            f"✅ Got it! Your request for '{movie_title}' has been sent. I'll let you know when it's available.",
            reply_markup=get_main_keyboard()
        )

        return MAIN_MENU

    except Exception as e:
        logger.error(f"Error in request_movie: {e}")
        await update.message.reply_text("Sorry, an error occurred while processing your request.")
        return REQUESTING

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks"""
    try:
        query = update.callback_query
        await query.answer()

        if query.data.startswith("movie_"):
            movie_id = int(query.data.replace("movie_", ""))

            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT id, title FROM movies WHERE id = %s", (movie_id,))
            movie = cur.fetchone()
            cur.close()
            conn.close()

            if not movie:
                await query.edit_message_text("❌ Movie not found in database.")
                return

            movie_id, title = movie
            qualities = get_all_movie_qualities(movie_id)

            if not qualities:
                await query.edit_message_text(f"✅ You selected: **{title}**\n\nSending movie...", parse_mode='Markdown')
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT url, file_id FROM movies WHERE id = %s", (movie_id,))
                url, file_id = cur.fetchone() or (None, None)
                cur.close()
                conn.close()

                await send_movie_to_user(update, context, movie_id, title, url, file_id)
                return

            context.user_data['selected_movie_data'] = {
                'id': movie_id,
                'title': title,
                'qualities': qualities
            }

            selection_text = f"✅ You selected: **{title}**\n\n⬇️ **Please choose the file quality:**"
            keyboard = create_quality_selection_keyboard(movie_id, title, qualities)

            await query.edit_message_text(
                selection_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )

        elif query.data.startswith("admin_fulfill_"):
            parts = query.data.split('_', 2)
            user_id = int(parts[1])
            movie_title = parts[2]

            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                cur.execute("SELECT id, url, file_id FROM movies WHERE title = %s LIMIT 1", (movie_title,))
                movie_data = cur.fetchone()

                if movie_data:
                    movie_id, url, file_id = movie_data
                    value_to_send = file_id if file_id else url
                    num_notified = await notify_users_for_movie(context, movie_title, value_to_send)

                    await query.edit_message_text(
                        f"✅ FULFILLED: Movie '{movie_title}' updated and user notified ({num_notified} total users).",
                        parse_mode='Markdown'
                    )
                else:
                    await query.edit_message_text(f"❌ ERROR: Movie '{movie_title}' not found in database.", parse_mode='Markdown')

                cur.close()
                conn.close()
            else:
                await query.edit_message_text("❌ Database error during fulfillment.")

        elif query.data.startswith("admin_delete_"):
            parts = query.data.split('_', 2)
            user_id = int(parts[1])
            movie_title = parts[2]

            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM user_requests WHERE user_id = %s AND movie_title = %s", (user_id, movie_title))
                conn.commit()
                cur.close()
                conn.close()
                await query.edit_message_text(f"❌ DELETED: Request for '{movie_title}' removed.", parse_mode='Markdown')
            else:
                await query.edit_message_text("❌ Database error during deletion.")

        elif query.data.startswith("quality_"):
            parts = query.data.split('_')
            movie_id = int(parts[1])
            selected_quality = parts[2]

            movie_data = context.user_data.get('selected_movie_data')

            if not movie_data or movie_data.get('id') != movie_id:
                qualities = get_all_movie_qualities(movie_id)
                movie_data = {'id': movie_id, 'title': 'Movie', 'qualities': qualities}

            if not movie_data or 'qualities' not in movie_data:
                await query.edit_message_text("❌ Error: Could not retrieve movie data. Please search again.")
                return

            chosen_file = None
            for quality, url, file_id in movie_data['qualities']:
                if quality == selected_quality:
                    chosen_file = {'url': url, 'file_id': file_id}
                    break

            if not chosen_file:
                 await query.edit_message_text("❌ Error fetching the file for that quality.")
                 return

            title = movie_data['title']

            await query.edit_message_text(f"✅ Sending **{title}** in **{selected_quality}**...", parse_mode='Markdown')

            await send_movie_to_user(
                update,
                context,
                movie_id,
                title,
                chosen_file['url'],
                chosen_file['file_id']
            )

            if 'selected_movie_data' in context.user_data:
                del context.user_data['selected_movie_data']

        elif query.data.startswith("page_"):
            page = int(query.data.replace("page_", ""))

            if 'search_results' not in context.user_data:
                await query.edit_message_text("❌ Search results expired. Please search again.")
                return

            movies = context.user_data['search_results']
            search_query = context.user_data.get('search_query', 'your search')

            selection_text = f"🎬 **Found {len(movies)} movies matching '{search_query}'**\n\nPlease select the movie you want:"
            keyboard = create_movie_selection_keyboard(movies, page=page)

            await query.edit_message_text(
                selection_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )

        elif query.data == "cancel_selection":
            await query.edit_message_text("❌ Selection cancelled.")
            if 'search_results' in context.user_data:
                del context.user_data['search_results']
            if 'search_query' in context.user_data:
                del context.user_data['search_query']
            if 'selected_movie_data' in context.user_data:
                del context.user_data['selected_movie_data']

        elif query.data.startswith("request_"):
            movie_title = query.data.replace("request_", "")
            user = update.effective_user

            store_user_request(
                user.id,
                user.username,
                user.first_name,
                movie_title,
                update.effective_chat.id if update.effective_chat.type != "private" else None,
                query.message.message_id
            )

            if ADMIN_CHANNEL_ID:
                await send_admin_notification(context, user, movie_title)

            await query.edit_message_text(f"✅ Got it! Your request for '{movie_title}' has been sent to the admin!")

        elif query.data.startswith("download_"):
            movie_title = query.data.replace("download_", "")

            conn = get_db_connection()
            if not conn:
                await query.answer("❌ Database connection failed.", show_alert=True)
                return

            cur = conn.cursor()
            cur.execute("SELECT id, title, url, file_id FROM movies WHERE title ILIKE %s LIMIT 1", (f'%{movie_title}%',))
            movie = cur.fetchone()
            cur.close()
            conn.close()

            if movie:
                movie_id, title, url, file_id = movie
                await send_movie_to_user(update, context, movie_id, title, url, file_id)
            else:
                await query.answer("❌ Movie not found.", show_alert=True)

    except Exception as e:
        logger.error(f"Error in button callback: {e}")
        try:
            await query.answer(f"❌ Error: {str(e)}", show_alert=True)
        except:
            pass

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the current operation"""
    await update.message.reply_text("Operation cancelled.", reply_markup=get_main_keyboard())
    return MAIN_MENU

# ==================== FLASK APP ====================
flask_app = Flask('')

@flask_app.route('/')
def home():
    return "Bot is running!"

@flask_app.route('/health')
def health():
    return "OK", 200

@flask_app.route(f'/{UPDATE_SECRET_CODE}')
def trigger_update():
    result = update_movies_in_db()
    return result

def run_flask():
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
    logger.info("Bot is starting...")

    if not TELEGRAM_BOT_TOKEN:
        logger.error("No Telegram bot token found. Exiting.")
        return

    try:
        setup_database()
    except Exception as e:
        logger.error(f"Database setup failed but continuing: {e}")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).read_timeout(30).write_timeout(30).build()

    # Add group message handler
    application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, handle_group_message))

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            MAIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu)],
            SEARCHING: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_movies)],
            REQUESTING: [MessageHandler(filters.TEXT & ~filters.COMMAND, request_movie)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_message=False,
        per_chat=True,
    )

    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(conv_handler)

    # Admin commands (keep your existing admin commands here)
    # ... [Your existing admin command handlers] ...

    # Start Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    logger.info("Flask server started in a background thread.")

    # Run the bot
    logger.info("Starting bot polling...")
    application.run_polling()

if __name__ == '__main__':
    main()
