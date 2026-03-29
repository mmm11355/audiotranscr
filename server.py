#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VoxCraft — Мульти-провайдерный аудио-транскрибер (для Render.com)
"""

import http.server
import json
import subprocess
import tempfile
import urllib.request
import urllib.error
import os
import sys
import time
import shutil
import socketserver
import io

PORT = int(os.environ.get("PORT", 8800))
POLL_INTERVAL = 3
MAX_POLL_TIME = 600

def error_response(code):
    mapping = {
        400: "Неверный запрос", 401: "Неверный API ключ", 402: "Закончились кредиты",
        403: "Доступ запрещён", 404: "Ресурс не найден", 413: "Файл слишком большой",
        429: "Слишком много запросов", 500: "Ошибка сервера", 503: "Сервис недоступен",
    }
    return mapping.get(code, f"Ошибка HTTP {code}")

def make_request(url, method="GET", headers=None, data=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        body = b""
        try: body = e.read()
        except: pass
        return e.code, body
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ошибка соединения: {e.reason}")

def build_multipart(fields, files, boundary=None):
    if boundary is None:
        boundary = "VoxCraftBoundary--" + str(int(time.time()))
    body = b""
    CRLF = b"\r\n"
    for name, value in (fields or {}).items():
        body += (f"--{boundary}\r\n").encode()
        body += (f'Content-Disposition: form-data; name="{name}"\r\n\r\n').encode()
        body += value.encode() + CRLF
    for (field_name, filename, content_type, file_bytes) in (files or []):
        body += (f"--{boundary}\r\n").encode()
        body += (f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n').encode()
        body += (f"Content-Type: {content_type}\r\n\r\n").encode()
        body += file_bytes + CRLF
    body += (f"--{boundary}--\r\n").encode()
    return body, f"multipart/form-data; boundary={boundary}"

def find_ffmpeg():
    # На Render.com ffmpeg уже установлен
    cmd = shutil.which("ffmpeg")
    if cmd: return cmd
    # Проверяем стандартные пути
    common_paths = ["/usr/bin/ffmpeg", "/bin/ffmpeg"]
    for path in common_paths:
        if os.path.exists(path):
            return path
    return None

def extract_audio_from_path(input_path):
    ffmpeg_cmd = find_ffmpeg()
    if not ffmpeg_cmd:
        raise RuntimeError("ffmpeg не найден на сервере")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = os.path.join(tmpdir, "output.mp3")
        
        # Пробуем разные методы извлечения аудио
        methods = [
            # Метод 1: Стандартный
            [ffmpeg_cmd, "-y", "-i", input_path, "-vn", "-acodec", "libmp3lame", 
             "-ac", "1", "-ar", "16000", "-b:a", "64k", output_path],
            # Метод 2: С игнорированием ошибок
            [ffmpeg_cmd, "-y", "-err_detect", "ignore_err", "-fflags", "+genpts+igndts",
             "-i", input_path, "-vn", "-acodec", "libmp3lame", "-ac", "1", 
             "-ar", "16000", "-b:a", "64k", output_path],
            # Метод 3: Только аудио
            [ffmpeg_cmd, "-y", "-i", input_path, "-vn", "-acodec", "copy", output_path]
        ]
        
        success = False
        last_error = ""
        
        for cmd in methods:
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=300)
                if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    success = True
                    break
                else:
                    last_error = result.stderr.decode("utf-8", errors="replace")[-500:]
            except Exception as e:
                last_error = str(e)
        
        if not success:
            raise RuntimeError(f"Не удалось извлечь аудио: {last_error}")
        
        with open(output_path, "rb") as f:
            return f.read()

def transcribe_deepgram(api_key, audio_bytes, mime_type, language, model, diarize):
    params = [f"model={model or 'nova-3'}", f"language={language or 'ru'}", "smart_format=true", "paragraphs=true"]
    if diarize: params += ["diarize=true", "utterances=true"]
    url = "https://api.deepgram.com/v1/listen?" + "&".join(params)
    headers = {"Authorization": f"Token {api_key}", "Content-Type": mime_type or "audio/mpeg"}
    status, body = make_request(url, method="POST", headers=headers, data=audio_bytes, timeout=300)
    if status != 200:
        try:
            err_json = json.loads(body)
            err_msg = err_json.get("err_msg") or err_json.get("message") or error_response(status)
        except: err_msg = error_response(status)
        raise RuntimeError(f"Deepgram: {err_msg}")
    
    data = json.loads(body)
    results = data.get("results", {})
    channels = results.get("channels", [{}])
    alt = channels[0].get("alternatives", [{}])[0] if channels else {}
    transcript = alt.get("transcript", "")
    confidence = alt.get("confidence", 0)
    
    words = []
    for w in alt.get("words", []):
        words.append({
            "word": w.get("punctuated_word") or w.get("word", ""),
            "start": float(w.get("start", 0)), "end": float(w.get("end", 0)),
            "confidence": float(w.get("confidence", 0)),
            "speaker": int(w.get("speaker", 0)) if w.get("speaker") is not None else 0,
        })

    utterances = []
    utterances_raw = results.get("utterances") or []
    if utterances_raw:
        for u in utterances_raw:
            utterances.append({
                "speaker": int(u.get("speaker", 0)), "text": u.get("transcript", ""),
                "start": float(u.get("start", 0)), "end": float(u.get("end", 0)),
                "confidence": float(u.get("confidence", 0)),
            })
    else:
        paragraphs = alt.get("paragraphs", {}).get("paragraphs", [])
        for p in paragraphs:
            text_parts = [s.get("text", "") for s in p.get("sentences", [])]
            utterances.append({
                "speaker": int(p.get("speaker", 0)), "text": " ".join(text_parts),
                "start": float(p.get("start", 0)), "end": float(p.get("end", 0)),
                "confidence": confidence,
            })

    meta = data.get("metadata", {})
    return {
        "success": True, "provider": "deepgram", "transcript": transcript,
        "utterances": utterances, "words": words,
        "metadata": {"duration": float(meta.get("duration", 0)), "confidence": round(confidence, 4), "language": language or "ru", "model": model or "nova-3"}
    }

def _speaker_label_to_int(label):
    if label is None: return 0
    if isinstance(label, int): return label
    if isinstance(label, str) and len(label) == 1 and label.isupper():
        return ord(label) - ord("A")
    try: return int(label)
    except: return 0

def transcribe_assemblyai(api_key, audio_bytes, language, model, diarize):
    # Upload
    status, body = make_request("https://api.assemblyai.com/v2/upload", method="POST", headers={"authorization": api_key, "Content-Type": "application/octet-stream"}, data=audio_bytes, timeout=300)
    if status != 200: raise RuntimeError(f"AssemblyAI upload: {error_response(status)}")
    upload_url = json.loads(body)["upload_url"]

    # Create
    use_lang_detection = (language == "auto")
    transcript_req = {"audio_url": upload_url, "speech_models": [model or "universal-2"], "speaker_labels": bool(diarize)}
    if use_lang_detection: transcript_req["language_detection"] = True
    else:
        transcript_req["language_code"] = language or "ru"
        transcript_req["language_detection"] = False

    status, body = make_request("https://api.assemblyai.com/v2/transcript", method="POST", headers={"authorization": api_key, "Content-Type": "application/json"}, data=json.dumps(transcript_req).encode("utf-8"), timeout=60)
    if status != 200:
        try: err_json = json.loads(body); err_msg = err_json.get("error") or error_response(status)
        except: err_msg = error_response(status)
        raise RuntimeError(f"AssemblyAI create: {err_msg}")
    
    transcript_id = json.loads(body)["id"]

    # Polling
    start_time = time.time()
    while True:
        if time.time() - start_time > MAX_POLL_TIME: raise RuntimeError("AssemblyAI: таймаут ожидания")
        time.sleep(POLL_INTERVAL)
        status, body = make_request(f"https://api.assemblyai.com/v2/transcript/{transcript_id}", method="GET", headers={"authorization": api_key}, timeout=60)
        if status != 200: raise RuntimeError(f"AssemblyAI poll: {error_response(status)}")
        result = json.loads(body)
        job_status = result.get("status")
        if job_status == "completed":
            return normalize_assemblyai(result, model, language)
        elif job_status == "error":
            raise RuntimeError(f"AssemblyAI: {result.get('error', 'Ошибка')}")

def normalize_assemblyai(data, model, language):
    transcript = data.get("text", "")
    confidence = float(data.get("confidence") or 0)
    duration = float((data.get("audio_duration") or 0))
    words = []
    for w in (data.get("words") or []):
        words.append({
            "word": w.get("text", ""),
            "start": round(float(w.get("start", 0)) / 1000, 3),
            "end": round(float(w.get("end", 0)) / 1000, 3),
            "confidence": float(w.get("confidence", 0)),
            "speaker": _speaker_label_to_int(w.get("speaker")),
        })
    utterances = []
    for u in (data.get("utterances") or []):
        utterances.append({
            "speaker": _speaker_label_to_int(u.get("speaker")), "text": u.get("text", ""),
            "start": round(float(u.get("start", 0)) / 1000, 3), "end": round(float(u.get("end", 0)) / 1000, 3),
            "confidence": float(u.get("confidence", 0)),
        })
    if not utterances and transcript:
        utterances.append({"speaker": 0, "text": transcript, "start": 0.0, "end": duration, "confidence": confidence})
    
    return {
        "success": True, "provider": "assemblyai", "transcript": transcript,
        "utterances": utterances, "words": words,
        "metadata": {"duration": duration, "confidence": round(confidence, 4), "language": data.get("language_code") or language or "auto", "model": model or "universal-2"}
    }

def transcribe_gladia(api_key, audio_bytes, language, diarize):
    # Upload
    body_bytes, content_type = build_multipart(fields={}, files=[("audio", "audio.mp3", "audio/mpeg", audio_bytes)])
    status, body = make_request("https://api.gladia.io/v2/upload", method="POST", headers={"x-gladia-key": api_key, "Content-Type": content_type}, data=body_bytes, timeout=300)
    if status not in (200, 201):
        try: err_json = json.loads(body); err_msg = err_json.get("message") or err_json.get("error") or error_response(status)
        except: err_msg = error_response(status)
        raise RuntimeError(f"Gladia upload: {err_msg}")
    
    upload_data = json.loads(body)
    audio_url = upload_data.get("audio_url", "")
    detected_duration = float(upload_data.get("audio_metadata", {}).get("audio_duration", 0))

    # Transcribe
    use_lang_detection = (language == "auto")
    transcription_req = {"audio_url": audio_url, "diarization": bool(diarize), "subtitles": False, "summarization": False}
    if use_lang_detection: transcription_req["detect_language"] = True
    else:
        transcription_req["language"] = language or "ru"
        transcription_req["detect_language"] = False
    if diarize: transcription_req["diarization_config"] = {"min_speakers": 1, "max_speakers": 10}

    status, body = make_request("https://api.gladia.io/v2/pre-recorded", method="POST", headers={"x-gladia-key": api_key, "Content-Type": "application/json"}, data=json.dumps(transcription_req).encode("utf-8"), timeout=60)
    if status not in (200, 201):
        try: err_json = json.loads(body); err_msg = err_json.get("message") or err_json.get("error") or error_response(status)
        except: err_msg = error_response(status)
        raise RuntimeError(f"Gladia transcribe: {err_msg}")
    
    job_id = json.loads(body).get("id", "")

    # Polling
    start_time = time.time()
    while True:
        if time.time() - start_time > MAX_POLL_TIME: raise RuntimeError("Gladia: таймаут ожидания")
        time.sleep(POLL_INTERVAL)
        status, body = make_request(f"https://api.gladia.io/v2/pre-recorded/{job_id}", method="GET", headers={"x-gladia-key": api_key}, timeout=60)
        if status != 200: raise RuntimeError(f"Gladia poll: {error_response(status)}")
        result = json.loads(body)
        job_status = result.get("status")
        if job_status == "done":
            return normalize_gladia(result, language, detected_duration)
        elif job_status == "error":
            err_detail = result.get("result", {}).get("error", "Ошибка транскрибации")
            raise RuntimeError(f"Gladia: {err_detail}")

def normalize_gladia(data, language, detected_duration=0.0):
    result = data.get("result", {})
    meta = result.get("metadata", {})
    transcription = result.get("transcription", {})
    transcript = transcription.get("full_transcript", "")
    duration = float(meta.get("audio_duration") or detected_duration or 0)
    
    utterances = []
    words_all = []
    gladia_utterances = transcription.get("utterances") or []
    
    for u in gladia_utterances:
        speaker_raw = u.get("speaker")
        speaker_int = int(speaker_raw) if speaker_raw is not None else 0
        utterances.append({
            "speaker": speaker_int, "text": u.get("text", ""),
            "start": float(u.get("time_begin") or u.get("start", 0)),
            "end": float(u.get("time_end") or u.get("end", 0)),
            "confidence": float(u.get("confidence", 0)),
        })
        for w in (u.get("words") or []):
            words_all.append({
                "word": w.get("word", ""), "start": float(w.get("start", 0)),
                "end": float(w.get("end", 0)), "confidence": float(w.get("confidence", 0)),
                "speaker": speaker_int,
            })
    
    if not utterances and transcript:
        utterances.append({"speaker": 0, "text": transcript, "start": 0.0, "end": duration, "confidence": 0.0})
    
    detected_lang = language if language != "auto" else (gladia_utterances[0].get("language", "auto") if gladia_utterances else "auto")
    
    return {
        "success": True, "provider": "gladia", "transcript": transcript,
        "utterances": utterances, "words": words_all,
        "metadata": {"duration": duration, "confidence": 0.0, "language": detected_lang, "model": "solaria"}
    }

class VoxCraftHandler(http.server.BaseHTTPRequestHandler):
    timeout = 300
    rbufsize = 256 * 1024
    wbufsize = 256 * 1024

    def log_message(self, format, *args):
        sys.stdout.write(f"  [{self.address_string()}] {format % args}\n")
        sys.stdout.flush()

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message, status=500):
        self.send_json({"success": False, "error": message}, status)

    def send_bytes(self, data, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Api-Key, X-Language, X-Model, X-Diarize, X-File-Extension")
        self.end_headers()

    def do_GET(self):
        # Пробуем найти index.html в разных местах
        possible_paths = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"),
            os.path.join(os.getcwd(), "index.html"),
            "/opt/render/project/src/index.html"
        ]
        
        html_path = None
        for path in possible_paths:
            if os.path.exists(path):
                html_path = path
                break
        
        if not html_path:
            self.send_error(404, "index.html not found")
            return
        
        with open(html_path, "rb") as f:
            html = f.read()
        self.send_bytes(html, "text/html; charset=utf-8")

    def _read_body(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0: return b""
        return self.rfile.read(content_length)

    def do_POST(self):
        path = self.path.split("?")[0]
        
        if path == "/api/check-file":
            try:
                body = self._read_body()
                req = json.loads(body)
                fpath = req.get("path", "").strip().strip('"\'')
                
                # На Render.com путь на диске не работает
                self.send_json({"ok": False, "error": "Режим 'Путь на диске' недоступен в облаке. Используйте загрузку файла."})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})
            return

        if path == "/api/transcribe-by-path-sse":
            # На Render.com путь на диске не работает
            self.send_error_json("Режим 'Путь на диске' недоступен в облаке. Используйте загрузку файла.", 400)
            return

        if path.startswith("/api/transcribe/"):
            provider = path.replace("/api/transcribe/", "").strip("/")
            api_key = self.headers.get("X-Api-Key", "")
            language = self.headers.get("X-Language", "ru")
            model = self.headers.get("X-Model", "")
            diarize = self.headers.get("X-Diarize", "false").lower() in ("true", "1", "yes")
            is_video = self.headers.get("X-Is-Video", "false").lower() in ("true", "1", "yes")
            file_ext = self.headers.get("X-File-Extension", "mp4").lstrip(".")

            if not api_key:
                self.send_error_json("API ключ не передан", 400)
                return
            
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0:
                self.send_error_json("Файл не передан", 400)
                return

            print(f"  [transcribe/{provider}] lang={language} model={model} is_video={is_video}")

            tmpdir_obj = None
            try:
                # Читаем файл
                file_bytes = self._read_body()
                
                if is_video:
                    import tempfile as _tf
                    tmpdir_obj = _tf.TemporaryDirectory()
                    tmp_input = os.path.join(tmpdir_obj.name, f"input.{file_ext}")
                    
                    with open(tmp_input, "wb") as f:
                        f.write(file_bytes)
                    
                    # Извлекаем аудио
                    audio_bytes = extract_audio_from_path(tmp_input)
                    audio_mime = "audio/mpeg"
                else:
                    audio_bytes = file_bytes
                    audio_mime = "audio/mpeg"

                if provider == "deepgram":
                    result = transcribe_deepgram(api_key, audio_bytes, audio_mime, language, model, diarize)
                elif provider == "assemblyai":
                    result = transcribe_assemblyai(api_key, audio_bytes, language, model, diarize)
                elif provider == "gladia":
                    result = transcribe_gladia(api_key, audio_bytes, language, diarize)
                else:
                    self.send_error_json(f"Неизвестный провайдер: {provider}", 400)
                    return

                self.send_json(result)
            except Exception as e:
                print(f"  [transcribe/{provider}] ERROR: {e}")
                self.send_error_json(str(e))
            finally:
                if tmpdir_obj:
                    try:
                        tmpdir_obj.cleanup()
                    except:
                        pass
            return

        self.send_error(404, "Not found")

if __name__ == "__main__":
    with socketserver.TCPServer(("0.0.0.0", PORT), VoxCraftHandler) as httpd:
        print(f"🚀 VoxCraft запущен на порту {PORT}")
        print(f"✅ Адаптировано для Render.com")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n👋 Остановка сервера...")
