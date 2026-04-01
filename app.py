import os
import imageio_ffmpeg
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
# Permitir solicitudes desde cualquier origen (CORS) para que el frontend local pueda conectarse
CORS(app) 

# Conseguimos la ruta al binario ffmpeg privado que acabamos de instalar
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

@app.route('/api/info', methods=['POST'])
def get_video_info():
    data = request.json
    url = data.get('url')
    
    if not url:
        return jsonify({'error': 'No se proporcionó URL'}), 400

    ydl_opts = {
        'quiet': True,
        'noplaylist': True
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Buscar resoluciones disponibles
            calidades_set = set()
            for f in info.get('formats', []):
                h = f.get('height')
                # Ignorar audios (none) y minúsculas formatios (< 144)
                if h and isinstance(h, int) and h >= 144:
                    calidades_set.add(h)
            
            calidades = sorted(list(calidades_set), reverse=True)

            return jsonify({
                'title': info.get('title'),
                'thumbnail': info.get('thumbnail'),
                'duration': info.get('duration_string'),
                'qualities': calidades
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download', methods=['POST'])
def download_video():
    data = request.json
    url = data.get('url')
    fmt = data.get('format', 'mp4') # 'mp4' o 'mp3'
    quality = data.get('quality') # e.g '1080' or ''
    
    if not url:
        return jsonify({'error': 'No se proporcionó URL'}), 400

    # Creamos la carpeta de Descargas donde guardaremos los archivos en el PC del usuario
    download_folder = os.path.join(os.getcwd(), 'Descargas')
    os.makedirs(download_folder, exist_ok=True)
    
    # Opciones base
    ydl_opts = {
        'outtmpl': os.path.join(download_folder, '%(title)s.%(ext)s'),
        'ffmpeg_location': FFMPEG_EXE,
        'quiet': True,
        'noplaylist': True
    }

    if fmt == 'mp3':
        # Con configuraciones de post-procesado le decimos que lo convierta a MP3 usando ffmpeg
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    else:
        # Tratamos de conseguir el MP4 combinando Audio y Video de alta calidad
        if quality and str(quality).isdigit():
            q_str = str(quality)
            ydl_opts['format'] = f'bestvideo[height<={q_str}][ext=mp4]+bestaudio[ext=m4a]/best[height<={q_str}]/best'
        else:
            ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        
        ydl_opts['merge_output_format'] = 'mp4'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Ahora SÍ descargamos físicamente y lo guardamos
            info = ydl.extract_info(url, download=True)
            return jsonify({
                'success': True,
                'path': download_folder,
                'title': info.get('title')
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/playlist/info', methods=['POST'])
def get_playlist_info():
    data = request.json
    url = data.get('url')
    if not url: return jsonify({'error': 'No se proporcionó URL'}), 400

    # Para obtener la información de una playlist rápidamente usamos extract_flat
    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'noplaylist': False
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [])
            return jsonify({
                'title': info.get('title'),
                'count': len(entries)
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/playlist/download', methods=['POST'])
def download_playlist():
    data = request.json
    url = data.get('url')
    fmt = data.get('format', 'mp4') # 'mp4' o 'mp3'
    
    if not url: return jsonify({'error': 'No se proporcionó URL'}), 400

    # Creamos una subcarpeta usando el nombre de la playlist para ordenar los archivos
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
        # Resoluciones más seguras para descargas masivas
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

if __name__ == '__main__':
    # El servidor correrá en el puerto 5000 (http://localhost:5000)
    app.run(debug=True, port=5000)
