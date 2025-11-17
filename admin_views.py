from flask import Blueprint, render_template, request, redirect, url_for, session, flash, current_app
import os
import io
import csv
from werkzeug.utils import secure_filename
import logging
import db_utils

logger = logging.getLogger(__name__)

admin = Blueprint('admin', __name__, template_folder='templates')

ADMIN_WEB_SECRET = os.environ.get('ADMIN_WEB_SECRET', 'change_this_to_secure_value')
ALLOWED_EXTENSIONS = {'csv'}

def login_required(f):
    def wrapper(*args, **kwargs):
        if session.get('logged_in') != True:
            flash('Please login to access admin.', 'error')
            return redirect(url_for('admin.login'))
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

@admin.route('/admin/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        secret = request.form.get('admin_secret', '')
        if secret == ADMIN_WEB_SECRET:
            session['logged_in'] = True
            flash('Login successful.', 'success')
            return redirect(url_for('admin.add_movie_form'))
        else:
            flash('Incorrect secret code.', 'error')
    return render_template('admin_login.html')

@admin.route('/admin/logout')
def logout():
    session.pop('logged_in', None)
    flash('Logged out.', 'success')
    return redirect(url_for('admin.login'))

@admin.route('/admin', methods=['GET'])
@login_required
def add_movie_form():
    return render_template('admin_add_movie.html')

@admin.route('/admin/add_movie', methods=['POST'])
@login_required
def add_movie_post():
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    aliases = request.form.get('aliases', '').strip()

    qualities = {
        'Low Quality': request.form.get('q_360', '').strip(),
    'SD Quality': request.form.get('q_480', '').strip(),
    'Standard Quality': request.form.get('q_720', '').strip(),
    'HD Quality': request.form.get('q_1080', '').strip(),
    '4K': request.form.get('q_2160', '').strip()
    }

    if not title:
        flash('Title is required.', 'error')
        return redirect(url_for('admin.add_movie_form'))

    # Ensure at least one non-empty quality (url or file id) before inserting/updating
    if not any(qualities.values()):
        flash('At least one quality link/file id is required.', 'error')
        return redirect(url_for('admin.add_movie_form'))

    conn = db_utils.get_db_connection()
    if not conn:
        flash('Database connection failed.', 'error')
        return redirect(url_for('admin.add_movie_form'))

    movie_id = db_utils.upsert_movie_and_files(conn, title, description, qualities, aliases)
    if movie_id:
        flash(f'Movie \"{title}\" added/updated (id={movie_id}).', 'success')
    else:
        flash(f'Failed to add/update movie \"{title}\". See logs.', 'error')

    return redirect(url_for('admin.add_movie_form'))

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@admin.route('/admin/bulk_upload', methods=['GET', 'POST'])
@login_required
def bulk_upload():
    if request.method == 'POST':
        if 'csvfile' not in request.files:
            flash('No file part', 'error')
            return redirect(url_for('admin.bulk_upload'))

        file = request.files['csvfile']
        if file.filename == '':
            flash('No selected file', 'error')
            return redirect(url_for('admin.bulk_upload'))

        if file and allowed_file(file.filename):
            try:
                stream = io.StringIO(file.stream.read().decode('utf-8'))
                reader = csv.DictReader(stream)
                conn = db_utils.get_db_connection()
                if not conn:
                    flash('DB connection failed', 'error')
                    return redirect(url_for('admin.bulk_upload'))

                success = 0
                failed = 0
                for row in reader:
                    title = (row.get('Title') or row.get('title') or '').strip()
                    if not title:
                        failed += 1
                        continue
                    description = (row.get('Description') or row.get('description') or '').strip()
                    qualities = {
    'Low Quality': (row.get('URL_360') or row.get('url_360') or row.get('360p') or '').strip(),
    'SD Quality': (row.get('URL_480') or row.get('url_480') or row.get('480p') or '').strip(),
    'Standard Quality': (row.get('URL_720') or row.get('url_720') or row.get('720p') or '').strip(),
    'HD Quality': (row.get('URL_1080') or row.get('url_1080') or row.get('1080p') or '').strip(),
    '4K': (row.get('URL_2160') or row.get('url_2160') or row.get('2160p') or '').strip()
}
                    aliases = (row.get('Aliases') or row.get('aliases') or '').strip()

                    # skip rows with no quality/file info
                    if not any(qualities.values()):
                        failed += 1
                        continue

                    mid = db_utils.upsert_movie_and_files(conn, title, description, qualities, aliases)
                    if mid:
                        success += 1
                    else:
                        failed += 1

                flash(f'Bulk upload done. Success: {success}, Failed: {failed}', 'success')
                return redirect(url_for('admin.add_movie_form'))
            except Exception as e:
                logger.exception('Bulk upload error')
                flash(f'Bulk upload failed: {e}', 'error')
                return redirect(url_for('admin.bulk_upload'))
        else:
            flash('Only CSV files allowed.', 'error')
            return redirect(url_for('admin.bulk_upload'))

    # GET
    return render_template('admin_add_movie.html')

# --- New: Manage movies and edit endpoints ---

@admin.route('/admin/manage', methods=['GET'])
@login_required
def manage_movies():
    conn = db_utils.get_db_connection()
    if not conn:
        flash('DB connection failed', 'error')
        return redirect(url_for('admin.add_movie_form'))
    # db_utils.get_all_movies should return list of dicts: id, title, url, file_id, description, aliases
    movies = []
    try:
        movies = db_utils.get_all_movies(conn)
    except Exception as e:
        logger.exception('Failed to fetch movies for management')
        flash('Failed to fetch movies. See logs.', 'error')
    return render_template('admin_manage_movies.html', movies=movies)

@admin.route('/admin/edit/<int:movie_id>', methods=['GET', 'POST'])
@login_required
def edit_movie(movie_id):
    conn = db_utils.get_db_connection()
    if not conn:
        flash('DB connection failed', 'error')
        return redirect(url_for('admin.manage_movies'))

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        aliases = request.form.get('aliases', '').strip()
        qualities = {
            'Low Quality': request.form.get('q_360', '').strip(),
    'SD Quality': request.form.get('q_480', '').strip(),
    'Standard Quality': request.form.get('q_720', '').strip(),
    'HD Quality': request.form.get('q_1080', '').strip(),
    '4K': request.form.get('q_2160', '').strip()
}

        if not title:
            flash('Title is required.', 'error')
            return redirect(url_for('admin.edit_movie', movie_id=movie_id))

        if not any(qualities.values()):
            flash('At least one quality link/file id is required.', 'error')
            return redirect(url_for('admin.edit_movie', movie_id=movie_id))

        mid = db_utils.upsert_movie_and_files(conn, title, description, qualities, aliases, movie_id=movie_id)
        if mid:
            flash('Movie updated.', 'success')
            return redirect(url_for('admin.manage_movies'))
        else:
            flash('Failed to update movie. See logs.', 'error')
            return redirect(url_for('admin.edit_movie', movie_id=movie_id))

    # GET: fetch movie
    movie = None
    try:
        movie = db_utils.get_movie_by_id(conn, movie_id)
    except Exception as e:
        logger.exception('Failed to fetch movie for edit')
        flash('Failed to fetch movie. See logs.', 'error')
        return redirect(url_for('admin.manage_movies'))

    if not movie:
        flash('Movie not found.', 'error')
        return redirect(url_for('admin.manage_movies'))

    return render_template('admin_edit_movie.html', movie=movie)
