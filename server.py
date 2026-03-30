#!/usr/bin/env python3
import os
import json
import tempfile
import urllib.request
import urllib.parse
import subprocess
import re
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8800))

def download_video_with_ytdlp(url):
    """Скачивает видео с YouTube, Vimeo, Rutube, Dzen и других платформ"""
    try:
        # Создаем временную директорию
        with tempfile.TemporaryDirectory() as tmpdir:
            output_template = os.path.join(tmpdir, '%(title)s.%(ext)s')
            
            # Команда для yt-dlp
            cmd = [
                'yt-dlp',
                '-f', 'bestaudio/best',  # Лучшее качество аудио
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
            
            # Находим скачанный файл
            files = os.listdir(tmpdir)
            if not files:
                raise Exception("Файл не найден после загрузки")
            
            audio_file = os.path.join(tmpdir, files[0])
            with open(audio_file, 'rb') as f:
                audio_data = f.read()
            
            print(f"[yt-dlp] Загружено {len(audio_data)} байт")
            return audio_data
            
    except subprocess.TimeoutExpired:
        raise Exception("Превышено время загрузки видео")
    except Exception as e:
        raise Exception(f"Ошибка yt-dlp: {str(e)}")

def extract_platform_info(url):
    """Определяет платформу по ссылке"""
    if 'youtube.com' in url or 'youtu.be' in url:
        return 'YouTube', 'https://www.youtube.com/watch?v=' + re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', url).group(1) if re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11})', url) else url
    elif 'vimeo.com' in url:
        return 'Vimeo', url
    elif 'rutube.ru' in url:
        return 'Rutube', url
    elif 'dzen.ru' in url or 'zen.yandex.ru' in url:
        return 'Dzen', url
    elif 'con.xl.ru' in url:
        return 'Protected', url
    else:
        return 'Direct', url

class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {format % args}")
    
    def send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()
    
    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            try:
                with open('index.html', 'rb') as f:
                    html = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(html)))
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(html)
            except:
                self.send_error(404, "index.html not found")
        else:
            self.send_error(404, "Not found")
    
    def do_POST(self):
        if self.path == '/transcribe':
            self.handle_transcribe()
        elif self.path == '/download-url':
            self.handle_download_url()
        else:
            self.send_error(404, "Not found")
    
    def handle_download_url(self):
        """Обработка загрузки видео по ссылке"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_error(400, "No data")
                return
            
            data = json.loads(self.rfile.read(content_length))
            url = data.get('url', '')
            
            if not url:
                self.send_json_error("URL не указан")
                return
            
            print(f"[download] Загрузка: {url}")
            platform, clean_url = extract_platform_info(url)
            print(f"[download] Платформа: {platform}")
            
            # Пробуем скачать через yt-dlp
            audio_data = download_video_with_ytdlp(clean_url)
            
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Content-Length', str(len(audio_data)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(audio_data)
            
        except Exception as e:
            print(f"[download] Ошибка: {e}")
            self.send_json_error(str(e))
    
    def handle_transcribe(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_error(400, "No data")
                return
            
            boundary = None
            content_type = self.headers.get('Content-Type', '')
            if 'boundary=' in content_type:
                boundary = content_type.split('boundary=')[1].encode()
            
            if not boundary:
                self.send_error(400, "No boundary")
                return
            
            data = self.rfile.read(content_length)
            parts = data.split(b'--' + boundary)
            form_data = {}
            file_data = None
            
            for part in parts:
                if b'Content-Disposition' in part:
                    headers, body = part.split(b'\r\n\r\n', 1)
                    body = body.rstrip(b'\r\n--')
                    
                    if b'name="file"' in headers:
                        file_data = body
                    elif b'name="url"' in headers:
                        form_data['url'] = body.decode()
                    elif b'name="provider"' in headers:
                        form_data['provider'] = body.decode()
                    elif b'name="api_key"' in headers:
                        form_data['api_key'] = body.decode()
                    elif b'name="language"' in headers:
                        form_data['language'] = body.decode()
                    elif b'name="model"' in headers:
                        form_data['model'] = body.decode()
                    elif b'name="diarize"' in headers:
                        form_data['diarize'] = body.decode().lower() == 'true'
            
            provider = form_data.get('provider', 'deepgram')
            api_key = form_data.get('api_key', '')
            language = form_data.get('language', 'ru')
            model = form_data.get('model', 'nova-3')
            diarize = form_data.get('diarize', True)
            url = form_data.get('url', '')
            
            if not api_key:
                self.send_json_error("API ключ не передан")
                return
            
            # Если есть URL, скачиваем видео
            if url and not file_data:
                try:
                    self.send_json_progress("Скачивание видео...", 20)
                    audio_data = download_video_with_ytdlp(url)
                except Exception as e:
                    self.send_json_error(f"Не удалось скачать видео: {str(e)}")
                    return
            elif file_data:
                audio_data = file_data
            else:
                self.send_json_error("Нет файла или ссылки")
                return
            
            # Отправляем на транскрибацию
            if provider == 'deepgram':
                result = self.transcribe_deepgram(api_key, audio_data, language, model, diarize)
            elif provider == 'assemblyai':
                result = self.transcribe_assemblyai(api_key, audio_data, language, model, diarize)
            elif provider == 'gladia':
                result = self.transcribe_gladia(api_key, audio_data, language, diarize)
            else:
                self.send_json_error(f"Неизвестный провайдер: {provider}")
                return
            
            self.send_json(result)
            
        except Exception as e:
            print(f"[transcribe] Ошибка: {e}")
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
            raise Exception(f"Deepgram API error {e.code}: {error_body}")
        
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
    
    def transcribe_assemblyai(self, api_key, audio_data, language, model, diarize):
        # Upload
        upload_req = urllib.request.Request(
            'https://api.assemblyai.com/v2/upload',
            data=audio_data,
            headers={'Authorization': api_key, 'Content-Type': 'application/octet-stream'},
            method='POST'
        )
        
        try:
            with urllib.request.urlopen(upload_req, timeout=120) as response:
                upload_url = json.loads(response.read())['upload_url']
        except Exception as e:
            raise Exception(f"AssemblyAI upload error: {e}")
        
        # Create transcript
        transcript_data = {
            'audio_url': upload_url,
            'speaker_labels': diarize,
            'punctuate': True,
            'format_text': True
        }
        
        if language != 'auto':
            transcript_data['language_code'] = language
            transcript_data['language_detection'] = False
        else:
            transcript_data['language_detection'] = True
        
        create_req = urllib.request.Request(
            'https://api.assemblyai.com/v2/transcript',
            data=json.dumps(transcript_data).encode(),
            headers={'Authorization': api_key, 'Content-Type': 'application/json'},
            method='POST'
        )
        
        try:
            with urllib.request.urlopen(create_req, timeout=60) as response:
                transcript_id = json.loads(response.read())['id']
        except Exception as e:
            raise Exception(f"AssemblyAI create error: {e}")
        
        # Poll for result
        import time
        for _ in range(60):
            time.sleep(2)
            poll_req = urllib.request.Request(
                f'https://api.assemblyai.com/v2/transcript/{transcript_id}',
                headers={'Authorization': api_key},
                method='GET'
            )
            try:
                with urllib.request.urlopen(poll_req, timeout=30) as response:
                    result = json.loads(response.read())
                    if result['status'] == 'completed':
                        break
                    elif result['status'] == 'error':
                        raise Exception(f"AssemblyAI error: {result.get('error')}")
            except Exception as e:
                raise Exception(f"AssemblyAI poll error: {e}")
        else:
            raise Exception("AssemblyAI timeout")
        
        transcript = result.get('text', '')
        confidence = result.get('confidence', 0)
        duration = result.get('audio_duration', 0)
        
        utterances = []
        for u in result.get('utterances', []):
            utterances.append({
                'speaker': u.get('speaker', 0),
                'text': u.get('text', ''),
                'start': u.get('start', 0) / 1000,
                'end': u.get('end', 0) / 1000,
                'confidence': u.get('confidence', 0)
            })
        
        if not utterances and transcript:
            utterances.append({
                'speaker': 0,
                'text': transcript,
                'start': 0,
                'end': duration,
                'confidence': confidence
            })
        
        return {
            'success': True,
            'provider': 'assemblyai',
            'transcript': transcript,
            'utterances': utterances,
            'metadata': {
                'duration': duration,
                'confidence': confidence,
                'language': result.get('language_code', language),
                'model': model
            }
        }
    
    def transcribe_gladia(self, api_key, audio_data, language, diarize):
        import base64
        audio_base64 = base64.b64encode(audio_data).decode()
        
        request_data = {
            'audio': audio_base64,
            'diarization': diarize,
            'subtitles': False,
            'summarization': False
        }
        
        if language != 'auto':
            request_data['language'] = language
            request_data['detect_language'] = False
        else:
            request_data['detect_language'] = True
        
        if diarize:
            request_data['diarization_config'] = {'min_speakers': 1, 'max_speakers': 10}
        
        req = urllib.request.Request(
            'https://api.gladia.io/v2/pre-recorded',
            data=json.dumps(request_data).encode(),
            headers={'x-gladia-key': api_key, 'Content-Type': 'application/json'},
            method='POST'
        )
        
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                job_id = json.loads(response.read())['id']
        except Exception as e:
            raise Exception(f"Gladia error: {e}")
        
        import time
        for _ in range(60):
            time.sleep(2)
            poll_req = urllib.request.Request(
                f'https://api.gladia.io/v2/pre-recorded/{job_id}',
                headers={'x-gladia-key': api_key},
                method='GET'
            )
            try:
                with urllib.request.urlopen(poll_req, timeout=30) as response:
                    result = json.loads(response.read())
                    if result['status'] == 'done':
                        break
                    elif result['status'] == 'error':
                        raise Exception(f"Gladia error: {result.get('error')}")
            except Exception as e:
                raise Exception(f"Gladia poll error: {e}")
        else:
            raise Exception("Gladia timeout")
        
        result_data = result.get('result', {})
        transcription = result_data.get('transcription', {})
        transcript = transcription.get('full_transcript', '')
        duration = result_data.get('metadata', {}).get('audio_duration', 0)
        
        utterances = []
        for u in transcription.get('utterances', []):
            utterances.append({
                'speaker': u.get('speaker', 0),
                'text': u.get('text', ''),
                'start': u.get('time_begin', 0),
                'end': u.get('time_end', 0),
                'confidence': u.get('confidence', 0)
            })
        
        if not utterances and transcript:
            utterances.append({
                'speaker': 0,
                'text': transcript,
                'start': 0,
                'end': duration,
                'confidence': 0
            })
        
        return {
            'success': True,
            'provider': 'gladia',
            'transcript': transcript,
            'utterances': utterances,
            'metadata': {
                'duration': duration,
                'confidence': 0,
                'language': language,
                'model': 'solaria'
            }
        }
    
    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)
    
    def send_json_error(self, message):
        self.send_json({'success': False, 'error': message})
    
    def send_json_progress(self, message, pct):
        self.send_json({'progress': True, 'message': message, 'pct': pct})

if __name__ == '__main__':
    print(f"🚀 VoxCraft запущен на порту {PORT}")
    print(f"✅ Поддерживаются: YouTube, Vimeo, Rutube, Dzen")
    print(f"📦 Для работы требуется установить yt-dlp: pip install yt-dlp")
    server = HTTPServer(('0.0.0.0', PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Остановка сервера...")
