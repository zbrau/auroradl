import os
import time
import threading
import json
import hashlib
import imageio_ffmpeg
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp
from collections import OrderedDict
from datetime import datetime, timedelta

app = Flask(__name__, static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'frontend'), static_url_path='')
CORS(app, expose_headers=["Content-Disposition"])
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Rate limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["100 per hour", "20 per minute"],
    storage_uri="memory://"
)

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

# ================== CACHE DE METADATOS ==================
class MetadataCache:
    def __init__(self, max_size=100, ttl_seconds=3600):
        self.cache = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl_seconds

    def _make_key(self, url):
        return hashlib.md5(url.encode()).hexdigest()

    def get(self, url):
        key = self._make_key(url)
        if key in self.cache:
            entry = self.cache[key]
            if time.time() - entry['time'] < self.ttl:
                self.cache.move_to_end(key)
                return entry['data']
            else:
                del self.cache[key]
        return None

    def set(self, url, data):
        key = self._make_key(url)
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = {'data': data, 'time': time.time()}
        if len(self.cache) > self.max_size:
            self.cache.popitem(last=False)

metadata_cache = MetadataCache()

# ================== COLA DE DESCARGAS ==================
class DownloadQueue:
    def __init__(self):
        self.queue = []
        self.processing = False
        self.lock = threading.Lock()
        self.current_job = None

    def add(self, job):
        with self.lock:
            job['id'] = len(self.queue) + 1
            job['status'] = 'queued'
            job['added_at'] = datetime.now().isoformat()
            self.queue.append(job)
            return job['id']

    def get_all(self):
        with self.lock:
            return list(self.queue)

    def get(self, job_id):
        with self.lock:
            for job in self.queue:
                if job['id'] == job_id:
                    return job
        return None

    def remove(self, job_id):
        with self.lock:
            self.queue = [j for j in self.queue if j['id'] != job_id]

    def clear(self):
        with self.lock:
            self.queue = [j for j in self.queue if j['status'] == 'processing']
            self.processing = False
            self.current_job = None

download_queue = DownloadQueue()

def process_queue():
    with download_queue.lock:
        if download_queue.processing:
            return
        download_queue.processing = True

    while True:
        job = None
        with download_queue.lock:
            for j in download_queue.queue:
                if j['status'] == 'queued':
                    job = j
                    j['status'] = 'processing'
                    download_queue.current_job = j
                    break

        if not job:
            break

        try:
            download_folder = os.path.join(os.getcwd(), 'Descargas')
            os.makedirs(download_folder, exist_ok=True)

            ydl_opts = {
                'outtmpl': os.path.join(download_folder, '%(title)s.%(ext)s'),
                'ffmpeg_location': FFMPEG_EXE,
                'quiet': True,
                'noplaylist': True,
                'progress_hooks': [lambda d: queue_progress_hook(d, job['id'])],
            }

            fmt = job.get('format', 'mp4')
            quality = job.get('quality', '')

            if fmt == 'mp3':
                ydl_opts['format'] = 'bestaudio/best'
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
            else:
                if quality:
                    ydl_opts['format'] = f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/best[height<={quality}]/best'
                else:
                    ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
                ydl_opts['merge_output_format'] = 'mp4'

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(job['url'], download=True)
                downloaded_file = ydl.prepare_filename(info)
                if fmt == 'mp3':
                    downloaded_file = os.path.splitext(downloaded_file)[0] + '.mp3'

            with download_queue.lock:
                job['status'] = 'completed'
                job['title'] = info.get('title', 'Unknown')
                job['completed_at'] = datetime.now().isoformat()

            socketio.emit('queue_job_update', {
                'job_id': job['id'],
                'status': 'completed',
                'progress': 100,
                'title': job.get('title', '')
            })

        except Exception as e:
            with download_queue.lock:
                job['status'] = 'failed'
                job['error'] = str(e)

            socketio.emit('queue_job_update', {
                'job_id': job['id'],
                'status': 'failed',
                'error': str(e)
            })

    with download_queue.lock:
        download_queue.processing = False
        download_queue.current_job = None

def queue_progress_hook(d, job_id):
    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
        downloaded = d.get('downloaded_bytes', 0)
        if total > 0:
            progress = (downloaded / total) * 100
            socketio.emit('queue_job_progress', {
                'job_id': job_id,
                'progress': round(progress, 1),
                'speed': d.get('speed', 0),
                'eta': d.get('eta', 0)
            })

# ================== PROGRESO REAL DE DESCARGA ==================
download_progress_store = {}

def progress_hook(d, session_id):
    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
        downloaded = d.get('downloaded_bytes', 0)
        speed = d.get('speed', 0)
        eta = d.get('eta', 0)

        if total > 0:
            progress = (downloaded / total) * 100
        else:
            progress = 0

        download_progress_store[session_id] = {
            'status': 'downloading',
            'progress': round(progress, 1),
            'speed': speed,
            'eta': eta,
            'downloaded': downloaded,
            'total': total
        }

        socketio.emit('download_progress', {
            'session_id': session_id,
            'status': 'downloading',
            'progress': round(progress, 1),
            'speed': format_speed(speed),
            'eta': format_eta(eta)
        })

    elif d['status'] == 'finished':
        download_progress_store[session_id] = {
            'status': 'finished',
            'progress': 100
        }
        socketio.emit('download_progress', {
            'session_id': session_id,
            'status': 'finished',
            'progress': 100
        })

def format_speed(speed):
    if not speed:
        return "0 B/s"
    if speed > 1048576:
        return f"{speed / 1048576:.1f} MB/s"
    elif speed > 1024:
        return f"{speed / 1024:.1f} KB/s"
    return f"{speed:.0f} B/s"

def format_eta(seconds):
    if not seconds:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

# ================== WEBSOCKET EVENTS ==================
@socketio.on('connect')
def handle_connect():
    emit('connected', {'message': 'Conectado al servidor'})

@socketio.on('subscribe_progress')
def handle_subscribe(data):
    session_id = data.get('session_id')
    if session_id and session_id in download_progress_store:
        emit('download_progress', {
            'session_id': session_id,
            **download_progress_store[session_id]
        })

# ================== ENDPOINTS ==================
@app.route('/api/info', methods=['POST'])
@limiter.limit("30 per minute")
def get_video_info():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({'error': 'No se proporcionó URL'}), 400

    cached = metadata_cache.get(url)
    if cached:
        return jsonify(cached)

    ydl_opts = {'quiet': True, 'noplaylist': True}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            calidades_set = set()
            for f in info.get('formats', []):
                h = f.get('height')
                if h and isinstance(h, int) and h >= 144:
                    calidades_set.add(h)

            calidades = sorted(list(calidades_set), reverse=True)

            # Extraer subtítulos disponibles
            subtitles = {}
            if info.get('subtitles'):
                for lang in info['subtitles']:
                    subtitles[lang] = len(info['subtitles'][lang])
            if info.get('automatic_captions'):
                for lang in info['automatic_captions']:
                    if lang not in subtitles:
                        subtitles[lang] = len(info['automatic_captions'][lang])

            # Detectar plataforma
            extractor = info.get('extractor', 'youtube')
            platform_name = get_platform_name(extractor)

            result = {
                'title': info.get('title'),
                'thumbnail': info.get('thumbnail'),
                'duration': info.get('duration_string'),
                'qualities': calidades,
                'subtitles': subtitles,
                'platform': platform_name,
                'uploader': info.get('uploader'),
                'view_count': info.get('view_count'),
                'upload_date': info.get('upload_date')
            }

            metadata_cache.set(url, result)
            return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download', methods=['POST'])
@limiter.limit("10 per minute")
def download_video():
    data = request.json
    url = data.get('url')
    fmt = data.get('format', 'mp4')
    quality = data.get('quality')
    start_time = data.get('start_time')
    end_time = data.get('end_time')
    subtitle_lang = data.get('subtitle_lang')

    if not url:
        return jsonify({'error': 'No se proporcionó URL'}), 400

    session_id = hashlib.md5(f"{url}{time.time()}".encode()).hexdigest()

    download_folder = os.path.join(os.getcwd(), 'Descargas')
    os.makedirs(download_folder, exist_ok=True)

    ydl_opts = {
        'outtmpl': os.path.join(download_folder, '%(title)s.%(ext)s'),
        'ffmpeg_location': FFMPEG_EXE,
        'quiet': True,
        'noplaylist': True,
        'progress_hooks': [lambda d: progress_hook(d, session_id)],
    }

    if fmt == 'mp3':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    else:
        if quality and str(quality).isdigit():
            q_str = str(quality)
            ydl_opts['format'] = f'bestvideo[height<={q_str}][ext=mp4]+bestaudio[ext=m4a]/best[height<={q_str}]/best'
        else:
            ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        ydl_opts['merge_output_format'] = 'mp4'

    # Recortar segmento
    if start_time or end_time:
        ydl_opts['postprocessors'] = ydl_opts.get('postprocessors', [])
        ydl_opts['postprocessors'].append({
            'key': 'FFmpegVideoSplitter',
            'starttime': start_time or '0',
            'endtime': end_time or '',
        })

    # Subtítulos
    if subtitle_lang:
        ydl_opts['writesubtitles'] = True
        ydl_opts['writeautomaticsub'] = True
        ydl_opts['subtitleslangs'] = [subtitle_lang]
        ydl_opts['embedsubtitles'] = True

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            downloaded_file = ydl.prepare_filename(info)
            if 'requested_downloads' in info and len(info['requested_downloads']) > 0:
                downloaded_file = info['requested_downloads'][0]['filepath']
            else:
                if fmt == 'mp3':
                    downloaded_file = os.path.splitext(downloaded_file)[0] + '.mp3'
                elif os.path.exists(os.path.splitext(downloaded_file)[0] + '.mp4'):
                    downloaded_file = os.path.splitext(downloaded_file)[0] + '.mp4'

            download_progress_store[session_id] = {
                'status': 'completed',
                'progress': 100
            }

            return send_file(downloaded_file, as_attachment=True)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/subtitles/<path:url>', methods=['GET'])
@limiter.limit("30 per minute")
def get_subtitles(url):
    import urllib.parse
    url = urllib.parse.unquote(url)

    ydl_opts = {'quiet': True, 'noplaylist': True}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            subtitles = {}
            if info.get('subtitles'):
                for lang, subs in info['subtitles'].items():
                    subtitles[lang] = {
                        'count': len(subs),
                        'type': 'manual',
                        'formats': [s.get('ext', 'unknown') for s in subs]
                    }
            if info.get('automatic_captions'):
                for lang, subs in info['automatic_captions'].items():
                    if lang not in subtitles:
                        subtitles[lang] = {
                            'count': len(subs),
                            'type': 'auto',
                            'formats': [s.get('ext', 'unknown') for s in subs]
                        }

            return jsonify({'subtitles': subtitles})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trim', methods=['POST'])
@limiter.limit("10 per minute")
def trim_video():
    data = request.json
    url = data.get('url')
    start_time = data.get('start_time', '0')
    end_time = data.get('end_time')
    fmt = data.get('format', 'mp4')

    if not url:
        return jsonify({'error': 'No se proporcionó URL'}), 400
    if not start_time and not end_time:
        return jsonify({'error': 'Se requiere al menos un tiempo de inicio o fin'}), 400

    session_id = hashlib.md5(f"{url}{start_time}{end_time}{time.time()}".encode()).hexdigest()

    download_folder = os.path.join(os.getcwd(), 'Descargas')
    os.makedirs(download_folder, exist_ok=True)

    ydl_opts = {
        'outtmpl': os.path.join(download_folder, '%(title)s.%(ext)s'),
        'ffmpeg_location': FFMPEG_EXE,
        'quiet': True,
        'noplaylist': True,
        'progress_hooks': [lambda d: progress_hook(d, session_id)],
    }

    if fmt == 'mp3':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    else:
        ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        ydl_opts['merge_output_format'] = 'mp4'

    ydl_opts['postprocessors'] = ydl_opts.get('postprocessors', [])
    ydl_opts['postprocessors'].append({
        'key': 'FFmpegVideoSplitter',
        'starttime': str(start_time),
        'endtime': str(end_time) if end_time else '',
    })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_file = ydl.prepare_filename(info)

            if fmt == 'mp3':
                downloaded_file = os.path.splitext(downloaded_file)[0] + '.mp3'
            elif os.path.exists(os.path.splitext(downloaded_file)[0] + '.mp4'):
                downloaded_file = os.path.splitext(downloaded_file)[0] + '.mp4'

            return send_file(downloaded_file, as_attachment=True)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/playlist/info', methods=['POST'])
@limiter.limit("20 per minute")
def get_playlist_info():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({'error': 'No se proporcionó URL'}), 400

    cached = metadata_cache.get(url)
    if cached:
        return jsonify(cached)

    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'noplaylist': False
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [])

            # Extraer detalles de cada video
            videos = []
            for entry in entries:
                videos.append({
                    'title': entry.get('title', 'Unknown'),
                    'url': entry.get('url', ''),
                    'id': entry.get('id', ''),
                    'thumbnail': entry.get('thumbnail', ''),
                    'duration': entry.get('duration_string', ''),
                })

            result = {
                'title': info.get('title'),
                'count': len(entries),
                'videos': videos,
                'uploader': info.get('uploader')
            }

            metadata_cache.set(url, result)
            return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/playlist/download', methods=['POST'])
@limiter.limit("5 per minute")
def download_playlist():
    data = request.json
    url = data.get('url')
    fmt = data.get('format', 'mp4')

    if not url:
        return jsonify({'error': 'No se proporcionó URL'}), 400

    download_folder = os.path.join(os.getcwd(), 'Descargas', '%(playlist_title)s')

    ydl_opts = {
        'outtmpl': os.path.join(download_folder, '%(playlist_index)03d - %(title)s.%(ext)s'),
        'ffmpeg_location': FFMPEG_EXE,
        'quiet': True,
        'noplaylist': False
    }

    if fmt == 'mp3':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    else:
        ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        ydl_opts['merge_output_format'] = 'mp4'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return jsonify({
                'success': True,
                'path': "Descargas / " + str(info.get('title', 'Playlist')),
                'title': info.get('title')
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ================== COLA DE DESCARGAS ENDPOINTS ==================
@app.route('/api/queue/add', methods=['POST'])
@limiter.limit("20 per minute")
def add_to_queue():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({'error': 'No se proporcionó URL'}), 400

    job = {
        'url': url,
        'format': data.get('format', 'mp4'),
        'quality': data.get('quality', ''),
        'title': data.get('title', ''),
        'thumbnail': data.get('thumbnail', ''),
    }

    job_id = download_queue.add(job)
    threading.Thread(target=process_queue, daemon=True).start()

    return jsonify({'success': True, 'job_id': job_id, 'position': len(download_queue.get_all())})

@app.route('/api/queue', methods=['GET'])
def get_queue():
    return jsonify({'queue': download_queue.get_all()})

@app.route('/api/queue/<int:job_id>', methods=['DELETE'])
def remove_from_queue(job_id):
    download_queue.remove(job_id)
    return jsonify({'success': True})

@app.route('/api/queue/clear', methods=['POST'])
def clear_queue():
    download_queue.clear()
    return jsonify({'success': True})

# ================== ESTADÍSTICAS ==================
stats_file = os.path.join(os.getcwd(), 'stats.json')

def load_stats():
    if os.path.exists(stats_file):
        with open(stats_file, 'r') as f:
            return json.load(f)
    return {
        'total_downloads': 0,
        'total_mp3': 0,
        'total_mp4': 0,
        'total_playlists': 0,
        'total_trimmed': 0,
        'total_subtitled': 0,
        'platforms': {},
        'daily': {},
        'started_at': datetime.now().isoformat()
    }

def save_stats(stats):
    with open(stats_file, 'w') as f:
        json.dump(stats, f, indent=2)

def record_download(fmt, platform, is_trimmed=False, is_subtitled=False, is_playlist=False):
    stats = load_stats()
    stats['total_downloads'] += 1

    if is_playlist:
        stats['total_playlists'] += 1
    elif fmt == 'mp3':
        stats['total_mp3'] += 1
    else:
        stats['total_mp4'] += 1

    if is_trimmed:
        stats['total_trimmed'] += 1
    if is_subtitled:
        stats['total_subtitled'] += 1

    if platform not in stats['platforms']:
        stats['platforms'][platform] = 0
    stats['platforms'][platform] += 1

    today = datetime.now().strftime('%Y-%m-%d')
    if today not in stats['daily']:
        stats['daily'][today] = 0
    stats['daily'][today] += 1

    save_stats(stats)

@app.route('/api/stats', methods=['GET'])
def get_stats():
    stats = load_stats()
    stats['queue_size'] = len([j for j in download_queue.get_all() if j['status'] in ['queued', 'processing']])
    return jsonify(stats)

# ================== UTILIDADES ==================
def get_platform_name(extractor):
    platforms = {
        'youtube': 'YouTube',
        'tiktok': 'TikTok',
        'instagram': 'Instagram',
        'twitter': 'X (Twitter)',
        'twitch': 'Twitch',
        'soundcloud': 'SoundCloud',
        'facebook': 'Facebook',
        'dailymotion': 'Dailymotion',
        'vimeo': 'Vimeo',
        'reddit': 'Reddit',
    }
    for key, name in platforms.items():
        if key in extractor.lower():
            return name
    return extractor.capitalize()

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/manifest.json')
def manifest():
    return send_from_directory(app.static_folder, 'manifest.json')

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', debug=True, port=5000)
