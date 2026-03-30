#!/usr/bin/env python3
import os
import json
import tempfile
import urllib.request
import urllib.parse
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
import traceback

PORT = int(os.environ.get("PORT", 10000))  # Render ожидает порт 10000

def download_video_with_ytdlp(url):
    """Скачивает видео с YouTube, Vimeo, Rutube, Dzen"""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_template = os.path.join(tmpdir, 'audio.%(ext)s')
            
            cmd = [
                'yt-dlp',
                '-f', 'bestaudio/best',
                '--extract-audio',
                '--audio-format', 'mp3',
                '--audio-quality', '0',
                '-o', output_template,
                url
            ]
            
            print(f"[yt-dlp] Загрузка: {url}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode != 0:
                error_msg = result.stderr[-500:] if result.stderr else "Неизвестная ошибка"
                raise Exception(f"Ошибка загрузки: {error_msg}")
            
            files = os.listdir(tmpdir)
            if not files:
                raise Exception("Файл не найден")
            
            audio_file = os.path.join(tmpdir, files[0])
            with open(audio_file, 'rb') as f:
                audio_data = f.read()
            
            print(f"[yt-dlp] Загружено {len(audio_data)} байт")
            return audio_data
            
    except subprocess.TimeoutExpired:
        raise Exception("Превышено время загрузки")
    except Exception as e:
        raise Exception(f"Ошибка: {str(e)}")

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    
    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {format % args}")
    
    def send_cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Api-Key, X-Language, X-Model, X-Diarize, X-File-Extension, X-Is-Video')
        self.send_header('Access-Control-Max-Age', '86400')
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors()
        self.end_headers()
    
    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            try:
                with open('index.html', 'rb') as f:
                    html = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(html)))
                self.send_cors()
                self.end_headers()
                self.wfile.write(html)
            except Exception as e:
                self.send_error(500, f"Error: {e}")
        else:
            self.send_error(404)
    
    def do_POST(self):
        try:
            if self.path == '/transcribe':
                self.handle_transcribe()
            elif self.path == '/download-url':
                self.handle_download()
            else:
                self.send_error(404)
        except BrokenPipeError:
            # Клиент закрыл соединение - игнорируем
            pass
        except Exception as e:
            print(f"[ERROR] {traceback.format_exc()}")
            try:
                self.send_json_error(str(e))
            except:
                pass
    
    def handle_download(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_json_error("Нет данных")
                return
            
            data = json.loads(self.rfile.read(content_length))
            url = data.get('url')
            
            if not url:
                self.send_json_error("URL не указан")
                return
            
            print(f"[download] Загрузка: {url}")
            audio_data = download_video_with_ytdlp(url)
            
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Content-Length', str(len(audio_data)))
            self.send_cors()
            self.end_headers()
            self.wfile.write(audio_data)
            
        except Exception as e:
            print(f"[download] Ошибка: {e}")
            self.send_json_error(str(e))
    
    def handle_transcribe(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_json_error("Нет данных")
                return
            
            # Получаем boundary для multipart
            content_type = self.headers.get('Content-Type', '')
            if 'boundary=' not in content_type:
                self.send_json_error("Неверный формат запроса")
                return
            
            boundary = content_type.split('boundary=')[1].encode()
            data = self.rfile.read(content_length)
            
            # Парсим multipart данные
            parts = data.split(b'--' + boundary)
            
            file_data = None
            api_key = ''
            language = 'ru'
            provider = 'deepgram'
            model = 'nova-3'
            diarize = True
            url = ''
            
            for part in parts:
                if b'Content-Disposition' not in part:
                    continue
                    
                headers, body = part.split(b'\r\n\r\n', 1)
                body = body.rstrip(b'\r\n--')
                
                if b'name="file"' in headers:
                    file_data = body
                elif b'name="url"' in headers:
                    url = body.decode()
                elif b'name="api_key"' in headers:
                    api_key = body.decode()
                elif b'name="language"' in headers:
                    language = body.decode()
                elif b'name="provider"' in headers:
                    provider = body.decode()
                elif b'name="model"' in headers:
                    model = body.decode()
                elif b'name="diarize"' in headers:
                    diarize = body.decode().lower() == 'true'
            
            if not api_key:
                self.send_json_error("API ключ не передан")
                return
            
            # Получаем аудио данные
            if url and not file_data:
                try:
                    audio_data = download_video_with_ytdlp(url)
                except Exception as e:
                    self.send_json_error(f"Не удалось скачать видео: {str(e)}")
                    return
            elif file_data:
                audio_data = file_data
            else:
                self.send_json_error("Нет файла или ссылки")
                return
            
            # Отправляем в Deepgram
            result = self.transcribe_deepgram(api_key, audio_data, language, model, diarize)
            self.send_json(result)
            
        except Exception as e:
            print(f"[transcribe] Ошибка: {traceback.format_exc()}")
            self.send_json_error(str(e))
    
    def transcribe_deepgram(self, api_key, audio_data, language, model, diarize):
        params = {
            'model': model,
            'language': language if language != 'auto' else 'ru',
            'smart_format': 'true',
            'punctuate': 'true',
            'utterances': 'true' if diarize else 'false',
            'diarize': 'true' if diarize else 'false'
        }
        
        url = f"https://api.deepgram.com/v1/listen?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            data=audio_data,
            headers={
                'Authorization': f'Token {api_key}',
                'Content-Type': 'audio/mpeg'
            },
            method='POST'
        )
        
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            raise Exception(f"Deepgram ошибка {e.code}: {error_body}")
        
        results = data.get('results', {})
        channels = results.get('channels', [])
        alternative = channels[0].get('alternatives', [{}])[0] if channels else {}
        transcript = alternative.get('transcript', '')
        confidence = alternative.get('confidence', 0)
        
        utterances = []
        for u in results.get('utterances', []):
            utterances.append({
                'speaker': u.get('speaker', 0),
                'text': u.get('transcript', ''),
                'start': u.get('start', 0),
                'end': u.get('end', 0),
                'confidence': u.get('confidence', 0)
            })
        
        if not utterances and transcript:
            utterances.append({
                'speaker': 0,
                'text': transcript,
                'start': 0,
                'end': data.get('metadata', {}).get('duration', 0),
                'confidence': confidence
            })
        
        return {
            'success': True,
            'provider': 'deepgram',
            'transcript': transcript,
            'utterances': utterances,
            'metadata': {
                'duration': data.get('metadata', {}).get('duration', 0),
                'confidence': confidence,
                'language': language,
                'model': model
            }
        }
    
    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_cors()
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            # Клиент закрыл соединение - игнорируем
            pass
    
    def send_json_error(self, message, status=500):
        self.send_json({'success': False, 'error': message})

if __name__ == '__main__':
    print(f"🚀 VoxCraft запущен на порту {PORT}")
    print(f"✅ Поддерживаются: YouTube, Vimeo, Rutube, Dzen")
    print(f"📦 Для работы требуется установить yt-dlp: pip install yt-dlp")
    
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Остановка сервера...")
