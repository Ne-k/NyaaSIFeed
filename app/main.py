import os
import sys
import requests
import xml.etree.ElementTree as ET
import re
import time
import collections
import signal
import subprocess
import base64
import secrets
from flask import Flask, render_template, request, session, redirect, url_for, abort, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from urllib.parse import quote_plus
from functools import wraps

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_urlsafe(32)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///anime.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
scheduler = BackgroundScheduler()
scheduler.start()

JIKAN_API_URL = "https://api.jikan.moe/v4/anime"
NYAA_RSS_URL = "https://nyaa.si/?page=rss&q={}&c=0_0&f=2"

ALL_LANGUAGES = [
    "ENG", "JPN", "SPA", "SPA-LA", "POR", "POR-BR", "FRE", "GER", "ITA", "RUS", "ARA", "KOR", "ZHO", "THA", "VIE",
    "HIN", "TUR", "POL", "NLD", "SWE", "FIN", "NOR", "DAN", "HEB", "CES", "SLK", "HUN", "RON", "UKR", "IND", "MYS",
    "ELL", "HRV", "SRP", "BUL", "CAT", "GLG", "LIT", "LAV", "EST", "SLV", "SRB", "FIL", "ISL", "GRC", "MNE", "ALB",
    "MKD", "BEL", "UZB", "KAZ", "AZE", "ARM", "GEO", "TAM", "TEL", "MAR", "GUJ", "PAN", "URD", "BHO", "ORI", "MLG",
    "NEP", "SIN", "LAO", "KHM", "BUR", "MON", "TGL", "YUE", "CMN", "CANTONESE", "MANDARIN"
]

# --- Rotating API Token ---
ROTATING_API_TOKEN = secrets.token_urlsafe(32)
ROTATE_INTERVAL = 3600  # seconds (1 hour)

def rotate_api_token():
    global ROTATING_API_TOKEN
    ROTATING_API_TOKEN = secrets.token_urlsafe(32)
    app.logger.info(f"API token rotated: {ROTATING_API_TOKEN}")

scheduler.add_job(rotate_api_token, 'interval', seconds=ROTATE_INTERVAL)

def get_current_api_token():
    return ROTATING_API_TOKEN

# --- End Rotating API Token ---

class TrackedAnime(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    rss_url = db.Column(db.String(500), nullable=False)
    save_path = db.Column(db.String(500), nullable=False)
    last_episode = db.Column(db.Integer, default=0)
    quality_preference = db.Column(db.String(20), default='1080p')
    status = db.Column(db.String(50), default='Unknown')
    expected_episodes = db.Column(db.Integer, default=0)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME").strip('"')
ADMIN_PASSWORD_B64 = os.getenv("ADMIN_PASSWORD_HASH")

def is_logged_in():
    return session.get("logged_in", False)

def get_decoded_password():
    if not ADMIN_PASSWORD_B64:
        return None
    return base64.b64decode(ADMIN_PASSWORD_B64).decode("utf-8")

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get("username")
        password = request.form.get("password")
        decoded_password = get_decoded_password()
        if username == ADMIN_USERNAME and password == decoded_password:
            session["logged_in"] = True
            flash("Login successful!", "success")
            return redirect(url_for('index'))
        else:
            flash("Invalid username or password.", "danger")
    return render_template("login.html")

@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for('login'))

@app.before_request
def require_login():
    allowed_routes = {'login', 'static', 'get_api_token', 'set_download_path'}
    if request.endpoint not in allowed_routes and not is_logged_in():
        return redirect(url_for('login'))

# --- API Authentication Decorator ---
def api_auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Missing or invalid Authorization header'}), 401
        token = auth_header.split(' ', 1)[1]
        if not secrets.compare_digest(token, get_current_api_token()):
            return jsonify({'error': 'Invalid API token'}), 401
        return f(*args, **kwargs)
    return decorated

# --- Endpoint for authenticated users to get the current API token ---
@app.route('/api/get-token')
def get_api_token():
    if not is_logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'token': get_current_api_token()})

# --- API Endpoints (examples) ---
@app.route('/api/search', methods=['POST'])
@api_auth_required
def api_search():
    query = request.json.get('query')
    rss_url = request.json.get('rss_url', '').strip()
    if not rss_url:
        formatted_title = quote_plus(query)
        rss_url = f"https://nyaa.si/?page=rss&q={formatted_title}&c=0_0&f=2"
    results = search_jikan(query)
    return jsonify([{
        'mal_id': a['mal_id'],
        'title': a['title'],
        'year': a.get('year'),
        'status': a.get('status', 'Unknown'),
        'episodes': a.get('episodes', 0)
    } for a in results])

@app.route('/api/qb-status')
@api_auth_required
def api_qb_status():
    try:
        session_api, qb_url = qb_login()
        resp = session_api.get(f"{qb_url}/api/v2/torrents/info", params={"filter": "downloading"})
        torrents = resp.json()
        return jsonify(torrents)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/tracking-list')
@api_auth_required
def api_tracking_list():
    tracked_anime = TrackedAnime.query.all()
    return jsonify([
        {
            'id': anime.id,
            'title': anime.title,
            'rss_url': anime.rss_url,
            'save_path': anime.save_path,
            'last_episode': anime.last_episode,
            'quality_preference': anime.quality_preference,
            'status': anime.status,
            'expected_episodes': anime.expected_episodes
        }
        for anime in tracked_anime
    ])

@app.route('/top-completed')
def top_completed():
    rss_url = "https://nyaa.si/?page=rss&c=0_0&f=2"
    page = int(request.args.get('page', 1))
    per_page = 20

    try:
        response = requests.get(rss_url, timeout=10)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        items = []
        for item in root.findall('./channel/item'):
            title = item.findtext('title', '').strip()
            link = item.findtext('link', '').strip()
            downloads = item.findtext('{https://nyaa.si/xmlns/nyaa}downloads', '0')
            seeders = item.findtext('{https://nyaa.si/xmlns/nyaa}seeders', '0')
            leechers = item.findtext('{https://nyaa.si/xmlns/nyaa}leechers', '0')
            size = item.findtext('{https://nyaa.si/xmlns/nyaa}size', 'N/A')
            category = item.findtext('{https://nyaa.si/xmlns/nyaa}category', '')
            pubDate = item.findtext('pubDate', '')
            items.append({
                'title': title,
                'link': link,
                'downloads': int(downloads),
                'seeders': int(seeders),
                'leechers': int(leechers),
                'size': size,
                'category': category,
                'pubDate': pubDate,
            })
        # Sort by downloads descending
        items.sort(key=lambda x: x['downloads'], reverse=True)
        total = len(items)
        pages = (total + per_page - 1) // per_page
        start = (page - 1) * per_page
        end = start + per_page
        page_items = items[start:end]
        return render_template(
            'top_completed.html',
            items=page_items,
            page=page,
            pages=pages
        )
    except Exception as e:
        return f"Failed to fetch or parse RSS feed: {e}", 500

@app.route('/api/untrack/<int:anime_id>', methods=['POST'])
@api_auth_required
def api_untrack(anime_id):
    anime = TrackedAnime.query.get(anime_id)
    if anime:
        db.session.delete(anime)
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'error': 'Not found'}), 404

# --- End API Endpoints ---

# --- Existing Web Endpoints (not API protected) ---

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

def search_jikan(query):
    response = requests.get(JIKAN_API_URL, params={'q': query, 'limit': 5})
    return response.json().get('data', []) if response.status_code == 200 else []

def parse_rss_items_for_template(rss_url):
    items = parse_rss_feed(rss_url)
    grouped = collections.defaultdict(list)
    group_regex = re.compile(r'\[([^\[\]]+)\]')
    video_codecs = set()
    audio_codecs = set()
    languages = set()
    qualities = set()
    video_codec_regex = re.compile(r'(HEVC|AVC|H\.264|H\.265)', re.IGNORECASE)
    audio_codec_regex = re.compile(
        r'(AAC|EAC3|FLAC|AC3|MP3|TRUEHD|DOLBY|ATMOS|DDP|DD|DOLBY DIGITAL|DOLBY TRUEHD|DOLBY ATMOS)',
        re.IGNORECASE
    )
    lang_regex = re.compile(r'\[([A-Z]{2,}(-[A-Z]{2,})?)\]')
    quality_regex = re.compile(r'([0-9]{3,4}p|4K)', re.IGNORECASE)
    group_batch_flags = {}
    for item in items:
        title = item.findtext('title', '').strip()
        match = group_regex.search(title)
        group = match.group(1) if match else "Other"
        video_codec_match = video_codec_regex.search(title)
        audio_codec_match = audio_codec_regex.search(title)
        video_codec = video_codec_match.group(1).upper() if video_codec_match else ""
        audio_codec = audio_codec_match.group(1).upper() if audio_codec_match else ""
        is_batch = '[BATCH]' in title.upper()
        langs = [m.group(1) for m in lang_regex.finditer(title) if m.group(1) not in [group, video_codec, audio_codec, 'BATCH']]
        for lang in langs:
            languages.add(lang)
        if group not in group_batch_flags:
            group_batch_flags[group] = False
        if is_batch:
            group_batch_flags[group] = True
        if video_codec:
            video_codecs.add(video_codec)
        if audio_codec:
            audio_codecs.add(audio_codec)
        quality_match = quality_regex.search(title)
        if quality_match:
            qualities.add(quality_match.group(1).lower())
        grouped[group].append({
            'title': title,
            'link': item.findtext('link', '').strip(),
            'size': item.findtext('{https://nyaa.si/xmlns/nyaa}size', 'N/A'),
            'seeders': item.findtext('{https://nyaa.si/xmlns/nyaa}seeders', 'N/A'),
            'leechers': item.findtext('{https://nyaa.si/xmlns/nyaa}leechers', 'N/A'),
            'video_codec': video_codec,
            'audio_codec': audio_codec,
            'is_batch': is_batch,
            'languages': langs,
        })
    return grouped, sorted(video_codecs), sorted(audio_codecs), sorted(languages), group_batch_flags, sorted(qualities)

def parse_rss_feed(rss_url):
    namespaces = {'nyaa': 'https://nyaa.si/xmlns/nyaa'}
    try:
        response = requests.get(rss_url)
        root = ET.fromstring(response.content)
        return root.findall('.//item')
    except Exception as e:
        app.logger.error(f"RSS Error: {str(e)}")
        return []

def get_episode_number(title):
    match = re.search(r'\b(\d{1,3})\b', title)
    return int(match.group(1)) if match else 0

def get_best_torrent(items, quality_preference):
    quality_order = ['1080p', '720p', '480p']
    if quality_preference in quality_order:
        quality_order.insert(0, quality_preference)
    for quality in quality_order:
        for item in items:
            title = item.findtext('title', '')
            if quality.lower() in title.lower():
                return item
    return None

def qb_login():
    qb_url = os.getenv('QB_URL').rstrip('/')
    username = os.getenv('QB_USERNAME')
    password = os.getenv('QB_PASSWORD')
    session = requests.Session()
    resp = session.post(f"{qb_url}/api/v2/auth/login", data={'username': username, 'password': password})
    if resp.text != 'Ok.':
        raise Exception("Failed to login to qBittorrent Web API")
    return session, qb_url

def qb_add_torrent_url(session, qb_url, torrent_url, save_path, retries=3):
    for attempt in range(retries):
        try:
            resp = session.post(
                f"{qb_url}/api/v2/torrents/add",
                data={'urls': torrent_url, 'savepath': save_path},
                timeout=15  # Add a timeout
            )
            if resp.status_code == 200:
                return
            else:
                app.logger.error(f"Failed to add torrent (attempt {attempt+1}): {torrent_url} ({resp.text})")
        except requests.RequestException as e:
            app.logger.error(f"Exception adding torrent (attempt {attempt+1}): {torrent_url} ({e})")
        time.sleep(2)
    raise Exception(f"Failed to add torrent after {retries} attempts: {torrent_url}")

def rank_torrent(item, preferred_quality, preferred_video_codec, preferred_audio_codec):
    score = 0
    title = item.findtext('title', '').lower()
    video_codec = ''
    audio_codec = ''
    video_codec = item.findtext('video_codec', '').lower() if item.find('video_codec') is not None else ''
    audio_codec = item.findtext('audio_codec', '').lower() if item.find('audio_codec') is not None else ''
    if not video_codec:
        if 'hevc' in title or 'h.265' in title:
            video_codec = 'hevc'
        elif 'avc' in title or 'h.264' in title:
            video_codec = 'avc'
    if not audio_codec:
        if 'eac3' in title:
            audio_codec = 'eac3'
        elif 'aac' in title:
            audio_codec = 'aac'
        elif 'flac' in title:
            audio_codec = 'flac'
        elif 'ac3' in title:
            audio_codec = 'ac3'
        elif 'mp3' in title:
            audio_codec = 'mp3'
    if preferred_quality and preferred_quality.lower() in title:
        pass
    else:
        score += 10
    if preferred_video_codec and preferred_video_codec.lower() in video_codec:
        pass
    else:
        score += 5
    if preferred_audio_codec and preferred_audio_codec.lower() in audio_codec:
        pass
    else:
        score += 2
    return score

def monitor_rss_feeds():
    with app.app_context():
        all_anime = TrackedAnime.query.all()
        for anime in all_anime:
            # Remove if finished and all episodes are out
            if (
                anime.status.lower() == "finished"
                and anime.expected_episodes > 0
                and anime.last_episode >= anime.expected_episodes
            ):
                db.session.delete(anime)
                db.session.commit()
                continue

            if anime.status != "Currently Airing":
                continue
            try:
                items = parse_rss_feed(anime.rss_url)
                episode_items = []
                for item in items:
                    ep_num = get_episode_number(item.findtext('title', ''))
                    if ep_num > anime.last_episode:
                        episode_items.append((ep_num, item))
                episodes = {}
                for ep_num, item in episode_items:
                    episodes.setdefault(ep_num, []).append(item)
                for ep_num in sorted(episodes.keys()):
                    best = sorted(
                        episodes[ep_num],
                        key=lambda item: rank_torrent(
                            item,
                            anime.quality_preference,
                            '',
                            ''
                        )
                    )[0]
                    torrent_url = best.findtext('link')
                    session, qb_url = qb_login()
                    qb_add_torrent_url(session, qb_url, torrent_url, anime.save_path)
                    anime.last_episode = ep_num
                    db.session.commit()
            except Exception as e:
                app.logger.error(f"Monitoring error for {anime.title}: {str(e)}")

check_interval = int(os.getenv('CHECK_INTERVAL', 1800))
scheduler.add_job(monitor_rss_feeds, 'interval', seconds=check_interval)

@app.route('/')
def index():
    return render_template(
        'index.html',
        api_token=get_current_api_token(),
        ADMIN_USERNAME=ADMIN_USERNAME
    )

@app.route('/search', methods=['POST'])
def search():
    # Check for download path first
    if not session.get('download_path'):
        # Store the search query and rss_url in session to resume after setting path
        session['pending_search_query'] = request.form['query']
        session['pending_search_rss_url'] = request.form.get('rss_url', '').strip()
        session['next_after_download_path'] = url_for('resume_search')
        return redirect(url_for('set_download_path'))

    query = request.form['query']
    rss_url = request.form.get('rss_url', '').strip()
    if not rss_url:
        formatted_title = quote_plus(query)
        rss_url = f"https://nyaa.si/?page=rss&q={formatted_title}&c=0_0&f=2"
    session['rss_url'] = rss_url
    results = search_jikan(query)
    session['results'] = [{
        'mal_id': a['mal_id'],
        'title': a['title'],
        'year': a.get('year'),
        'status': a.get('status', 'Unknown'),
        'episodes': a.get('episodes', 0)
    } for a in results]
    return render_template('search_results.html', results=session['results'])

@app.route('/resume-search')
def resume_search():
    # Resume the search after setting download path
    query = session.pop('pending_search_query', None)
    rss_url = session.pop('pending_search_rss_url', '').strip()
    if not query:
        return redirect(url_for('index'))
    if not rss_url:
        formatted_title = quote_plus(query)
        rss_url = f"https://nyaa.si/?page=rss&q={formatted_title}&c=0_0&f=2"
    session['rss_url'] = rss_url
    results = search_jikan(query)
    session['results'] = [{
        'mal_id': a['mal_id'],
        'title': a['title'],
        'year': a.get('year'),
        'status': a.get('status', 'Unknown'),
        'episodes': a.get('episodes', 0)
    } for a in results]
    return render_template('search_results.html', results=session['results'])

@app.route('/set-download-path', methods=['GET', 'POST'])
def set_download_path():
    if request.method == 'POST':
        session['download_path'] = request.form['download_path']
        flash("Download path updated!", "success")
        next_page = session.pop('next_after_download_path', None)
        if next_page:
            return redirect(next_page)
        return redirect(url_for('index'))

    def get_dir_tree(root, depth=2):
        tree = []
        if depth < 0 or not os.path.isdir(root):
            return tree
        try:
            for entry in os.scandir(root):
                if entry.is_dir():
                    subtree = get_dir_tree(entry.path, depth-1)
                    tree.append({
                        'name': entry.name,
                        'path': entry.path,
                        'children': subtree
                    })
        except Exception:
            pass
        return tree

    dir_trees = []
    if os.name == 'nt':
        import string
        from ctypes import windll

        bitmask = windll.kernel32.GetLogicalDrives()
        for letter in string.ascii_uppercase:
            if bitmask & 1:
                drive = f"{letter}:\\"
                # This check ensures only existing drives are added
                if os.path.isdir(drive):
                    dir_trees.append({
                        'name': drive,
                        'path': drive,
                        'children': get_dir_tree(drive, depth=2)
                    })
            bitmask >>= 1
        else:
            roots = ['/']
            for extra in ['/media', '/mnt']:
                if os.path.isdir(extra):
                    roots.append(extra)
            for root in roots:
                dir_trees.append({
                    'name': root,
                    'path': root,
                    'children': get_dir_tree(root, depth=2)
                })

    return render_template(
        'set_download_path.html',
        current=session.get('download_path', ''),
        dir_tree=dir_trees
    )



@app.route('/select/<int:mal_id>')
def select_anime(mal_id):
    if 'results' not in session:
        return redirect(url_for('index'))
    anime = next((a for a in session['results'] if a['mal_id'] == mal_id), None)
    if not anime:
        return redirect(url_for('index'))
    session['confirmed_title'] = anime['title']
    session['current_mal_id'] = mal_id
    rss_url = session.get('rss_url')
    if not rss_url:
        formatted_title = quote_plus(anime['title'])
        rss_url = f"https://nyaa.si/?page=rss&q={formatted_title}&c=0_0&f=2"
    # Use dynamic download path
    download_path = session.get('download_path')
    if not download_path:
        return redirect(url_for('set_download_path'))
    default_save_path = os.path.join(download_path, anime['title'])
    # --- Directory tree for easier folder selection ---
    def get_dir_tree(root, depth=2):
        tree = []
        if depth < 0 or not os.path.isdir(root):
            return tree
        try:
            for entry in os.scandir(root):
                if entry.is_dir():
                    subtree = get_dir_tree(entry.path, depth-1)
                    tree.append({
                        'name': entry.name,
                        'path': entry.path,
                        'children': subtree
                    })
        except Exception:
            pass
        return tree
    dir_tree = get_dir_tree(download_path, depth=2)
    # -------------------------------------------------
    quality = request.args.get('quality') or ''
    video_codec = request.args.get('video_codec') or ''
    audio_codec = request.args.get('audio_codec') or ''
    language = request.args.get('language') or ''
    rss_items, video_codecs, audio_codecs, languages, group_batch_flags, qualities = parse_rss_items_for_template(rss_url)
    return render_template(
        'confirm_download.html',
        title=anime['title'],
        rss_url=rss_url,
        default_save_path=default_save_path,
        rss_items=rss_items,
        quality=quality,
        video_codecs=video_codecs,
        audio_codecs=audio_codecs,
        selected_video_codec=video_codec,
        selected_audio_codec=audio_codec,
        languages=ALL_LANGUAGES,
        selected_language=language,
        group_batch_flags=group_batch_flags,
        qualities=qualities,
        dir_tree=dir_tree  # Pass the directory tree to the template
    )

@app.route('/track', methods=['POST'])
def track():
    try:
        title = request.form.get('title')
        rss_url = request.form.get('rss_url')
        save_path = request.form.get('save_path')
        quality = request.form.get('quality') or '1080p'
        selected_torrents = request.form.getlist('selected_torrents')
        status = None
        expected_episodes = 0
        # Try to get expected episodes from session['results']
        for a in session.get('results', []):
            if a['title'] == title:
                status = a.get('status', 'Unknown')
                expected_episodes = a.get('episodes', 0) or 0
                break
        if not all([title, rss_url, save_path, quality]):
            return redirect(url_for('index'))
        if selected_torrents:
            session_api, qb_url = qb_login()
            for torrent_url in selected_torrents:
                qb_add_torrent_url(session_api, qb_url, torrent_url, save_path)
                time.sleep(2)
        existing = TrackedAnime.query.filter_by(title=title, rss_url=rss_url, save_path=save_path).first()
        if not existing:
            new_anime = TrackedAnime(
                title=title,
                rss_url=rss_url,
                save_path=save_path,
                quality_preference=quality,
                status=status or 'Unknown',
                expected_episodes=expected_episodes
            )
            db.session.add(new_anime)
            db.session.commit()
        session.pop('results', None)
        session.pop('confirmed_title', None)
        session.pop('current_mal_id', None)
        return redirect(url_for('tracking_list'))
    except Exception as e:
        app.logger.error(f"Tracking error: {str(e)}")
        db.session.rollback()
        return redirect(url_for('index'))

@app.route('/untrack/<int:anime_id>', methods=['POST'])
def untrack(anime_id):
    anime = TrackedAnime.query.get(anime_id)
    if anime:
        db.session.delete(anime)
        db.session.commit()
    return redirect(url_for('tracking_list'))

@app.route('/qb-status')
def qb_status():
    try:
        session_api, qb_url = qb_login()
        resp = session_api.get(f"{qb_url}/api/v2/torrents/info", params={"filter": "downloading"})
        torrents = resp.json()
        return render_template('qb_status.html', torrents=torrents)
    except Exception as e:
        return f"Error fetching qBittorrent status: {e}", 500

@app.route('/tracking-list')
def tracking_list():
    tracked_anime = TrackedAnime.query.all()
    return render_template('tracking_list.html', tracked_anime=tracked_anime)

@app.context_processor
def inject_project_name():
    return dict(project_name="RSS Tracker")

@app.route('/kill-process', methods=['POST'])
def kill_process():
    os.kill(os.getpid(), signal.SIGTERM)
    return "Process killed", 200

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=False, host='0.0.0.0', port=5000)

