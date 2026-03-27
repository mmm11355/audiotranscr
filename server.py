#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VoxCraft — Мульти-провайдерный аудио-транскрибер
Python HTTP сервер (стандартная библиотека, без pip install)
Порт: 8800
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
import threading
import io

PORT = 8800
POLL_INTERVAL = 3       # секунды между запросами поллинга
MAX_POLL_TIME = 600     # максимальное время ожидания (10 минут)

# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────

def error_response(code):
    """Маппинг HTTP-кодов ошибок на понятные русские сообщения."""
    mapping = {
        400: "Неверный запрос — проверьте параметры",
        401: "Неверный API ключ — проверьте ключ в настройках",
        402: "Закончились кредиты — пополните баланс",
        403: "Доступ запрещён — проверьте права API ключа",
        404: "Ресурс не найден",
        413: "Файл слишком большой (максимум 2 GB)",
        429: "Слишком много запросов — подождите и попробуйте снова",
        500: "Внутренняя ошибка сервера провайдера",
        503: "Сервис провайдера временно недоступен",
    }
    return mapping.get(code, f"Ошибка HTTP {code}")


def make_request(url, method="GET", headers=None, data=None, timeout=120):
    """
    Выполнить HTTP-запрос через urllib.
    Возвращает (status_code, response_body_bytes).
    """
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return e.code, body
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ошибка соединения: {e.reason}")


def build_multipart(fields, files, boundary=None):
    """
    Сформировать multipart/form-data тело вручную.
    fields — dict строковых полей
    files  — list of (field_name, filename, content_type, data_bytes)
    """
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


# ─────────────────────────────────────────────────────────────────────────────
# Конвертация видео → MP3 через ffmpeg
# ─────────────────────────────────────────────────────────────────────────────

def find_ffmpeg():
    """Ищет ffmpeg в PATH и рядом с server.py."""
    cmd = shutil.which("ffmpeg")
    if cmd:
        return cmd
    for name in ("ffmpeg.exe", "ffmpeg"):
        local = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        if os.path.exists(local):
            return local
    return None


def extract_audio(video_bytes, ext):
    """
    Конвертирует видео/аудио в MP3 16kHz mono через ffmpeg.
    Использует pipe (stdin→stdout) — файл не пишется на диск,
    работает даже с 500MB+ без OOM.
    """
    ffmpeg_cmd = find_ffmpeg()
    if not ffmpeg_cmd:
        raise RuntimeError(
            "ffmpeg не найден. Скачайте ffmpeg и поместите рядом с server.py или добавьте в PATH"
        )

    cmd = [
        ffmpeg_cmd, "-y",
        "-f", ext.lstrip("."),   # явно указываем формат входа
        "-i", "pipe:0",          # читаем из stdin
        "-vn",                   # убираем видео
        "-acodec", "libmp3lame",
        "-ac", "1",
        "-ar", "16000",
        "-b:a", "64k",
        "-f", "mp3",
        "pipe:1",                # пишем в stdout
    ]

    proc = subprocess.run(
        cmd,
        input=video_bytes,
        capture_output=True,
        timeout=600,             # 10 минут для больших файлов
    )

    if proc.returncode != 0 or len(proc.stdout) < 100:
        # pipe:0 не работает для некоторых форматов (mkv, avi) — fallback через tmpfile
        return _extract_audio_tmpfile(video_bytes, ext, ffmpeg_cmd)

    return proc.stdout


def _extract_audio_tmpfile(video_bytes, ext, ffmpeg_cmd):
    """Fallback: пишем во временный файл (для форматов не поддерживающих pipe)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path  = os.path.join(tmpdir, f"input.{ext.lstrip('.')}")
        output_path = os.path.join(tmpdir, "output.mp3")
        with open(input_path, "wb") as f:
            f.write(video_bytes)
        cmd = [
            ffmpeg_cmd, "-y", "-i", input_path,
            "-vn", "-acodec", "libmp3lame",
            "-ac", "1", "-ar", "16000", "-b:a", "64k",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg завершился с ошибкой:\n{err[-2000:]}")
        with open(output_path, "rb") as f:
            return f.read()


def extract_audio_from_path(input_path):
    """
    Конвертирует файл с ДИСКА в MP3 16kHz mono через ffmpeg.
    НЕ загружает исходный файл в RAM — ffmpeg читает прямо с диска.
    Возвращает bytes MP3 (обычно 50-80 MB для часового видео).
    """
    ffmpeg_cmd = find_ffmpeg()
    if not ffmpeg_cmd:
        raise RuntimeError(
            "ffmpeg не найден. Скачайте ffmpeg и поместите рядом с server.py или добавьте в PATH"
        )
    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = os.path.join(tmpdir, "output.mp3")
        cmd = [
            ffmpeg_cmd, "-y",
            "-i", input_path,          # читаем прямо с диска — без stdin
            "-vn",
            "-acodec", "libmp3lame",
            "-ac", "1",
            "-ar", "16000",
            "-b:a", "64k",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg завершился с ошибкой:\n{err[-2000:]}")
        with open(output_path, "rb") as f:
            return f.read()


# ─────────────────────────────────────────────────────────────────────────────
# DEEPGRAM
# ─────────────────────────────────────────────────────────────────────────────

def transcribe_deepgram(api_key, audio_bytes, mime_type, language, model, diarize):
    """Синхронная транскрибация через Deepgram API."""
    params = [
        f"model={model or 'nova-3'}",
        f"language={language or 'ru'}",
        "smart_format=true",
        "paragraphs=true",
    ]
    if diarize:
        params += ["diarize=true", "utterances=true"]
    url = "https://api.deepgram.com/v1/listen?" + "&".join(params)

    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": mime_type or "audio/mpeg",
    }
    status, body = make_request(url, method="POST", headers=headers, data=audio_bytes, timeout=300)
    if status != 200:
        try:
            err_json = json.loads(body)
            err_msg = err_json.get("err_msg") or err_json.get("message") or error_response(status)
        except Exception:
            err_msg = error_response(status)
        raise RuntimeError(f"Deepgram: {err_msg}")

    data = json.loads(body)
    return normalize_deepgram(data, model, language)


def normalize_deepgram(data, model, language):
    """Нормализация ответа Deepgram в единый формат."""
    results = data.get("results", {})
    channels = results.get("channels", [{}])
    alt = channels[0].get("alternatives", [{}])[0] if channels else {}

    transcript = alt.get("transcript", "")
    confidence = alt.get("confidence", 0)

    # Слова
    words_raw = alt.get("words", [])
    words = []
    for w in words_raw:
        words.append({
            "word":       w.get("punctuated_word") or w.get("word", ""),
            "start":      float(w.get("start", 0)),
            "end":        float(w.get("end", 0)),
            "confidence": float(w.get("confidence", 0)),
            "speaker":    int(w.get("speaker", 0)) if w.get("speaker") is not None else 0,
        })

    # Utterances
    utterances_raw = results.get("utterances") or []
    utterances = []
    if utterances_raw:
        for u in utterances_raw:
            utterances.append({
                "speaker":    int(u.get("speaker", 0)),
                "text":       u.get("transcript", ""),
                "start":      float(u.get("start", 0)),
                "end":        float(u.get("end", 0)),
                "confidence": float(u.get("confidence", 0)),
            })
    else:
        # Если diarization не включён — один utterance из параграфов
        paragraphs = alt.get("paragraphs", {}).get("paragraphs", [])
        if paragraphs:
            for p in paragraphs:
                text_parts = [s.get("text", "") for s in p.get("sentences", [])]
                utterances.append({
                    "speaker":    int(p.get("speaker", 0)),
                    "text":       " ".join(text_parts),
                    "start":      float(p.get("start", 0)),
                    "end":        float(p.get("end", 0)),
                    "confidence": confidence,
                })

    meta = data.get("metadata", {})
    duration = float(meta.get("duration", 0))

    return {
        "success":    True,
        "provider":   "deepgram",
        "transcript": transcript,
        "utterances": utterances,
        "words":      words,
        "metadata": {
            "duration":   duration,
            "confidence": round(confidence, 4),
            "language":   language or "ru",
            "model":      model or "nova-3",
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# ASSEMBLYAI
# ─────────────────────────────────────────────────────────────────────────────

def transcribe_assemblyai(api_key, audio_bytes, language, model, diarize):
    """Асинхронная транскрибация через AssemblyAI (3 шага)."""

    # Шаг 1: Загрузить файл
    status, body = make_request(
        "https://api.assemblyai.com/v2/upload",
        method="POST",
        headers={
            "authorization": api_key,
            "Content-Type": "application/octet-stream",
        },
        data=audio_bytes,
        timeout=300,
    )
    if status != 200:
        raise RuntimeError(f"AssemblyAI upload: {error_response(status)}")
    upload_url = json.loads(body)["upload_url"]

    # Шаг 2: Создать транскрипцию
    use_lang_detection = (language == "auto")
    transcript_req = {
        "audio_url":      upload_url,
        "speech_models":  [model or "universal-2"],   # speech_model устарел
        "speaker_labels": bool(diarize),
    }
    if use_lang_detection:
        transcript_req["language_detection"] = True
    else:
        transcript_req["language_code"] = language or "ru"
        transcript_req["language_detection"] = False

    status, body = make_request(
        "https://api.assemblyai.com/v2/transcript",
        method="POST",
        headers={
            "authorization": api_key,
            "Content-Type": "application/json",
        },
        data=json.dumps(transcript_req).encode("utf-8"),
        timeout=60,
    )
    if status != 200:
        try:
            err_json = json.loads(body)
            err_msg = err_json.get("error") or error_response(status)
        except Exception:
            err_msg = error_response(status)
        raise RuntimeError(f"AssemblyAI create: {err_msg}")

    transcript_id = json.loads(body)["id"]

    # Шаг 3: Поллинг
    start_time = time.time()
    while True:
        if time.time() - start_time > MAX_POLL_TIME:
            raise RuntimeError("AssemblyAI: превышено время ожидания (10 минут)")

        time.sleep(POLL_INTERVAL)
        status, body = make_request(
            f"https://api.assemblyai.com/v2/transcript/{transcript_id}",
            method="GET",
            headers={"authorization": api_key},
            timeout=60,
        )
        if status != 200:
            raise RuntimeError(f"AssemblyAI poll: {error_response(status)}")

        result = json.loads(body)
        job_status = result.get("status")

        if job_status == "completed":
            return normalize_assemblyai(result, model, language)
        elif job_status == "error":
            raise RuntimeError(f"AssemblyAI: {result.get('error', 'Неизвестная ошибка транскрибации')}")
        # else: queued / processing — продолжаем поллинг


def normalize_assemblyai(data, model, language):
    """Нормализация ответа AssemblyAI в единый формат. Таймкоды ms → s."""
    transcript = data.get("text", "")
    confidence = float(data.get("confidence") or 0)
    duration   = float((data.get("audio_duration") or 0))

    # Слова (таймкоды в ms → s)
    words = []
    for w in (data.get("words") or []):
        words.append({
            "word":       w.get("text", ""),
            "start":      round(float(w.get("start", 0)) / 1000, 3),
            "end":        round(float(w.get("end", 0)) / 1000, 3),
            "confidence": float(w.get("confidence", 0)),
            "speaker":    _speaker_label_to_int(w.get("speaker")),
        })

    # Utterances
    utterances = []
    for u in (data.get("utterances") or []):
        utterances.append({
            "speaker":    _speaker_label_to_int(u.get("speaker")),
            "text":       u.get("text", ""),
            "start":      round(float(u.get("start", 0)) / 1000, 3),
            "end":        round(float(u.get("end", 0)) / 1000, 3),
            "confidence": float(u.get("confidence", 0)),
        })

    # Если utterances нет — один большой блок
    if not utterances and transcript:
        utterances.append({
            "speaker":    0,
            "text":       transcript,
            "start":      0.0,
            "end":        duration,
            "confidence": confidence,
        })

    detected_lang = data.get("language_code") or language or "auto"

    return {
        "success":    True,
        "provider":   "assemblyai",
        "transcript": transcript,
        "utterances": utterances,
        "words":      words,
        "metadata": {
            "duration":   duration,
            "confidence": round(confidence, 4),
            "language":   detected_lang,
            "model":      model or "universal-2",
        }
    }


def _speaker_label_to_int(label):
    """Преобразует 'A', 'B', ... или 0, 1 в целое число."""
    if label is None:
        return 0
    if isinstance(label, int):
        return label
    if isinstance(label, str) and len(label) == 1 and label.isupper():
        return ord(label) - ord("A")
    try:
        return int(label)
    except (ValueError, TypeError):
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# GLADIA
# ─────────────────────────────────────────────────────────────────────────────

def transcribe_gladia(api_key, audio_bytes, language, diarize):
    """Асинхронная транскрибация через Gladia (3 шага)."""

    # Шаг 1: Загрузить файл (multipart/form-data)
    body_bytes, content_type = build_multipart(
        fields={},
        files=[("audio", "audio.mp3", "audio/mpeg", audio_bytes)],
    )
    status, body = make_request(
        "https://api.gladia.io/v2/upload",
        method="POST",
        headers={
            "x-gladia-key": api_key,
            "Content-Type": content_type,
        },
        data=body_bytes,
        timeout=300,
    )
    if status not in (200, 201):
        try:
            err_json = json.loads(body)
            err_msg = err_json.get("message") or err_json.get("error") or error_response(status)
        except Exception:
            err_msg = error_response(status)
        raise RuntimeError(f"Gladia upload: {err_msg}")

    upload_data = json.loads(body)
    audio_url   = upload_data.get("audio_url", "")
    detected_duration = 0.0
    if upload_data.get("audio_metadata"):
        detected_duration = float(upload_data["audio_metadata"].get("audio_duration", 0))

    # Шаг 2: Запустить транскрипцию
    use_lang_detection = (language == "auto")
    transcription_req = {
        "audio_url":    audio_url,
        "diarization":  bool(diarize),
        "subtitles":    False,
        "summarization": False,
    }
    if use_lang_detection:
        transcription_req["detect_language"] = True
    else:
        transcription_req["language"]        = language or "ru"
        transcription_req["detect_language"] = False

    if diarize:
        transcription_req["diarization_config"] = {
            "min_speakers": 1,
            "max_speakers": 10,
        }

    status, body = make_request(
        "https://api.gladia.io/v2/pre-recorded",
        method="POST",
        headers={
            "x-gladia-key": api_key,
            "Content-Type": "application/json",
        },
        data=json.dumps(transcription_req).encode("utf-8"),
        timeout=60,
    )
    if status not in (200, 201):
        try:
            err_json = json.loads(body)
            err_msg = err_json.get("message") or err_json.get("error") or error_response(status)
        except Exception:
            err_msg = error_response(status)
        raise RuntimeError(f"Gladia transcribe: {err_msg}")

    transcription_data = json.loads(body)
    job_id = transcription_data.get("id", "")

    # Шаг 3: Поллинг
    start_time = time.time()
    while True:
        if time.time() - start_time > MAX_POLL_TIME:
            raise RuntimeError("Gladia: превышено время ожидания (10 минут)")

        time.sleep(POLL_INTERVAL)
        status, body = make_request(
            f"https://api.gladia.io/v2/pre-recorded/{job_id}",
            method="GET",
            headers={"x-gladia-key": api_key},
            timeout=60,
        )
        if status != 200:
            raise RuntimeError(f"Gladia poll: {error_response(status)}")

        result = json.loads(body)
        job_status = result.get("status")

        if job_status == "done":
            return normalize_gladia(result, language, detected_duration)
        elif job_status == "error":
            err_detail = ""
            if result.get("result") and result["result"].get("error"):
                err_detail = result["result"]["error"]
            raise RuntimeError(f"Gladia: {err_detail or 'Ошибка транскрибации'}")
        # else: queued / processing — продолжаем


def normalize_gladia(data, language, detected_duration=0.0):
    """Нормализация ответа Gladia в единый формат. Таймкоды уже в секундах."""
    result     = data.get("result", {})
    meta       = result.get("metadata", {})
    transcription = result.get("transcription", {})

    transcript = transcription.get("full_transcript", "")
    duration   = float(meta.get("audio_duration") or detected_duration or 0)

    # Utterances и Words из utterances Gladia
    utterances = []
    words_all  = []
    gladia_utterances = transcription.get("utterances") or []

    for u in gladia_utterances:
        speaker_raw = u.get("speaker")
        speaker_int = int(speaker_raw) if speaker_raw is not None else 0
        utterances.append({
            "speaker":    speaker_int,
            "text":       u.get("text", ""),
            "start":      float(u.get("time_begin") or u.get("start", 0)),
            "end":        float(u.get("time_end")   or u.get("end", 0)),
            "confidence": float(u.get("confidence", 0)),
        })
        for w in (u.get("words") or []):
            words_all.append({
                "word":       w.get("word", ""),
                "start":      float(w.get("start", 0)),
                "end":        float(w.get("end", 0)),
                "confidence": float(w.get("confidence", 0)),
                "speaker":    speaker_int,
            })

    if not utterances and transcript:
        utterances.append({
            "speaker":    0,
            "text":       transcript,
            "start":      0.0,
            "end":        duration,
            "confidence": 0.0,
        })

    detected_lang = language if language != "auto" else (
        gladia_utterances[0].get("language", "auto") if gladia_utterances else "auto"
    )

    return {
        "success":    True,
        "provider":   "gladia",
        "transcript": transcript,
        "utterances": utterances,
        "words":      words_all,
        "metadata": {
            "duration":   duration,
            "confidence": 0.0,
            "language":   detected_lang,
            "model":      "solaria",
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTTP Request Handler
# ─────────────────────────────────────────────────────────────────────────────

class VoxCraftHandler(http.server.BaseHTTPRequestHandler):

    # Увеличиваем таймаут чтения до 5 минут (для больших файлов)
    timeout = 300

    # Буферизованное чтение сокета — 256KB буфер вместо 0 (unbuffered)
    rbufsize = 256 * 1024  # 256 KB
    wbufsize = 256 * 1024  # 256 KB

    def log_message(self, format, *args):
        """Кастомный лог — без лишнего шума."""
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
        """CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Api-Key, X-Language, X-Model, X-Diarize, X-File-Extension")
        self.end_headers()

    def do_GET(self):
        """Отдаём index.html для всех GET-запросов."""
        # Ищем index.html рядом с server.py (абсолютный путь)
        base_dir  = os.path.dirname(os.path.abspath(__file__))
        html_path = os.path.join(base_dir, "index.html")
        if not os.path.exists(html_path):
            # latin-1 safe error message (Python http.server requirement)
            self.send_error(404, "index.html not found")
            return
        with open(html_path, "rb") as f:
            html = f.read()
        self.send_bytes(html, "text/html; charset=utf-8")

    def _read_body(self):
        """Читаем тело чанками по 1MB. Возвращает bytes."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            return b""
        buf = io.BytesIO()
        remaining  = content_length
        chunk_size = 1024 * 1024  # 1 MB
        while remaining > 0:
            chunk = self.rfile.read(min(chunk_size, remaining))
            if not chunk:
                break
            buf.write(chunk)
            remaining -= len(chunk)
        return buf.getvalue()

    def _stream_body_to_file(self, path):
        """
        Читаем тело запроса чанками и пишем сразу на диск.
        Для больших файлов (500MB+) — не держит всё в RAM.
        Возвращает количество записанных байт.
        """
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            return 0
        written    = 0
        remaining  = content_length
        chunk_size = 2 * 1024 * 1024  # 2 MB чанки
        with open(path, "wb") as fout:
            while remaining > 0:
                chunk = self.rfile.read(min(chunk_size, remaining))
                if not chunk:
                    break
                fout.write(chunk)
                written   += len(chunk)
                remaining -= len(chunk)
        return written

    def do_POST(self):
        path = self.path.split("?")[0]

        content_length = int(self.headers.get("Content-Length", 0))
        STREAM_THRESHOLD = 50 * 1024 * 1024  # 50 MB
        large_file = content_length > STREAM_THRESHOLD

        # ── Проверка файла по пути ────────────────────────────────────────
        if path == "/api/check-file":
            try:
                body = self._read_body()
                req  = json.loads(body)
                fpath = req.get("path", "").strip()
                if not fpath:
                    self.send_json({"ok": False, "error": "Путь не указан"})
                    return
                # Нормализуем (убираем кавычки если скопировали с кавычками)
                fpath = fpath.strip('"\'')
                if not os.path.exists(fpath):
                    self.send_json({"ok": False, "error": f"Файл не найден: {fpath}"})
                    return
                if not os.path.isfile(fpath):
                    self.send_json({"ok": False, "error": "Это не файл"})
                    return
                size = os.path.getsize(fpath)
                name = os.path.basename(fpath)
                self.send_json({"ok": True, "path": fpath, "name": name, "size": size})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── Поиск файла по имени (для автоподстановки пути) ──────────────
        if path == "/api/find-file":
            try:
                body  = self._read_body()
                req   = json.loads(body)
                fname = req.get("name", "").strip()
                if not fname:
                    self.send_json({"found": False})
                    return

                # Ищем в типичных папках пользователя
                home = os.path.expanduser("~")
                search_dirs = [
                    os.path.join(home, "Downloads"),
                    os.path.join(home, "Desktop"),
                    os.path.join(home, "Videos"),
                    os.path.join(home, "Documents"),
                    home,
                ]
                # Windows: дополнительные диски
                if os.name == "nt":
                    for drive in ["D:\\", "E:\\", "F:\\"]:
                        if os.path.exists(drive):
                            search_dirs.append(drive)

                for d in search_dirs:
                    candidate = os.path.join(d, fname)
                    if os.path.isfile(candidate):
                        size = os.path.getsize(candidate)
                        self.send_json({
                            "found": True,
                            "path":  candidate,
                            "name":  fname,
                            "size":  size,
                        })
                        return

                self.send_json({"found": False, "searched": search_dirs})
            except Exception as e:
                self.send_json({"found": False, "error": str(e)})
            return

        # ── Транскрибация по пути (без передачи файла через сеть) ─────────
        if path == "/api/transcribe-by-path":
            try:
                body = self._read_body()
                req  = json.loads(body)

                fpath    = req.get("path", "").strip().strip('"\'')
                provider = req.get("provider", "")
                api_key  = req.get("api_key", "")
                language = req.get("language", "ru")
                model    = req.get("model", "")
                diarize  = bool(req.get("diarize", False))

                if not fpath or not os.path.exists(fpath):
                    self.send_error_json(f"Файл не найден: {fpath}", 400)
                    return
                if not api_key:
                    self.send_error_json("API ключ не передан", 400)
                    return

                ext       = os.path.splitext(fpath)[1].lstrip(".").lower()
                file_size = os.path.getsize(fpath)
                size_mb   = file_size / (1024 * 1024)
                print(f"  [transcribe-by-path/{provider}] {os.path.basename(fpath)} "
                      f"({size_mb:.0f}MB) lang={language} model={model}")

                VIDEO_EXTS_PY = {"mp4","webm","mkv","avi","mov","m4v","3gp","flv","wmv","ts"}
                if ext in VIDEO_EXTS_PY:
                    # ffmpeg читает прямо с диска — исходный файл НЕ грузим в RAM
                    print(f"  [transcribe-by-path/{provider}] ffmpeg {ext}→mp3 (file→file)...")
                    audio_bytes = extract_audio_from_path(fpath)
                    audio_mime  = "audio/mpeg"
                    print(f"  [transcribe-by-path/{provider}] конвертация OK, "
                          f"{len(audio_bytes)//1024}KB")
                else:
                    # Аудио-файл: читаем в RAM только его (уже небольшое)
                    with open(fpath, "rb") as f:
                        audio_bytes = f.read()
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

                print(f"  [transcribe-by-path/{provider}] Готово! "
                      f"Длительность: {result['metadata']['duration']}s")
                self.send_json(result)
            except Exception as e:
                print(f"  [transcribe-by-path] ERROR: {e}")
                self.send_error_json(str(e))
            return

        # ── Транскрибация по пути — SSE-стриминг прогресса ──────────────
        if path == "/api/transcribe-by-path-sse":
            try:
                body = self._read_body()
                req  = json.loads(body)

                fpath    = req.get("path", "").strip().strip('"\'')
                provider = req.get("provider", "")
                api_key  = req.get("api_key", "")
                language = req.get("language", "ru")
                model    = req.get("model", "")
                diarize  = bool(req.get("diarize", False))

                if not fpath or not os.path.exists(fpath):
                    self.send_error_json(f"Файл не найден: {fpath}", 400)
                    return
                if not api_key:
                    self.send_error_json("API ключ не передан", 400)
                    return

                # Настраиваем SSE-ответ
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                def sse(event, data):
                    msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    try:
                        self.wfile.write(msg.encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        pass

                ext       = os.path.splitext(fpath)[1].lstrip(".").lower()
                file_size = os.path.getsize(fpath)
                size_mb   = file_size / (1024 * 1024)
                fname     = os.path.basename(fpath)

                sse("progress", {"step": "start", "pct": 5,
                                 "text": f"📂 Файл найден: {fname} ({size_mb:.0f} MB)"})

                VIDEO_EXTS_PY = {"mp4","webm","mkv","avi","mov","m4v","3gp","flv","wmv","ts"}
                if ext in VIDEO_EXTS_PY:
                    sse("progress", {"step": "ffmpeg", "pct": 15,
                                     "text": f"🎬 Извлечение аудио ffmpeg ({size_mb:.0f} MB → ~50 MB)..."})
                    audio_bytes = extract_audio_from_path(fpath)
                    audio_mime  = "audio/mpeg"
                    mp3_mb = len(audio_bytes) / (1024 * 1024)
                    sse("progress", {"step": "ffmpeg_done", "pct": 40,
                                     "text": f"✅ Аудио извлечено: {mp3_mb:.1f} MB"})
                else:
                    sse("progress", {"step": "read", "pct": 20,
                                     "text": f"📖 Читаем аудио-файл ({size_mb:.1f} MB)..."})
                    with open(fpath, "rb") as f:
                        audio_bytes = f.read()
                    audio_mime = "audio/mpeg"
                    sse("progress", {"step": "read_done", "pct": 40,
                                     "text": f"✅ Файл прочитан"})

                sse("progress", {"step": "upload", "pct": 45,
                                 "text": f"📤 Загружаем на {provider.capitalize()}..."})

                if provider == "deepgram":
                    result = transcribe_deepgram(api_key, audio_bytes, audio_mime, language, model, diarize)
                elif provider == "assemblyai":
                    result = transcribe_assemblyai(api_key, audio_bytes, language, model, diarize)
                elif provider == "gladia":
                    result = transcribe_gladia(api_key, audio_bytes, language, diarize)
                else:
                    sse("error", {"error": f"Неизвестный провайдер: {provider}"})
                    return

                sse("progress", {"step": "done", "pct": 100, "text": "✅ Готово!"})
                sse("result", result)

            except Exception as e:
                try:
                    sse("error", {"error": str(e)})
                except Exception:
                    pass
            return

        # ── Извлечение аудио (legacy endpoint) ───────────────────────────
        if path == "/api/extract-audio":
            ext = self.headers.get("X-File-Extension", "mp4").lstrip(".")
            try:
                body = self._read_body()
                mp3_bytes = extract_audio(body, ext)
                print(f"  [extract-audio] OK, {len(mp3_bytes)//1024} KB")
                self.send_bytes(mp3_bytes, "audio/mpeg")
            except Exception as e:
                print(f"  [extract-audio] ERROR: {e}")
                self.send_error_json(str(e))
            return

        # ── Транскрибация ────────────────────────────────────────────────
        if path.startswith("/api/transcribe/"):
            provider = path.replace("/api/transcribe/", "").strip("/")
            api_key  = self.headers.get("X-Api-Key", "")
            language = self.headers.get("X-Language", "ru")
            model    = self.headers.get("X-Model", "")
            diarize  = self.headers.get("X-Diarize", "false").lower() in ("true", "1", "yes")
            mime     = self.headers.get("Content-Type", "audio/mpeg")
            is_video = self.headers.get("X-Is-Video", "false").lower() in ("true", "1", "yes")
            file_ext = self.headers.get("X-File-Extension", "mp4").lstrip(".")

            if not api_key:
                self.send_error_json("API ключ не передан (заголовок X-Api-Key)", 400)
                return
            if content_length <= 0:
                self.send_error_json("Аудиоданные не переданы", 400)
                return

            size_str = f"{content_length // (1024*1024)}MB" if content_length > 1048576 else f"{content_length//1024}KB"
            print(f"  [transcribe/{provider}] lang={language} model={model} diarize={diarize} "
                  f"size={size_str} is_video={is_video} large={large_file}")

            tmpdir_obj = None
            try:
                audio_bytes = None
                audio_mime  = mime

                if large_file and is_video:
                    # ── Большой видеофайл: стримим на диск → ffmpeg с файла ──
                    import tempfile as _tf
                    tmpdir_obj = _tf.TemporaryDirectory()
                    tmp_input  = os.path.join(tmpdir_obj.name, f"input.{file_ext}")
                    tmp_output = os.path.join(tmpdir_obj.name, "output.mp3")

                    print(f"  [transcribe/{provider}] Стримим {size_str} на диск...")
                    written = self._stream_body_to_file(tmp_input)
                    print(f"  [transcribe/{provider}] Записано {written//1024//1024}MB, запускаем ffmpeg...")

                    ffmpeg_cmd = find_ffmpeg()
                    if not ffmpeg_cmd:
                        raise RuntimeError("ffmpeg не найден")
                    cmd = [
                        ffmpeg_cmd, "-y", "-i", tmp_input,
                        "-vn", "-acodec", "libmp3lame",
                        "-ac", "1", "-ar", "16000", "-b:a", "64k",
                        tmp_output,
                    ]
                    proc = subprocess.run(cmd, capture_output=True, timeout=600)
                    if proc.returncode != 0:
                        err = proc.stderr.decode("utf-8", errors="replace")
                        raise RuntimeError(f"ffmpeg ошибка: {err[-1000:]}")

                    with open(tmp_output, "rb") as f:
                        audio_bytes = f.read()
                    audio_mime = "audio/mpeg"
                    print(f"  [transcribe/{provider}] ffmpeg OK → {len(audio_bytes)//1024}KB MP3")

                else:
                    # ── Обычный путь: читаем в RAM ───────────────────────────
                    body = self._read_body()
                    if not body:
                        self.send_error_json("Аудиоданные не переданы", 400)
                        return
                    if is_video:
                        print(f"  [transcribe/{provider}] Конвертируем {file_ext}→mp3...")
                        audio_bytes = extract_audio(body, file_ext)
                        audio_mime  = "audio/mpeg"
                        print(f"  [transcribe/{provider}] Конвертация OK, {len(audio_bytes)//1024}KB")
                    else:
                        audio_bytes = body

                if provider == "deepgram":
                    result = transcribe_deepgram(api_key, audio_bytes, audio_mime, language, model, diarize)
                elif provider == "assemblyai":
                    result = transcribe_assemblyai(api_key, audio_bytes, language, model, diarize)
                elif provider == "gladia":
                    result = transcribe_gladia(api_key, audio_bytes, language, diarize)
                else:
                    self.send_error_json(f"Неизвестный провайдер: {provider}", 400)
                    return

                print(f"  [transcribe/{provider}] Готово! Длительность: {result['metadata']['duration']}s")
                self.send_json(result)

            except Exception as e:
                print(f"  [transcribe/{provider}] ERROR: {e}")
                self.send_error_json(str(e))
            finally:
                if tmpdir_obj:
                    tmpdir_obj.cleanup()
            return

        self.send_error(404, "Not found")


# ─────────────────────────────────────────────────────────────────────────────
# Точка входа
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  VoxCraft — Мульти-провайдерный аудио-транскрибер")
    print("=" * 60)

    # Проверка ffmpeg
    ffmpeg_path = shutil.which("ffmpeg")
    local_ffmpeg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe")
    if ffmpeg_path:
        print(f"  ✓ ffmpeg найден: {ffmpeg_path}")
    elif os.path.exists(local_ffmpeg):
        print(f"  ✓ ffmpeg найден локально: {local_ffmpeg}")
    else:
        print("  ⚠ ffmpeg не найден — конвертация видео недоступна")
        print("    Скачайте: https://ffmpeg.org/download.html")

    # Многопоточный сервер — каждый запрос в своём потоке
    class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True  # потоки завершаются при выходе
        allow_reuse_address = True

    server = ThreadedHTTPServer(("0.0.0.0", PORT), VoxCraftHandler)
    print(f"\n  🚀 Сервер запущен: http://127.0.0.1:{PORT}")
    print(f"  Открываю браузер...\n")
    print("  Для остановки нажмите Ctrl+C\n")

    # Открываем браузер (опционально)
    try:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Сервер остановлен.")


if __name__ == "__main__":
    main()
