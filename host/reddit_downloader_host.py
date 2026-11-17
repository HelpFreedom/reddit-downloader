#!/usr/bin/env python3
"""Native messaging host для расширения Reddit Downloader.

Chrome запускает этот скрипт и общается с ним через stdin/stdout
(каждое сообщение — 4 байта длины в порядке байтов машины + JSON в UTF-8).
В stdout нельзя писать ничего, кроме сообщений протокола.

Запрос:  {"type": "download", "job": {...}}  или  {"type": "ping"}
Ответы:  {"type": "progress", "stage": str, "percent": float|None}
         {"type": "done", "path": str, "warnings": [str]}
         {"type": "error", "message": str}
         {"type": "pong", "version": str, "ffmpeg": bool, "downloads": str}

Видео Reddit берётся целыми файлами из DASH-манифеста (картинка + звук), а не через HLS:
ffmpeg разбирает HLS Reddit (byte-range CMAF) только до конца первого сегмента — 4 с — и
при этом выходит с кодом 0. Поэтому после любого способа сверяется длительность результата.

Правила нагрузки: ffmpeg всегда строго по одному (глобальная блокировка на файл),
nice -n 19, -threads 4.
"""

import fcntl
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

VERSION = "1.0.0"
CACHE_DIR = os.path.join(os.path.expanduser("~/.cache"), "reddit-downloader")
LOCK_PATH = os.path.join(CACHE_DIR, "ffmpeg.lock")
LOG_PATH = os.path.join(CACHE_DIR, "host.log")
NICE = ["nice", "-n", "19"]
THREADS = "4"
NET_TIMEOUT = 30  # секунд без данных — считаем, что сеть зависла
MAX_NAME_BYTES = 180  # имя файла в байтах UTF-8 (предел ФС — 255)
ALLOWED_HOST_SUFFIXES = (
    "redd.it", "reddit.com", "redditmedia.com", "redditstatic.com",
    "imgur.com", "giphy.com", "gfycat.com", "redgifs.com", "tenor.com",
)

_out_lock = threading.Lock()
_current_proc = None  # текущий ffmpeg — чтобы убить его при закрытии Chrome
_cleanup_paths = set()  # недокачанные файлы, удаляются при аварийном выходе
_workdir = None  # временная папка текущего задания


# ---------------------------------------------------------------- протокол

def send(msg):
    data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    with _out_lock:
        sys.stdout.buffer.write(struct.pack("=I", len(data)))
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (length,) = struct.unpack("=I", raw)
    if length > 16 * 1024 * 1024:
        raise ValueError("слишком большое сообщение")
    data = sys.stdin.buffer.read(length)
    if len(data) < length:
        return None
    return json.loads(data.decode("utf-8"))


def log(text):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2 * 1024 * 1024:
            os.replace(LOG_PATH, LOG_PATH + ".old")  # простая ротация
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text.rstrip() + "\n")
    except OSError:
        pass


class JobError(Exception):
    pass


# ---------------------------------------------------------------- пути и имена

def downloads_dir():
    override = os.environ.get("RD_TEST_DOWNLOAD_DIR")  # только для тестов
    if override:
        return override
    try:
        out = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        if out and os.path.isdir(out) and os.path.realpath(out) != os.path.realpath(os.path.expanduser("~")):
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    path = os.path.expanduser("~/Downloads")
    os.makedirs(path, exist_ok=True)
    return path


def safe_name(name):
    name = unicodedata.normalize("NFC", str(name))
    name = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in name)
    name = re.sub(r'[/\\:*?"<>|]', "-", name)
    name = re.sub(r"\s+", " ", name).strip().lstrip(".").strip()
    # обрезка по байтам, не разрывая символы; «[id]» в конце стараемся сохранить
    tail = ""
    m = re.search(r"( \[[a-z0-9]+\](?: \d+)?)$", name)
    if m:
        tail, name = m.group(1), name[: m.start()]
    budget = MAX_NAME_BYTES - len(tail.encode("utf-8"))
    enc = name.encode("utf-8")
    if len(enc) > budget:
        name = enc[:budget].decode("utf-8", "ignore").rstrip(" .-") + "…"
    name = (name + tail).strip(" .")
    return name or "reddit"


def unique_path(directory, base, ext):
    path = os.path.join(directory, base + ext)
    n = 2
    while os.path.exists(path):
        path = os.path.join(directory, f"{base} ({n}){ext}")
        n += 1
    return path


def check_url(url):
    u = urllib.parse.urlparse(url)
    if os.environ.get("RD_TEST_ALLOW_LOCAL") == "1" and u.scheme == "http" and u.hostname == "127.0.0.1":
        return  # только для тестов (tests/test_host.py)
    if u.scheme != "https":
        raise JobError(f"разрешены только https-ссылки: {url}")
    host = (u.hostname or "").lower()
    if not any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES):
        raise JobError(f"неизвестный сайт: {host}")


# ---------------------------------------------------------------- ffmpeg

def run_ffmpeg(args, duration, stage):
    """Запускает ffmpeg с прогрессом. args — всё после 'ffmpeg'."""
    global _current_proc
    cmd = NICE + ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                  "-progress", "pipe:1", "-nostats"] + args
    log("RUN " + " ".join(cmd))
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err,
                                stdin=subprocess.DEVNULL, text=True,
                                start_new_session=True)
        _current_proc = proc
        last_sent = 0.0
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and duration > 0:
                    try:
                        sec = int(value) / 1e6
                    except ValueError:
                        continue
                    now = time.monotonic()
                    if now - last_sent > 0.5:
                        last_sent = now
                        send({"type": "progress", "stage": stage,
                              "percent": max(0.0, min(99.0, sec / duration * 100))})
            proc.wait()
        finally:
            _current_proc = None
            if proc.poll() is None:
                kill_proc(proc)
        err.seek(0)
        tail = err.read()[-1500:].strip()
    if proc.returncode != 0:
        log(f"ffmpeg exit {proc.returncode}: {tail}")
        raise JobError(f"ffmpeg завершился с ошибкой ({proc.returncode}): {tail[-400:] or 'без подробностей'}")


def kill_proc(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    except OSError:
        pass


def probe(path):
    try:
        out = subprocess.run(
            NICE + ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise JobError("ffprobe не ответил за 60 с")
    if out.returncode != 0:
        raise JobError("ffprobe не смог прочитать файл: " + out.stderr.strip()[-300:])
    return json.loads(out.stdout or "{}")


def streams_of(info, kind):
    return [s for s in info.get("streams", []) if s.get("codec_type") == kind]


def http_input_args(meta):
    ua = meta.get("userAgent") or "Mozilla/5.0"
    # protocol_whitelist: ffmpeg ходит только по сети и не откроет локальный файл,
    # даже если манифест сошлётся на file:// (http оставлен для локальных тестов).
    return ["-user_agent", ua, "-headers", "Referer: https://www.reddit.com/\r\n",
            "-rw_timeout", str(NET_TIMEOUT * 1_000_000),
            "-protocol_whitelist", "https,http,tls,tcp,crypto"]


# ---------------------------------------------------------------- скачивание

def req_headers(meta):
    return {
        "User-Agent": meta.get("userAgent") or "Mozilla/5.0",
        "Referer": "https://www.reddit.com/",
        "Accept": "image/gif,video/*,*/*;q=0.5",
    }


def http_get(url, meta, limit=4 * 1024 * 1024):
    """Небольшой текстовый ответ (манифест)."""
    req = urllib.request.Request(url, headers=req_headers(meta))
    try:
        with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp:
            check_url(resp.geturl())
            return resp.read(limit)
    except urllib.error.HTTPError as e:
        raise JobError(f"HTTP {e.code} при загрузке {url}")
    except (urllib.error.URLError, OSError) as e:
        raise JobError(f"сеть: {getattr(e, 'reason', e)}")


def url_exists(url, meta):
    """HEAD-запрос: файл есть и это не веб-страница."""
    req = urllib.request.Request(url, method="HEAD", headers=req_headers(meta))
    try:
        with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp:
            check_url(resp.geturl())
            return not (resp.headers.get("Content-Type") or "").lower().startswith("text/html")
    except (urllib.error.URLError, OSError, JobError):
        return False


def download_file(url, dest, meta):
    req = urllib.request.Request(url, headers=req_headers(meta))
    try:
        resp = urllib.request.urlopen(req, timeout=NET_TIMEOUT)
    except urllib.error.HTTPError as e:
        raise JobError(f"HTTP {e.code} при загрузке {url}")
    except (urllib.error.URLError, OSError) as e:
        raise JobError(f"сеть: {getattr(e, 'reason', e)}")
    with resp:
        check_url(resp.geturl())  # редирект не должен увести на чужой сайт
        total = int(resp.headers.get("Content-Length") or 0)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype.startswith("text/html"):
            raise JobError(f"вместо медиа пришла веб-страница: {url}")
        done = 0
        last_sent = 0.0
        with open(dest, "wb") as f:
            while True:
                try:
                    chunk = resp.read(256 * 1024)
                except (OSError, TimeoutError) as e:
                    raise JobError(f"обрыв загрузки: {e}")
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last_sent > 0.5:
                    last_sent = now
                    send({"type": "progress", "stage": "загрузка",
                          "percent": (done / total * 100) if total else None})
    if total and done < total:
        raise JobError(f"файл скачан не полностью ({done} из {total} байт)")
    if done == 0:
        raise JobError("пустой файл")


def from_manifest(url, out_part, meta):
    # Без -map: стандартный выбор ffmpeg берёт видео с наибольшим разрешением
    # и аудио с наибольшим числом каналов из всех вариантов плейлиста.
    duration = float(meta.get("duration") or 0)
    run_ffmpeg(http_input_args(meta) + [
        "-i", url, "-c", "copy", "-threads", THREADS,
        "-movflags", "+faststart", "-f", "mp4", out_part,
    ], duration, "склейка видео и звука")
    return duration


# Имена звуковой дорожки у Reddit в разные годы — на случай, если манифест недоступен.
AUDIO_GUESSES = ("CMAF_AUDIO_128.mp4", "CMAF_AUDIO_64.mp4", "DASH_AUDIO_128.mp4",
                 "DASH_AUDIO_64.mp4", "DASH_audio.mp4", "audio")


def parse_iso_duration(text):
    m = re.fullmatch(r"P(?:(\d+(?:\.\d+)?)D)?T?(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?",
                     (text or "").strip())
    if not m:
        return 0.0
    d, h, mi, sec = (float(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


def parse_mpd(data, mpd_url):
    """→ (видео [(высота, битрейт, url)], аудио [(битрейт, url)], длительность), лучшие первыми."""
    root = ET.fromstring(data)
    local = lambda el: el.tag.rsplit("}", 1)[-1]
    videos, audios = [], []
    for aset in root.iter():
        if local(aset) != "AdaptationSet":
            continue
        set_kind = (aset.get("contentType") or (aset.get("mimeType") or "").split("/")[0]).lower()
        for rep in aset:
            if local(rep) != "Representation":
                continue
            base = next((c.text for c in rep if local(c) == "BaseURL" and c.text), None)
            if not base:
                continue
            kind = set_kind or (rep.get("mimeType") or "").split("/")[0].lower()
            if not kind:
                kind = "audio" if (rep.get("codecs") or "").startswith("mp4a") else "video"
            url = urllib.parse.urljoin(mpd_url, base.strip())
            bw = int(rep.get("bandwidth") or 0)
            if kind == "video":
                videos.append((int(rep.get("height") or 0), bw, url))
            elif kind == "audio":
                audios.append((bw, url))
    videos.sort(reverse=True)
    audios.sort(reverse=True)
    return videos, audios, parse_iso_duration(root.get("mediaPresentationDuration"))


def from_reddit(c, out_part, meta):
    """Видео Reddit: лучшая картинка и лучший звук целыми файлами → склейка без перекодирования."""
    mpd_url, hint = c.get("url"), c.get("video")
    if mpd_url and not urllib.parse.urlparse(mpd_url).path.lower().endswith(".mpd"):
        hint, mpd_url = hint or mpd_url, None  # манифеста нет — в url сразу файл видео
    videos, audios, expected = [], [], 0.0
    if mpd_url:
        try:
            videos, audios, expected = parse_mpd(http_get(mpd_url, meta), mpd_url)
        except (JobError, ET.ParseError) as e:
            log(f"манифест не прочитан: {e}")
    # fallback_url из JSON поста обычно лучше всего, что есть в манифесте (там бывает до 720p)
    video_urls, seen = [], set()
    for u in ([hint] if hint else []) + [v[2] for v in videos]:
        key = urllib.parse.urlparse(u).path
        if key not in seen:
            seen.add(key)
            video_urls.append(u)
    send({"type": "progress", "stage": "поиск лучшего качества", "percent": None})
    video = next((u for u in video_urls if url_exists(u, meta)), None)
    if not video:
        raise JobError("не нашлось файла с картинкой")
    audio = None
    if meta.get("hasAudio") is not False:
        audio_urls = [a[1] for a in audios]
        if not audio_urls:
            audio_urls = [urllib.parse.urljoin(video, n) for n in AUDIO_GUESSES]
        audio = next((u for u in audio_urls if url_exists(u, meta)), None)
    log(f"reddit: видео {video} | звук {audio} | длительность {expected}")
    expected = expected or float(meta.get("duration") or 0)
    args = http_input_args(meta) + ["-i", video]
    maps = ["-map", "0:v:0"]
    if audio:
        args += http_input_args(meta) + ["-i", audio]
        maps += ["-map", "1:a:0"]
    run_ffmpeg(args + maps + ["-c", "copy", "-threads", THREADS,
                              "-movflags", "+faststart", "-f", "mp4", out_part],
               expected, "склейка видео и звука" if audio else "загрузка видео")
    return expected


def from_file(url, out_part, meta, workdir):
    src = os.path.join(workdir, "source")
    _cleanup_paths.add(src)
    download_file(url, src, meta)
    info = probe(src)
    video = streams_of(info, "video")
    if not video:
        raise JobError("в файле нет видео")
    codec = video[0].get("codec_name")
    duration = float(info.get("format", {}).get("duration") or 0)
    if codec in ("png", "mjpeg", "webp", "bmp"):
        raise JobError(f"это картинка ({codec}), а не анимация")
    if codec == "gif":
        # gif → mp4: H.264, yuv420p (иначе не играется в браузерах/телефонах),
        # чётные размеры, исходные задержки кадров (vfr), без дублирования кадров.
        run_ffmpeg([
            "-i", src,
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=lanczos,format=yuv420p",
            "-fps_mode", "vfr",
            "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-tune", "animation",
            "-threads", THREADS, "-an",
            "-movflags", "+faststart", "-f", "mp4", out_part,
        ], duration, "перекодирование gif → mp4")
    else:
        run_ffmpeg([
            "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-threads", THREADS,
            "-movflags", "+faststart", "-f", "mp4", out_part,
        ], duration, "упаковка в mp4")
    os.remove(src)
    _cleanup_paths.discard(src)
    return duration


def do_job(job):
    candidates = job.get("candidates") or []
    if not candidates:
        raise JobError("нет ссылок на медиа")
    for c in candidates:
        check_url(c.get("url", ""))
        if c.get("video"):
            check_url(c["video"])
    want_audio = job.get("hasAudio")  # True / False / None (неизвестно)
    out_dir = downloads_dir()
    os.makedirs(CACHE_DIR, exist_ok=True)

    send({"type": "progress", "stage": "ожидание очереди ffmpeg", "percent": None})
    with open(LOCK_PATH, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # строго один ffmpeg за раз
        global _workdir
        workdir = _workdir = tempfile.mkdtemp(prefix="job-", dir=CACHE_DIR)
        best_silent = None  # результат без звука — на случай, если со звуком не выйдет
        errors = []
        try:
            for i, c in enumerate(candidates):
                part = os.path.join(workdir, f"out{i}.mp4")
                _cleanup_paths.add(part)
                try:
                    kind = c.get("type")
                    if kind == "reddit":
                        expected = from_reddit(c, part, job)
                    elif kind == "manifest":
                        expected = from_manifest(c["url"], part, job)
                    else:
                        expected = from_file(c["url"], part, job, workdir)
                    info = probe(part)
                    if not streams_of(info, "video"):
                        raise JobError("в результате нет видео")
                    # ffmpeg может «успешно» сохранить только начало (так было с HLS Reddit)
                    got = float(info.get("format", {}).get("duration") or 0)
                    if expected > 1 and got < expected * 0.95 - 0.5:
                        raise JobError(f"видео обрезано: {got:.1f} с из {expected:.1f} с")
                    if want_audio and not streams_of(info, "audio"):
                        log(f"кандидат {i} без звука: {c['url']}")
                        if best_silent is None:
                            best_silent = part
                        else:
                            os.remove(part)
                        errors.append(f"#{i + 1}: нет звука")
                        continue
                    return finish(part, out_dir, job, [], best_silent)
                except JobError as e:
                    log(f"кандидат {i} ошибка: {e}")
                    errors.append(f"#{i + 1}: {e}")
                    if os.path.exists(part) and part != best_silent:
                        os.remove(part)
            if best_silent:
                return finish(best_silent, out_dir, job,
                              ["Звук получить не удалось — сохранено видео без звука."], None)
            raise JobError("; ".join(errors))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            _cleanup_paths.clear()


def finish(part, out_dir, job, warnings, drop):
    if drop and drop != part and os.path.exists(drop):
        os.remove(drop)
    final = unique_path(out_dir, safe_name(job.get("baseName") or "reddit"), ".mp4")
    shutil.move(part, final)
    log(f"DONE {final}")
    return {"type": "done", "path": final, "warnings": warnings}


# ---------------------------------------------------------------- main

def emergency_exit(*_):
    proc = _current_proc
    if proc is not None and proc.poll() is None:
        kill_proc(proc)
    for p in list(_cleanup_paths):
        try:
            os.remove(p)
        except OSError:
            pass
    if _workdir:
        shutil.rmtree(_workdir, ignore_errors=True)
    os._exit(0)


def watch_stdin_eof():
    """Пока идёт задание, Chrome может закрыть порт: тогда stdin даёт EOF — убиваем ffmpeg."""
    try:
        while sys.stdin.buffer.read(1):
            pass
    except OSError:
        pass
    emergency_exit()


def main():
    signal.signal(signal.SIGTERM, emergency_exit)
    signal.signal(signal.SIGINT, emergency_exit)
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        msg = read_message()
    except (ValueError, json.JSONDecodeError) as e:
        send({"type": "error", "message": f"плохое сообщение: {e}"})
        return
    if msg is None:
        return
    if msg.get("type") == "ping":
        send({"type": "pong", "version": VERSION,
              "ffmpeg": shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None,
              "downloads": downloads_dir()})
        return
    if msg.get("type") != "download":
        send({"type": "error", "message": "неизвестная команда"})
        return
    threading.Thread(target=watch_stdin_eof, daemon=True).start()
    job = msg.get("job") or {}
    log("JOB " + json.dumps(job, ensure_ascii=False))
    try:
        send(do_job(job))
    except JobError as e:
        send({"type": "error", "message": str(e)})
    except Exception as e:  # noqa: BLE001 — любую неожиданность отдаём в расширение
        log(f"CRASH {e!r}")
        send({"type": "error", "message": f"внутренняя ошибка помощника: {e!r}"})
    # Поток watch_stdin_eof всё ещё ждёт stdin; обычное завершение интерпретатора
    # с таким потоком аварийно падает (_enter_buffered_busy). Ответ уже отправлен.
    os._exit(0)


if __name__ == "__main__":
    main()
