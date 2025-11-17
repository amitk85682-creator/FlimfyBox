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
import random # FIX 1: Added import random
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
# Channel URL for the "Join" button
FILMFYBOX_CHANNEL_URL = os.environ.get('FILMFYBOX_CHANNEL_URL', 'http://t.me/filmfybox')


# Rate limiting dictionary
user_last_request = defaultdict(lambda: datetime.min) # FIX 2: Removed duplicate definition

# ===== New / Configurable rate-limiting and fuzzy settings =====
REQUEST_COOLDOWN_MINUTES = int(os.environ.get('REQUEST_COOLDOWN_MINUTES', '10'))  # per-user cooldown for same/similar movie
SIMILARITY_THRESHOLD = int(os.environ.get('SIMILARITY_THRESHOLD', '80'))        # fuzzy similarity % to consider titles "same"
MAX_REQUESTS_PER_MINUTE = int(os.environ.get('MAX_REQUESTS_PER_MINUTE', '10'))  # burst limit per user

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
    """Normalize title for fuzzy matching (lowercase, remove extra spaces and punctuation)."""
    if not title:
        return ""
    t = re.sub(r'[^\w\s]', ' ', title)  # remove punctuation
    t = re.sub(r'\s+', ' ', t).strip()
    return t.lower()

def get_last_similar_request_for_user(user_id: int, title: str, minutes_window: int = REQUEST_COOLDOWN_MINUTES):
    """
    Look up the user's most recent request that is sufficiently similar to `title`
    AND within the specified minutes_window. Returns a dict with stored_title, requested_at, score or None.
    """
    conn = get_db_connection()
    if not conn:
        return None

    try:
        cur = conn.cursor()
        # Get recent requests by this user (limit to last 200 to keep quick)
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
            # Only consider requests inside the time window
            try:
                if isinstance(requested_at, datetime):
                    requested_time = requested_at
                else:
                    # attempt parse if needed (fallback)
                    requested_time = datetime.strptime(str(requested_at), '%Y-%m-%d %H:%M:%S')
            except Exception:
                requested_time = requested_at  # if something odd, still try to compare

            if requested_time < cutoff:
                # since rows are ordered by requested_at DESC, once we hit older than cutoff we can break
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
    """Count how many requests this user made in the last window_seconds."""
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
        # ...
        # Enable pg_trgm extension
        cur.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm;')

        # Create movies table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS movies (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                file_id TEXT
            )
        ''')

        # Create sync_info table
        cur.execute('CREATE TABLE IF NOT EXISTS sync_info (id SERIAL PRIMARY KEY, last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP);')

        # Create user_requests table
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

        # Create movie_aliases table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS movie_aliases (
                id SERIAL PRIMARY KEY,
                movie_id INTEGER REFERENCES movies(id) ON DELETE CASCADE,
                alias TEXT NOT NULL,
                UNIQUE(movie_id, alias)
            )
        ''')

        # Add unique constraint
        cur.execute('''
            DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'user_requests_unique_constraint') THEN
                ALTER TABLE user_requests ADD CONSTRAINT user_requests_unique_constraint UNIQUE (user_id, movie_title);
            END IF;
            END $$;
        ''')

        # Add missing columns if they don't exist
        try:
            cur.execute("ALTER TABLE movies ADD COLUMN IF NOT EXISTS file_id TEXT;")
        except Exception as e:
            logger.info("file_id column already exists or couldn't be added")

        try:
            cur.execute("ALTER TABLE user_requests ADD COLUMN IF NOT EXISTS message_id BIGINT;")
        except Exception as e:
            logger.info("message_id column already exists or couldn't be added")

        # Create indexes
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
            logger.error("No database URL configured (FIXED_DATABASE_URL or DATABASE_URL).")
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

        # Get last sync time
        cur.execute("SELECT last_sync FROM sync_info ORDER BY id DESC LIMIT 1;")
        last_sync_result = cur.fetchone()
        last_sync_time = last_sync_result[0] if last_sync_result else None

        cur.execute("SELECT title FROM movies;")
        existing_movies = {row[0] for row in cur.fetchall()}

        # Only proceed if Blogger API keys are available
        if not BLOGGER_API_KEY or not BLOG_ID:
            return "Blogger API keys not configured"

        service = build('blogger', 'v3', developerKey=BLOGGER_API_KEY)
        all_items = []

        # Fetch all posts
        posts_request = service.posts().list(blogId=BLOG_ID, maxResults=500)
        while posts_request is not None:
            posts_response = posts_request.execute()
            all_items.extend(posts_response.get('items', []))
            posts_request = service.posts().list_next(posts_request, posts_response)

        # Fetch all pages
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
                    conn.rollback() # Rollback transaction on error
                    continue

        # Update sync time
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

        # First try exact matches using ILIKE
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

        # Then try alias matches
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

        # Finally fuzzy matching
        cur.execute("SELECT id, title, url, file_id FROM movies")
        all_movies = cur.fetchall()

        if not all_movies:
            cur.close()
            conn.close()
            return []

        # Create dictionary for fuzzy matching
        movie_titles = [movie[1] for movie in all_movies]
        movie_dict = {movie[1]: movie for movie in all_movies}

        # Use fuzzy matching
        matches = process.extract(user_query, movie_titles, scorer=fuzz.token_sort_ratio, limit=limit)

        # Filter matches with score >= 65
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

# ==================== STORE USER REQUEST (fixed) ====================
def store_user_request(user_id, username, first_name, movie_title, group_id=None, message_id=None):
    """Store user request in database. Uses ON CONFLICT DO UPDATE to refresh timestamp for exact duplicates."""
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

# ==================== NOTIFICATION FUNCTIONS (updated with caption support) ====================
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
🎬 **NEW MOVIE REQUEST!** 🎬

**Movie:** {movie_title}
{user_info}
{group_info_text}
Time: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}
        """

        # FIX: Send notification with inline keyboard
        await context.bot.send_message(
            chat_id=ADMIN_CHANNEL_ID,
            text=message,
            reply_markup=get_admin_request_keyboard(user.id, movie_title), # Pass user ID and title
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
    """Notify users who requested a movie — add caption when sending media."""
    logger.info(f"Attempting to notify users for movie: {movie_title}")
    conn = None
    cur = None
    notified_count = 0

    caption_text = (
    f"🎬 <b>{movie_title}</b>\n\n"
    "🔗 <b>JOIN »</b> FilmfyBox (http://t.me/filmfybox)\n\n"
    "🔹 <b>Please drop the movie name, and I’ll find it for you as soon as possible. 🎬✨👇</b>\n"
    "🔹 <b>FlimfyBox Chat (https://t.me/Filmfybox002)</b>"
)
    # Keyboard with a "Join Channel" button, to be attached to the media message
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
                # First send a small text notifying them
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎉 Hey {first_name or username}! Your requested movie '{movie_title}' is now available!"
                )

                warning_msg = await context.bot.send_message(
                    chat_id=user_id,
                    text="⚠️ ❌👉This file automatically❗️delete after 1 minute❗️so please forward in another chat👈❌",
                    parse_mode='Markdown'
                )

                sent_msg = None

                # Check if it's a Telegram file ID
                if isinstance(movie_url_or_file_id, str) and any(movie_url_or_file_id.startswith(prefix) for prefix in ["BQAC", "BAAC", "CAAC", "AQAC"]):
                    sent_msg = await context.bot.send_document(
                        chat_id=user_id,
                        document=movie_url_or_file_id,
                        caption=caption_text,
                        parse_mode='HTML',
                        reply_markup=join_channel_keyboard
                    )
                # Check if it's a Telegram channel link
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
                # Check if it's a regular HTTP URL
                elif isinstance(movie_url_or_file_id, str) and movie_url_or_file_id.startswith("http"):
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"🎬 {movie_title} is now available!\n\n{caption_text}",
                        reply_markup=get_movie_options_keyboard(movie_title, movie_url_or_file_id),
                        parse_mode='HTML'
                    )
                else: # Fallback for other cases, assuming it might be a file_id
                    sent_msg = await context.bot.send_document(
                        chat_id=user_id,
                        document=movie_url_or_file_id,
                        caption=caption_text,
                        parse_mode='HTML',
                        reply_markup=join_channel_keyboard
                    )

                # Auto-delete
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
                await asyncio.sleep(0.1) # Avoid hitting rate limits

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

        # Group users by their group_id
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
                    mention = f"[{first_name or username}](tg://user?id={user_id})"
                    user_mentions.append(mention)
                    notified_users_ids.append(user_id)

                notification_text += ", ".join(user_mentions)
                notification_text += f"\n\nआपकी फिल्म '{movie_title}' अब उपलब्ध है! इसे पाने के लिए, कृपया मुझे private [...]"

                await context.bot.send_message(
                    chat_id=group_id,
                    text=notification_text,
                    parse_mode='Markdown'
                )

                # Update notified status for all users in the group notification
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

# ==================== KEYBOARD MARKUPS ====================
def get_main_keyboard():
    """Get the main menu keyboard"""
    keyboard = [
        ['🔍 Search Movies', '🙋 Request Movie'],
        ['📊 My Stats', '❓ Help']
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def get_admin_request_keyboard(user_id, movie_title):
    """Inline keyboard for admin actions on a user request."""
    # Note: movie_title needs to be sanitized for callback data length (max 64 bytes)
    # We use a short version of the title and the user ID to trace the request back.

    sanitized_title = movie_title[:30]

    keyboard = [
        [InlineKeyboardButton("✅ FULFILL MOVIE", callback_data=f"admin_fulfill_{user_id}_{sanitized_title}")],
        [InlineKeyboardButton("❌ IGNORE/DELETE", callback_data=f"admin_delete_{user_id}_{sanitized_title}")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_movie_options_keyboard(movie_title, url):
    """Get inline keyboard for movie options, now with Join Channel button."""
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

    # Add movie buttons
    for movie in current_movies:
        movie_id, title, url, file_id = movie
        # Show title with truncation if too long
        button_text = title if len(title) <= 40 else title[:37] + "..."
        # Use movie ID as callback data for uniqueness
        keyboard.append([InlineKeyboardButton(
            f"🎬 {button_text}",
            callback_data=f"movie_{movie_id}"
        )])

    # Add navigation buttons if needed
    nav_buttons = []
    total_pages = (len(movies) + movies_per_page - 1) // movies_per_page

    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Previous", callback_data=f"page_{page-1}"))

    if end_idx < len(movies):
        nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"page_{page+1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    # Add cancel button
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_selection")])

    return InlineKeyboardMarkup(keyboard)

def get_all_movie_qualities(movie_id):
    """Fetch all available qualities (URL/File ID) for a given movie ID."""
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
    """Create inline keyboard with quality selection buttons."""
    keyboard = []

    # Store movie info for later use (needed in callback)
    # We store the title to ensure we can use it in the final send message even if quality selection takes time
    # This stores the state in user_data

    for quality, url, file_id in qualities:
        # Callback format: 'quality_<movie_id>_<quality>'
        callback_data = f"quality_{movie_id}_{quality}"
        button_text = f"🎬 {quality} ({'File' if file_id else 'Link'})"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])

    # Add cancel button
    keyboard.append([InlineKeyboardButton("❌ Cancel Selection", callback_data="cancel_selection")])

    return InlineKeyboardMarkup(keyboard)

# ==================== HELPER FUNCTION (FIXED FOR QUALITY CHOICE) ====================
async def send_movie_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: int, title: str, url: Optional[str] = None, file_id: Optional[str] = None):
    """
    Sends the movie file/link to the user with a warning and caption.
    This function expects the specific URL/File ID to be passed as arguments.
    """
    chat_id = update.effective_chat.id

    # ------------------- DATA FALLBACK REMOVED / CHECK -------------------
    # This logic is now handled in button_callback where the user selects the quality.
    # If the initial call (from search_movies single result) has no URL/File_ID,
    # we need to check if multi-quality files exist and prompt the user.
    if not url and not file_id:
        qualities = get_all_movie_qualities(movie_id)
        if qualities:
            # Re-engage the user for selection if files exist in multi-quality table
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
            return # Exit after sending the selection prompt
    # ----------------------------------------------------------------------


    try:
        # Initial warning (auto-delete with media if media sent)
        warning_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ ❌👉This file automatically❗️deletes after 1 minute❗️so please forward it to another chat👈❌",
            parse_mode='Markdown'
        )

        sent_msg = None
        name = title  # Use 'title' from the function arguments for the caption
        caption_text = (
            f"🎬 <b>{name}</b>\n\n"
            "🔗 <b>JOIN »</b> <a href='http://t.me/filmfybox'>FilmfyBox</a>\n\n"
            "🔹 <b>Please drop the movie name, and I’ll find it for you as soon as possible. 🎬✨👇</b>\n"
            "🔹 <b><a href='https://t.me/Filmfybox002'>FlimfyBox Chat</a></b>"
        )
        
        # Keyboard with a "Join Channel" button, to be attached to the media message
        join_channel_keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔗 Join Channel", url=FILMFYBOX_CHANNEL_URL)
        ]])


        # 1) file_id -> caption attached under media
        if file_id:
            sent_msg = await context.bot.send_document(
                chat_id=chat_id,
                document=file_id,
                caption=caption_text,
                parse_mode='HTML',
                reply_markup=join_channel_keyboard
            )

        # 2) Private channel message link: t.me/c/<chat_id>/<msg_id>
        elif url and url.startswith("https://t.me/c/"):
            try:
                parts = url.rstrip('/').split('/')
                from_chat_id = int("-100" + parts[-2])
                message_id = int(parts[-1])
                # Attach caption directly via copy_message
                sent_msg = await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                    caption=caption_text,
                    parse_mode='HTML',
                    reply_markup=join_channel_keyboard
                )
            except Exception as e:
                logger.error(f"Copy private link failed {url}: {e}")
                # Fallback to sending a message with inline keyboard if copy fails
                sent_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🎬 Found: {name}\n\n{caption_text}",
                    reply_markup=get_movie_options_keyboard(name, url),
                    parse_mode='HTML'
                )

        # 3) Public channel message link: https://t.me/Username/123
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
                    parse_mode='HTML',
                    reply_markup=join_channel_keyboard
                )
            except Exception as e:
                logger.error(f"Copy public link failed {url}: {e}")
                # Fallback to sending a message with inline keyboard if copy fails
                sent_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🎬 Found: {name}\n\n{caption_text}",
                    reply_markup=get_movie_options_keyboard(name, url),
                    parse_mode='HTML'
                )

        # 4) Normal external link
        elif url and url.startswith("http"):
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"🎉 Found it! '{name}' is available!\n\n{caption_text}",
                reply_markup=get_movie_options_keyboard(name, url),
                parse_mode='HTML'
            )

        # 5) Nothing valid to send
        else:
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Sorry, '{name}' found but no valid file or link is attached in the database."
            )

        # Auto-delete for media + warning
        if sent_msg:
            message_ids_to_delete = [warning_msg.message_id, sent_msg.message_id]

            asyncio.create_task(
                delete_messages_after_delay(
                    context,
                    chat_id,
                    message_ids_to_delete,
                    60 # 60 seconds delay
                )
            )

    except Exception as e:
        logger.error(f"Error sending movie to user: {e}")
        try:
            await context.bot.send_message(chat_id=chat_id, text="❌ Server failed to send file. Please report to Admin.")
        except Exception as e2:
            logger.error(f"Secondary send error: {e2}")

# ==================== TELEGRAM BOT HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler, now with deep linking for movie delivery."""
    # Check for deep link payload (e.g., from a group button click)
    if context.args and context.args[0].startswith("movie_"):
        try:
            movie_id = int(context.args[0].split('_')[1])
            conn = get_db_connection()
            if not conn:
                await update.message.reply_text("Database connection failed. Please try again.")
                return MAIN_MENU

            cur = conn.cursor()
            # Fetch all details needed for send_movie_to_user
            cur.execute("SELECT title, url, file_id FROM movies WHERE id = %s", (movie_id,))
            movie_data = cur.fetchone()
            cur.close()
            conn.close()

            if movie_data:
                title, url, file_id = movie_data
                # Directly call the function to send the movie to the user
                await send_movie_to_user(update, context, movie_id, title, url, file_id)
                # After sending, show the main menu
                await update.message.reply_text("What would you like to do next?", reply_markup=get_main_keyboard())
                return MAIN_MENU
            else:
                await update.message.reply_text("Sorry, I couldn't find the movie associated with that link. It might have been removed.")
        except (IndexError, ValueError, Exception) as e:
            logger.error(f"Error processing deep link: {e}")
            await update.message.reply_text("Sorry, there was an error processing your link.")
        
        return MAIN_MENU

    # If no deep link, show the standard welcome message
    try:
        welcome_text = """
📨 Sᴇɴᴅ Mᴏᴠɪᴇ Oʀ Sᴇʀɪᴇs Nᴀᴍᴇ ᴀɴᴅ Yᴇᴀʀ Aꜱ Pᴇʀ Gᴏᴏɢʟᴇ Sᴘᴇʟʟɪɴɢ..!! 👍

⚠️ Exᴀᴍᴘʟᴇ Fᴏʀ Mᴏᴠɪᴇ 👇

👉 Jailer
👉 Jailer 2023

⚠️ Exᴀᴍᴘʟᴇ Fᴏʀ WᴇʙSᴇʀɪᴇs 👇

👉 Stranger Things
👉 Stranger Things S02 E04

⚠️ ᴅᴏɴ'ᴛ ᴀᴅᴅ ᴇᴍᴏᴊɪꜱ ᴀɴᴅ ꜱʏᴍʙᴏʟꜱ ɪɴ ᴍᴏᴠɪᴇ ɴᴀᴍᴇ, ᴜꜱᴇ ʟᴇᴛᴛᴇʀꜱ ᴏɴʟʏ..!! ❌
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
                    # FIX: Fetch the first element from the tuple
                    request_count = cur.fetchone()[0]

                    cur.execute("SELECT COUNT(*) FROM user_requests WHERE user_id = %s AND notified = TRUE", (user_id,))
                    # FIX: Fetch the first element from the tuple
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
🤖 How to use FilmfyBox Bot:

🔍 Search Movies: Find movies in our collection
🙋 Request Movie: Request a new movie to be added
📊 My Stats: View your request statistics

Just use the buttons below to navigate!
            """
            await update.message.reply_text(help_text)
            return MAIN_MENU
        else:
            # If the text is not a menu option, treat it as a search
            return await search_movies(update, context)

    except Exception as e:
        logger.error(f"Error in main menu: {e}")
        return MAIN_MENU

async def search_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle movie search with multiple results support"""
    try:
        # Rate limiting
        if not await check_rate_limit(update.effective_user.id):
            await update.message.reply_text("⚠️ Please wait a moment before searching again.")
            return SEARCHING

        user_message = update.message.text.strip()
        processed_query = preprocess_query(user_message) if user_message else user_message
        search_query = processed_query if processed_query else user_message

        # Search for MULTIPLE movies in database
        movies_found = get_movies_from_db(search_query, limit=10)

        if not movies_found:
            # Movie not found - store request
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

            # --- 1. Send a single, hardcoded GIF ---
            try:
                gif_msg = await context.bot.send_animation(
                    chat_id=update.effective_chat.id,
                    animation='CgACAgQAAxkBAAECz0ppEaLwgDbNfPPFl5lgtFjjmztKKgAC5wIAAmaoDVMH7bkdAqNVnDYE', # Hardcoded GIF ID
                    caption="🎬 **Movie Search Tips** 🔍",
                    parse_mode='Markdown'
                )
                messages_to_delete.append(gif_msg.message_id)
            except Exception as e:
                logger.error(f"Failed to send hardcoded animation: {e}")

            # --- 2. Request button ---
            request_btn_msg = await update.message.reply_text(
                f"😔 Sorry, '{user_message}' is not in my collection right now. Would you like to request it?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Yes, Request It", callback_data=f"request_{user_message[:50]}")
                ]])
            )
            messages_to_delete.append(request_btn_msg.message_id)


            # --- 3. Search Tip message ---
            error_msg = """
Mᴏᴠɪᴇ ᴋɪ sᴘᴇʟʟɪɴɢ Gᴏᴏɢʟᴇ ᴘᴀʀ sᴇᴀʀᴄʜ ᴋᴀʀᴋᴇ, ᴄᴏᴘʏ ᴋᴀʀᴇ, ᴜsᴋᴇ ʙᴀᴀᴅ ʏᴀʜᴀ́ ᴛʏᴘᴇ/PAST ᴋᴀʀᴇ́. ✔️

Bᴀs ᴍᴏᴠɪᴇ ᴋᴀ ɴᴀᴍᴇ + ʏᴇᴀʀ (ᴏʀ Sᴇʀɪᴇs Sᴇᴀsᴏɴ/Eᴘɪsᴏᴅᴇ) ʟɪᴋʜᴇ́, ᴜsᴋᴇ ᴀᴀɢᴇ ᴘɪᴄʜʜᴇ ᴋᴜᴄʜʜ ʙʜɪ ɴᴀ ʟɪᴋʜᴇ́. ❌

---
**📝 Example:**
**Sᴀʜɪ ʜᴀɪ! 👇**
👉 `KGF 2`
👉 `Asur S01 E03`

**Gᴀʟᴀᴛ ʜᴀɪ! 🙅**
❌ `KGF 2 Movie`
❌ `Asur Season 3 Download`
---

**Dᴏɴ’T ᴀᴅᴅ Eᴍᴏᴊɪs ᴀɴᴅ Sʏᴍʙᴏʟs ɪɴ Mᴏᴠɪᴇ Nᴀᴍᴇs!** ⚠️
"""
            tip_msg = await update.message.reply_text(error_msg, parse_mode='Markdown')
            messages_to_delete.append(tip_msg.message_id)

            # --- 4. Auto-Delete Task ---
            if messages_to_delete:
                asyncio.create_task(
                    delete_messages_after_delay(
                        context,
                        update.effective_chat.id,
                        messages_to_delete,
                        60 # 60 seconds delay for deletion
                    )
                )

        elif len(movies_found) == 1:
            movie_id, title, url, file_id = movies_found[0]
            # When only one result, check for multi-quality files within send_movie_to_user
            await send_movie_to_user(update, context, movie_id, title, url, file_id)

        else:
            # Multiple movies found - show selection menu
            context.user_data['search_results'] = movies_found
            context.user_data['search_query'] = user_message

            selection_text = f"🎬 **Found {len(movies_found)} movies matching '{user_message}'**\n\nPlease select the movie you want:"
            keyboard = create_movie_selection_keyboard(movies_found, page=0)

            await update.message.reply_text(
                selection_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )

        await update.message.reply_text("Ab Aap Aage kya karana chaahenge?", reply_markup=get_main_keyboard())
        return MAIN_MENU

    except Exception as e:
        logger.error(f"Error in search movies: {e}")
        await update.message.reply_text("Sorry, something went wrong. Please try again.")
        return MAIN_MENU

# ==================== REQUEST MOVIE (updated) ====================
async def request_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle movie requests with duplicate detection, fuzzy matching and cooldowns."""
    try:
        user_message = (update.message.text or "").strip()
        user = update.effective_user

        if not user_message:
            await update.message.reply_text(" कृपया मूवी का नाम भेजें।")
            return REQUESTING

        # 1) Quick burst protection (many different requests in short time)
        burst = user_burst_count(user.id, window_seconds=60)
        if burst >= MAX_REQUESTS_PER_MINUTE:
            await update.message.reply_text(
                "🛑 तुम बहुत जल्दी-जल्दी requests भेज रहे हो। कुछ देर रोकें (कुछ मिनट) और फिर कोशिश करें।\n"
                "बार‑बार भेजने से फ़ायदा नहीं होगा।"
            )
            return REQUESTING

        # 2) Analyze intent (existing AI helper) — fallback to plain text if API unavailable
        intent = await analyze_intent(user_message)
        if not intent["is_request"]:
            await update.message.reply_text("यह एक मूवी/सीरीज़ का नाम नहीं लग रहा है। कृपया सही नाम भेजें।")
            return REQUESTING

        movie_title = intent["content_title"] or user_message

        # 3) Check if the same user already requested a VERY SIMILAR title recently
        similar = get_last_similar_request_for_user(user.id, movie_title, minutes_window=REQUEST_COOLDOWN_MINUTES)
        if similar:
            last_time = similar.get("requested_at")
            elapsed = datetime.now() - last_time
            minutes_passed = int(elapsed.total_seconds() / 60)
            minutes_left = max(0, REQUEST_COOLDOWN_MINUTES - minutes_passed)
            if minutes_left > 0:
                # Send strict message preventing duplicate requests
                strict_text = (
                    "🛑 Ruk jao! Aapne ye request abhi bheji thi.\n\n"
                    "Baar‑baar request karne se movie jaldi nahi aayegi.\n\n"
                    f"Similar previous request: \"{similar.get('stored_title')}\" ({similar.get('score')}% match)\n"
                    f"Kripya {minutes_left} minute baad dobara koshish karein. 🙏"
                )
                await update.message.reply_text(strict_text)
                return REQUESTING
            # else: cooldown expired -> allow storing new request (maybe user still wants)

        # 4) If no recent similar, store request and notify admin
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
            await update.message.reply_text("Sorry, आपका request store नहीं हो पाया। बाद में कोशिश करें।")
            return REQUESTING

        # Send admin notification
        group_info = update.effective_chat.title if update.effective_chat.type != "private" else None
        await send_admin_notification(context, user, movie_title, group_info)

        # Thank the user
        await update.message.reply_text(
            f"✅ Got it! Your request for '{movie_title}' has been sent. I'll let you know when it's available.",
            reply_markup=get_main_keyboard()
        )

        return MAIN_MENU

    except Exception as e:
        logger.error(f"Error in request_movie: {e}")
        await update.message.reply_text("Sorry, an error occurred while processing your request.")
        return REQUESTING

# ==================== NEW: GROUP MESSAGE HANDLER ====================
async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Listens to all messages in a group, and if a message matches a movie
    in the database with high confidence, it prompts the user.
    """
    # Ensure it's a text message and not from the bot itself
    if not update.message or not update.message.text or update.message.from_user.is_bot:
        return

    message_text = update.message.text.strip()
    user = update.effective_user

    # Avoid triggering on very short messages or commands
    if len(message_text) < 4 or message_text.startswith('/'):
        return

    # Use a precise search to find one potential match
    movies_found = get_movies_from_db(message_text, limit=1)

    if movies_found:
        # Check if the match is good enough to avoid false positives
        match_title = movies_found[0][1]
        score = fuzz.token_sort_ratio(_normalize_title_for_match(message_text), _normalize_title_for_match(match_title))

        # Use a high confidence threshold for group prompts
        if score > 90:
            movie_id, title, _, _ = movies_found[0]

            # To avoid spamming, check if this user was prompted for this movie recently
            prompt_key = f"prompt_{user.id}_{movie_id}"
            now = datetime.now()
            last_prompt_time = context.chat_data.get(prompt_key)

            if last_prompt_time and (now - last_prompt_time) < timedelta(minutes=10):
                logger.info(f"Skipping prompt for user {user.id} for movie {movie_id} due to recent prompt.")
                return  # Don't prompt again so soon

            context.chat_data[prompt_key] = now

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, get this movie", callback_data=f"group_get_{movie_id}_{user.id}")
            ]])

            # Reply to the user's message, mentioning them
            reply_msg = await update.message.reply_text(
                text=f"Hey {user.mention_markdown()}, are you looking for **{title}**? Click the button to get it.",
                reply_markup=keyboard,
                parse_mode='Markdown'
            )

            # Schedule the prompt message to be deleted after 2 minutes to keep the chat clean
            asyncio.create_task(
                delete_messages_after_delay(context, update.effective_chat.id, [reply_msg.message_id], delay=120)
            )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks - INCLUDING MOVIE SELECTION"""
    try:
        query = update.callback_query
        await query.answer()

        # --- NEW: Handle "Get Movie" from a group prompt ---
        if query.data.startswith("group_get_"):
            # Format: group_get_{movie_id}_{user_id}
            try:
                _, movie_id_str, original_user_id_str = query.data.split('_')
                movie_id = int(movie_id_str)
                original_user_id = int(original_user_id_str)
            except ValueError:
                await query.edit_message_text("❌ Error: Invalid button data.")
                return

            # Security Check: Only the user who was prompted can click the button.
            if query.from_user.id != original_user_id:
                await query.answer("This button is not for you.", show_alert=True)
                return

            # Get bot's username to create a deep link to the private chat
            bot_username = (await context.bot.get_me()).username
            deep_link = f"https://t.me/{bot_username}?start=movie_{movie_id}"

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🤖 Start Chat & Get Movie", url=deep_link)
            ]])

            # Edit the original message in the group to provide the private chat link
            await query.edit_message_text(
                "Great! Click the button below to go to our private chat, and I'll send you the movie.",
                reply_markup=keyboard
            )
            return

        # Handle movie selection (Now prompts for quality)
        if query.data.startswith("movie_"):
            movie_id = int(query.data.replace("movie_", ""))

            # Get movie title
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

            # Fetch all available qualities from movie_files table
            qualities = get_all_movie_qualities(movie_id)

            # If only one quality is found OR no qualities, still proceed to check for old links
            if not qualities:
                # Fallback: Call send_movie_to_user with movie_id, hoping the main movie table has the link
                await query.edit_message_text(f"✅ You selected: **{title}**\n\nSending movie (single quality)...", parse_mode='Markdown')
                # Fetch base URL/File_ID from movies table for this ID
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT url, file_id FROM movies WHERE id = %s", (movie_id,))
                url, file_id = cur.fetchone() or (None, None)
                cur.close()
                conn.close()

                await send_movie_to_user(update, context, movie_id, title, url, file_id)
                return

            # Store available qualities in user_data for the next callback step
            context.user_data['selected_movie_data'] = {
                'id': movie_id,
                'title': title,
                'qualities': qualities
            }

            selection_text = f"✅ You selected: **{title}**\n\n⬇️ **Please choose the file quality:**"
            keyboard = create_quality_selection_keyboard(movie_id, title, qualities)

            # Edit the original selection message to show quality options
            await query.edit_message_text(
                selection_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )

        # --- NEW ADMIN HANDLERS ---
        elif query.data.startswith("admin_fulfill_"):
            # Format: 'admin_fulfill_<user_id>_<movie_title>'
            parts = query.data.split('_', 2)
            user_id = int(parts[1])
            movie_title = parts[2]

            # 1. Update the request status (SET notified=TRUE)
            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                # Find movie in movies table
                cur.execute("SELECT id, url, file_id FROM movies WHERE title = %s LIMIT 1", (movie_title,))
                movie_data = cur.fetchone()

                if movie_data:
                    movie_id, url, file_id = movie_data
                    value_to_send = file_id if file_id else url

                    # Notify the single user (optional: update ALL pending requests for this movie)
                    num_notified = await notify_users_for_movie(context, movie_title, value_to_send)

                    await query.edit_message_text(
                        f"✅ FULFILLED: Movie '{movie_title}' updated and user (ID: {user_id}) notified ({num_notified} total users).",
                        parse_mode='Markdown'
                    )
                else:
                    # Only delete request in user_requests table if fulfill is successful
                    await query.edit_message_text(f"❌ ERROR: Movie '{movie_title}' not found in the `movies` table. Please add it first.", parse_mode='Markdown')

                cur.close()
                conn.close()
            else:
                await query.edit_message_text("❌ Database error during fulfillment.")

        elif query.data.startswith("admin_delete_"):
            # Format: 'admin_delete_<user_id>_<movie_title>'
            parts = query.data.split('_', 2)
            user_id = int(parts[1])
            movie_title = parts[2]

            # 1. Delete the specific user request from the database
            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                # Delete only the specific user's request for this exact title
                cur.execute("DELETE FROM user_requests WHERE user_id = %s AND movie_title = %s", (user_id, movie_title))
                conn.commit()
                cur.close()
                conn.close()
                await query.edit_message_text(f"❌ DELETED: Request for '{movie_title}' from User ID {user_id} removed.", parse_mode='Markdown')
            else:
                await query.edit_message_text("❌ Database error during deletion.")

        # --- END NEW ADMIN HANDLERS ---
        # --- NEW BLOCK FOR QUALITY SELECTION ---
        elif query.data.startswith("quality_"):
            # Format: 'quality_<movie_id>_<quality>'
            parts = query.data.split('_')
            movie_id = int(parts[1])
            selected_quality = parts[2]

            # Retrieve movie data from user_data
            movie_data = context.user_data.get('selected_movie_data')

            if not movie_data or movie_data.get('id') != movie_id:
                # Try fetching data again if session expired (less reliable but fallback)
                qualities = get_all_movie_qualities(movie_id)
                movie_data = {'id': movie_id, 'title': 'Movie', 'qualities': qualities}

            if not movie_data or 'qualities' not in movie_data:
                await query.edit_message_text("❌ Error: Could not retrieve movie data. Please search again.")
                return

            # Find the specific link/file_id for the chosen quality
            chosen_file = None
            for quality, url, file_id in movie_data['qualities']:
                if quality == selected_quality:
                    # Prefer file_id over URL for sending media
                    chosen_file = {'url': url, 'file_id': file_id}
                    break

            if not chosen_file:
                 await query.edit_message_text("❌ Error fetching the file for that quality.")
                 return

            title = movie_data['title']

            # Edit the quality selection message
            await query.edit_message_text(f"✅ Sending **{title}** in **{selected_quality}**...", parse_mode='Markdown')

            # Send the movie using the specific file/url
            await send_movie_to_user(
                update,
                context,
                movie_id,
                title,
                chosen_file['url'],
                chosen_file['file_id']
            )

            # Clear user data once transaction is complete
            if 'selected_movie_data' in context.user_data:
                del context.user_data['selected_movie_data']

        # Handle page navigation (remains the same)
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

        # Handle cancel selection
        elif query.data == "cancel_selection":
            await query.edit_message_text("❌ Selection cancelled.")
            # Clear stored search results
            if 'search_results' in context.user_data:
                del context.user_data['search_results']
            if 'search_query' in context.user_data:
                del context.user_data['search_query']
            if 'selected_movie_data' in context.user_data:
                del context.user_data['selected_movie_data']

        # Handle movie request confirmation
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

        # Handle download button
        elif query.data.startswith("download_"):
            movie_title = query.data.replace("download_", "")

            # Search for the movie
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
                # Direct download button will check for qualities within send_movie_to_user
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

# ==================== ADMIN COMMANDS ====================
async def add_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to add a movie manually"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Sorry, सिर्फ एडमिन ही इस कमांड का इस्तेमाल कर सकते हैं।")
        return

    conn = None
    try:
        parts = context.args
        if len(parts) < 2:
            await update.message.reply_text("गलत फॉर्मेट! ऐसे इस्तेमाल करें:\n/addmovie टाइटल का नाम [File ID या Link]")
            return

        value = parts[-1]
        title = " ".join(parts[:-1])

        logger.info(f"Adding movie: {title} with value: {value}")

        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("❌ Database connection failed.")
            return

        cur = conn.cursor()

        # Check if it's a Telegram file ID
        if any(value.startswith(prefix) for prefix in ["BQAC", "BAAC", "CAAC", "AQAC"]):
            cur.execute(
                "INSERT INTO movies (title, url, file_id) VALUES (%s, %s, %s) ON CONFLICT (title) DO UPDATE SET url = EXCLUDED.url, file_id = EXCLUDED.file_id",
                (title.strip(), "", value.strip())
            )
            message = f"✅ '{title}' को file ID के साथ सफलतापूर्वक जोड़ दिया गया है।"

        # Check if it's any kind of URL
        elif "http" in value or "." in value:
            normalized_url = value.strip()

            if not value.startswith(('http://', 'https://')):
                await update.message.reply_text("❌ Invalid URL format. URL must start with http:// or https://")
                return

            cur.execute(
                "INSERT INTO movies (title, url, file_id) VALUES (%s, %s, NULL) ON CONFLICT (title) DO UPDATE SET url = EXCLUDED.url, file_id = NULL",
                (title.strip(), normalized_url)
            )
            message = f"✅ '{title}' को URL के साथ सफलतापूर्वक जोड़ दिया गया है।"

        else:
            await update.message.reply_text("❌ Invalid format. कृपया सही File ID या URL दें।")
            return

        conn.commit()
        await update.message.reply_text(message)

        # --- IMPORTANT: Fetch the movie ID and details again after UPSET to use in notification ---
        cur.execute("SELECT id, title, url, file_id FROM movies WHERE title = %s", (title.strip(),))
        movie_found = cur.fetchone()

        if movie_found:
            movie_id, title, url, file_id = movie_found
            value_to_send = file_id if file_id else url

            # Notify users who requested this movie
            num_notified = await notify_users_for_movie(context, title, value_to_send)
            await notify_in_group(context, title)
            await update.message.reply_text(f"कुल {num_notified} users को notify किया गया है।")
        else:
             await update.message.reply_text("Notification failed: Could not retrieve updated movie details.")

    except Exception as e:
        logger.error(f"Error in add_movie command: {e}")
        await update.message.reply_text(f"एक एरर आया: {e}")
    finally:
        if conn:
            conn.close()

async def bulk_add_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add multiple movies at once"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Sorry, सिर्फ एडमिन ही इस कमांड का इस्तेमाल कर सकते हैं।")
        return

    try:
        full_text = update.message.text
        lines = full_text.split('\n')

        if len(lines) <= 1 and not context.args:
            await update.message.reply_text("""
गलत फॉर्मेट! ऐसे इस्तेमाल करें:

/bulkadd
Movie1 https://link1.com
Movie2 https://link2.com
Movie3 file_id_here
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
                results.append(f"❌ Invalid line format: {line}")
                continue

            url_or_id = parts[-1]
            title = ' '.join(parts[:-1])

            try:
                conn = get_db_connection()
                if not conn:
                    failed_count += 1
                    results.append(f"❌ {title} - Database connection failed")
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
            except Exception as e:
                failed_count += 1
                results.append(f"❌ {title} - Error: {str(e)}")

        result_message = f"""
📊 Bulk Add Results:

Successfully added: {success_count}
Failed: {failed_count}

Details:
""" + "\n".join(results[:10])

        if len(results) > 10:
            result_message += f"\n\n... और {len(results) - 10} more items"

        await update.message.reply_text(result_message)

    except Exception as e:
        logger.error(f"Error in bulk_add_movies: {e}")
        await update.message.reply_text(f"Bulk add में error: {e}")

async def add_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add an alias for an existing movie"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Sorry, सिर्फ एडमिन ही इस कमांड का इस्तेमाल कर सकते हैं।")
        return

    conn = None
    try:
        if not context.args or len(context.args) < 2:
            await update.message.reply_text("गलत फॉर्मेट! ऐसे इस्तेमाल करें:\n/addalias मूवी_का_असली_नाम alias_name")
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
            await update.message.reply_text(f"❌ '{movie_title}' डेटाबेस में नहीं मिली। पहले मूवी को add करें।")
            return

        movie_id = movie[0]

        cur.execute(
            "INSERT INTO movie_aliases (movie_id, alias) VALUES (%s, %s) ON CONFLICT (movie_id, alias) DO NOTHING",
            (movie_id, alias.lower())
        )

        conn.commit()
        await update.message.reply_text(f"✅ Alias '{alias}' successfully added for '{movie_title}'")

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
            await update.message.reply_text("कृपया मूवी का नाम दें:\n/aliases मूवी_का_नाम")
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
            WHERE m.title = %s
            GROUP BY m.title
        """, (movie_title,))

        result = cur.fetchone()

        if not result:
            await update.message.reply_text(f"'{movie_title}' डेटाबेस में नहीं मिली।")
            return

        title, aliases = result
        aliases_list = "\n".join(f"- {alias}" for alias in aliases) if aliases else "कोई aliases नहीं हैं"

        await update.message.reply_text(f"🎬 **{title}**\n\n**Aliases:**\n{aliases_list}", parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error listing aliases: {e}")
        await update.message.reply_text(f"Error: {e}")
    finally:
        if conn:
            conn.close()

async def bulk_add_aliases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add multiple aliases at once"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Sorry, सिर्फ एडमिन ही इस कमांड का इस्तेमाल कर सकते हैं।")
        return

    conn = None
    try:
        full_text = update.message.text
        lines = full_text.split('\n')

        if len(lines) <= 1 and not context.args:
            await update.message.reply_text("""
गलत फॉर्मेट! ऐसे इस्तेमाल करें:

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

            cur.execute("SELECT id FROM movies WHERE title = %s", (movie_title,))
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

Successfully added: {success_count}
Failed: {failed_count}
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
        await update.message.reply_text("Sorry, सिर्फ एडमिन ही इस कमांड का इस्तेमाल कर सकते हैं।")
        return

    try:
        if not context.args:
            await update.message.reply_text("Usage: /notify <movie_title>")
            return

        movie_title = " ".join(context.args)

        # Get movie from database
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
            await update.message.reply_text(f"{num_notified} users को '{title}' के लिए notify किया गया है।")
        else:
            await update.message.reply_text(f"'{movie_title}' डेटाबेस में नहीं मिली।")
    except Exception as e:
        logger.error(f"Error in notify_manually: {e}")
        await update.message.reply_text(f"एक एरर आया: {e}")

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
            await update.message.reply_text(f"❌ User `@{target_username}` not found in database.", parse_mode='Markdown')
            cur.close()
            conn.close()
            return

        user_id, first_name = user

        notification_text = f"📬 **Message from Admin**\n\n{message_text}"

        await context.bot.send_message(
            chat_id=user_id,
            text=notification_text,
            parse_mode='Markdown'
        )

        await update.message.reply_text(f"✅ Message sent to `@{target_username}` ({first_name})", parse_mode='Markdown')

        cur.close()
        conn.close()

    except telegram.error.Forbidden:
        await update.message.reply_text(f"❌ User blocked the bot.")
    except Exception as e:
        logger.error(f"Error in notify_user_by_username: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

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
            await update.message.reply_text("No users found in database.")
            cur.close()
            conn.close()
            return

        status_msg = await update.message.reply_text(
            f"📤 Broadcasting to {len(all_users)} users...\n⏳ Please wait..."
        )

        success_count = 0
        failed_count = 0

        broadcast_text = f"📢 **Broadcast Message**\n\n{message_text}"

        for user_id, first_name, username in all_users:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=broadcast_text,
                    parse_mode='Markdown'
                )
                success_count += 1
                await asyncio.sleep(0.05)
            except telegram.error.Forbidden:
                failed_count += 1
            except Exception as e:
                failed_count += 1
                logger.error(f"Failed broadcast to {user_id}: {e}")


        await status_msg.edit_text(
            f"📊 **Broadcast Complete**\n\n"
            f"✅ Sent: {success_count}\n"
            f"❌ Failed: {failed_count}\n"
            f"📝 Total: {len(all_users)}"
        )

        cur.close()
        conn.close()

    except Exception as e:
        logger.error(f"Error in broadcast_message: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

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
            await update.message.reply_text(f"❌ User `@{target_username}` not found.", parse_mode='Markdown')
            cur.close()
            conn.close()
            return

        user_id, first_name = user

        async def send_scheduled_notification():
            await asyncio.sleep(delay_minutes * 60)
            try:
                notification_text = f"⏰ **Scheduled Message**\n\n{message_text}"
                await context.bot.send_message(
                    chat_id=user_id,
                    text=notification_text,
                    parse_mode='Markdown'
                )
                logger.info(f"Scheduled notification sent to {user_id}")
            except Exception as e:
                logger.error(f"Failed to send scheduled notification to {user_id}: {e}")

        asyncio.create_task(send_scheduled_notification())

        await update.message.reply_text(
            f"⏰ Notification scheduled!\n\n"
            f"To: `@{target_username}` ({first_name})\n"
            f"Delay: {delay_minutes} minutes\n"
            f"Message: {message_text[:50]}...",
            parse_mode='Markdown'
        )

        cur.close()
        conn.close()

    except ValueError:
        await update.message.reply_text("❌ Invalid delay. Please provide number of minutes.")
    except Exception as e:
        logger.error(f"Error in schedule_notification: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def notify_user_with_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Notify user with media by replying to a message"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    try:
        if not update.message.reply_to_message:
            await update.message.reply_text(
                "❌ Please reply to a message (file/video/audio/photo) with:\n"
                "/notifyuserwithmedia @username Optional message"
            )
            return

        if not context.args:
            await update.message.reply_text(
                "Usage: /notifyuserwithmedia @username [optional message]\n"
                "Example: /notifyuserwithmedia @amit002 Here's your requested movie!"
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
            await update.message.reply_text(f"❌ User `@{target_username}` not found in database.", parse_mode='Markdown')
            cur.close()
            conn.close()
            return

        user_id, first_name = user

        notification_header = f"📬 **Message from Admin**\n"
        if optional_message:
            notification_header += f"\n{optional_message}\n"

        warning_msg = await context.bot.send_message(
            chat_id=user_id,
            text=notification_header + "\n⚠️ ❌👉This file automatically❗️delete after 1 minute❗️so please forward in another chat👈❌",
            parse_mode='Markdown'
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
            cur.close()
            conn.close()
            return

        if sent_msg and media_type != "text":
            asyncio.create_task(
                delete_messages_after_delay(
                    context,
                    user_id,
                    [sent_msg.message_id, warning_msg.message_id],
                    60
                )
            )

        confirmation = f"✅ **Notification Sent!**\n\n"
        confirmation += f"To: `@{target_username}` ({first_name})\n"
        confirmation += f"Media Type: {media_type.capitalize()}"

        await update.message.reply_text(confirmation, parse_mode='Markdown')

        cur.close()
        conn.close()

    except telegram.error.Forbidden:
        await update.message.reply_text(f"❌ User blocked the bot.")
    except Exception as e:
        logger.error(f"Error in notify_user_with_media: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def broadcast_with_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast media to all users"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    replied_message = update.message.reply_to_message
    if not replied_message:
        await update.message.reply_text("❌ Please reply to a media message to broadcast it.")
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
            await update.message.reply_text("No users found in database.")
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
                header = "📢 **Broadcast from Admin**\n"
                if optional_message:
                    header += f"\n{optional_message}\n"

                await context.bot.send_message(
                    chat_id=user_id,
                    text=header,
                    parse_mode='Markdown'
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
            f"📊 **Broadcast Complete**\n\n"
            f"✅ Sent: {success_count}\n"
            f"❌ Failed: {failed_count}\n"
            f"📝 Total: {len(all_users)}"
        )

        cur.close()
        conn.close()

    except Exception as e:
        logger.error(f"Error in broadcast_with_media: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

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
                # Add other media types if needed

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
        await update.message.reply_text(f"❌ Error: {e}")

async def forward_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward message from channel to user"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    replied_message = update.message.reply_to_message
    if not replied_message:
        await update.message.reply_text("❌ Reply to a message first!")
        return

    if not context.args:
        await update.message.reply_text("Usage: /forwardto @username_or_userid")
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
            await update.message.reply_text(f"❌ User `@{target_username}` not found.", parse_mode='Markdown')
            cur.close()
            conn.close()
            return

        user_id, first_name = user

        await replied_message.forward(chat_id=user_id)

        await update.message.reply_text(f"✅ Forwarded to `@{target_username}` ({first_name})", parse_mode='Markdown')

        cur.close()
        conn.close()

    except Exception as e:
        logger.error(f"Error in forward_to_user: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

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
            await update.message.reply_text(f"❌ No data found for `@{target_username}`", parse_mode='Markdown')
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

        username_str = f"`@{username}`" if username else "N/A"

        info_text = f"""
👤 **User Information**

**Basic Info:**
• Name: {first_name}
• Username: {username_str}
• User ID: `{user_id}`

**Statistics:**
• Total Requests: {total}
• Fulfilled: {fulfilled}
• Pending: {total - fulfilled}
• Last Request: {last_request.strftime('%Y-%m-%d %H:%M') if last_request else 'N/A'}

**Recent Requests:**
"""

        if recent_requests:
            for movie, req_time, notified in recent_requests:
                status = "✅" if notified else "⏳"
                info_text += f"{status} {movie} - {req_time.strftime('%m/%d %H:%M')}\n"
        else:
            info_text += "No recent requests."

        await update.message.reply_text(info_text, parse_mode='Markdown')

        cur.close()
        conn.close()

    except Exception as e:
        logger.error(f"Error in get_user_info: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def list_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all bot users"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    try:
        page = 1
        # FIX: Check the first argument in the list
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
        # FIX: Fetch the first element from the tuple
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

        total_pages = (total_users + per_page - 1) // per_page if total_users > 0 else 1

        users_text = f"👥 **Bot Users** (Page {page}/{total_pages})\n\n"

        if not users:
            users_text += "No users found on this page."
        else:
            for idx, (user_id, username, first_name, req_count, last_seen) in enumerate(users, start=offset+1):
                username_str = f"`@{username}`" if username else "N/A"
                users_text += f"{idx}. {first_name} ({username_str})\n"
                users_text += f"   ID: `{user_id}` | Requests: {req_count}\n"
                users_text += f"   Last seen: {last_seen.strftime('%Y-%m-%d %H:%M')}\n\n"

        users_text += f"\n📊 Total Users: {total_users}"

        await update.message.reply_text(users_text, parse_mode='Markdown')

        cur.close()
        conn.close()

    except Exception as e:
        logger.error(f"Error in list_all_users: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

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

        # FIX: Fetch the first element [0] from the tuple for all COUNT queries
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

        fulfillment_rate = (fulfilled / total_requests * 100) if total_requests > 0 else 0

        stats_text = f"""
📊 **Bot Statistics**

**Database:**
• Movies: {total_movies}
• Users: {total_users}
• Total Requests: {total_requests}
• Fulfilled: {fulfilled}
• Pending: {total_requests - fulfilled}

**Activity:**
• Today's Requests: {today_requests}
• Fulfillment Rate: {fulfillment_rate:.1f}%

**Top Requesters:**
"""

        if top_users:
            for name, username, count in top_users:
                username_str = f"`@{username}`" if username else "N/A"
                stats_text += f"• {name} ({username_str}): {count} requests\n"
        else:
            stats_text += "No user data available."

        await update.message.reply_text(stats_text, parse_mode='Markdown')

        cur.close()
        conn.close()

    except Exception as e:
        logger.error(f"Error in get_bot_stats: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin commands help"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only command.")
        return

    help_text = """
👑 **Admin Commands Guide**

**Media Notifications:**
• `/notifyuserwithmedia @user [msg]` - Reply to media + send to user
• `/qnotify <@user|MovieTitle>` - Quick notify (reply to media)
• `/forwardto @user` - Forward channel message (reply to msg)
• `/broadcastmedia [msg]` - Broadcast media to all (reply to media)

**Text Notifications:**
• `/notifyuser @user <msg>` - Send text message
• `/broadcast <msg>` - Text broadcast to all
• `/schedulenotify <min> @user <msg>` - Schedule notification

**User Management:**
• `/userinfo @username` - Get user stats
• `/listusers [page]` - List all users

**Movie Management:**
• `/addmovie <Title> <URL|FileID>` - Add movie
• `/bulkadd` - Bulk add movies (multi-line)
• `/addalias <Title> <alias>` - Add alias
• `/aliasbulk` - Bulk add aliases (multi-line)
• `/aliases <MovieTitle>` - List aliases
• `/notify <MovieTitle>` - Auto-notify requesters

**Stats & Help:**
• `/stats` - Bot statistics
• `/adminhelp` - This help message
"""

    await update.message.reply_text(help_text, parse_mode='Markdown')

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
            logger.error(f"Failed to send error message to user: {e}")

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

# main.py (Replace the existing run_flask function)
def run_flask():
    port = int(os.environ.get('PORT', 8080))
    # Use a proper secret for sessions (set FLASK_SECRET_KEY in env). Fallback to a random 24-byte key.
    flask_app.secret_key = os.environ.get('FLASK_SECRET_KEY', None) or os.urandom(24)

    # Register admin blueprint if available. Do late import to avoid circular imports.
    try:
        # import admin_views here to avoid circular import during startup
        from admin_views import admin as admin_blueprint
        flask_app.register_blueprint(admin_blueprint)
        logger.info("Admin blueprint registered successfully.")
    except Exception as e:
        logger.error(f"Failed to register admin blueprint: {e}")

    # Run the Flask dev server (this is what you already used). For production use a WSGI server.
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

    # Conversation handler for user interaction flow in private chat
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

    # Register callback handler FIRST to prioritize button clicks over text messages.
    application.add_handler(CallbackQueryHandler(button_callback))

    # NEW: Add the group message handler with a specific group so it doesn't interfere with conv_handler
    # The handler will listen to text messages in groups that are not commands.
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
        group_message_handler
    ))

    # Add the main conversation handler for private chats and direct commands
    application.add_handler(conv_handler)

    # Admin commands
    application.add_handler(CommandHandler("addmovie", add_movie))
    application.add_handler(CommandHandler("bulkadd", bulk_add_movies))
    application.add_handler(CommandHandler("notify", notify_manually))
    application.add_handler(CommandHandler("addalias", add_alias))
    application.add_handler(CommandHandler("aliases", list_aliases))
    application.add_handler(CommandHandler("aliasbulk", bulk_add_aliases))

    # Advanced notification commands
    application.add_handler(CommandHandler("notifyuser", notify_user_by_username))
    application.add_handler(CommandHandler("broadcast", broadcast_message))
    application.add_handler(CommandHandler("schedulenotify", schedule_notification))
    application.add_handler(CommandHandler("notifyuserwithmedia", notify_user_with_media))
    application.add_handler(CommandHandler("qnotify", quick_notify))
    application.add_handler(CommandHandler("forwardto", forward_to_user))
    application.add_handler(CommandHandler("broadcastmedia", broadcast_with_media))

    # User management commands
    application.add_handler(CommandHandler("userinfo", get_user_info))
    application.add_handler(CommandHandler("listusers", list_all_users))

    # Admin utility commands
    application.add_handler(CommandHandler("adminhelp", admin_help))
    application.add_handler(CommandHandler("stats", get_bot_stats))

    application.add_error_handler(error_handler)

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
