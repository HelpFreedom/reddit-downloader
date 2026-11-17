#!/usr/bin/env python3
"""Сквозная проверка помощника: локальный HTTP-сервер с медиа «как у Reddit» →
сообщение native messaging → готовый mp4. Запуск: python3 -I tests/test_host.py

ffmpeg для подготовки фикстур запускается строго по одному, nice -n 19, -threads 4.
"""
import functools
import http.server
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.path.join(ROOT, "host", "reddit_downloader_host.py")
FF = ["nice", "-n", "19", "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-threads", "4"]


def ff(*args, cwd=None):
    subprocess.run(FF + list(args), check=True, cwd=cwd)


def make_fixtures(d):
    src = os.path.join(d, "src.mp4")
    # 4 с: картинка 1280x720 + стерео-тон; ключевой кадр каждую секунду
    ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=4",
       "-f", "lavfi", "-i", "sine=frequency=440:duration=4:sample_rate=48000",
       "-ac", "2", "-c:v", "libx264", "-preset", "veryfast", "-g", "30", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "128k", "-shortest", src)
    # HLS как у Reddit: два видеоварианта + отдельная звуковая дорожка, сегменты fmp4
    hls = os.path.join(d, "hls")
    os.makedirs(hls)
    ff("-i", src, "-filter_complex", "[0:v]split=2[a][b];[b]scale=640:360[b2]",
       "-map", "[a]", "-map", "[b2]", "-map", "0:a",
       "-c:v", "libx264", "-preset", "veryfast", "-g", "30", "-c:a", "copy",
       "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4",
       "-var_stream_map", "v:0,agroup:aud v:1,agroup:aud a:0,agroup:aud",
       "-master_pl_name", "HLSPlaylist.m3u8", "-hls_segment_filename", "s_%v_%03d.m4s", "v%v.m3u8",
       cwd=hls)
    # DASH
    dash = os.path.join(d, "dash")
    os.makedirs(dash)
    ff("-i", src, "-map", "0:v", "-map", "0:a", "-c", "copy", "-f", "dash", "-seg_duration", "1",
       "DASHPlaylist.mpd", cwd=dash)
    # только картинка (как fallback_url)
    ff("-i", src, "-map", "0:v", "-c", "copy", os.path.join(d, "CMAF_720.mp4"))
    # gif с нечётными размерами и неровными задержками кадров
    ff("-f", "lavfi", "-i", "testsrc=size=201x151:rate=10:duration=2",
       "-vf", "split[a][b];[a]palettegen[p];[b][p]paletteuse", os.path.join(d, "anim.gif"))
    with open(os.path.join(d, "page.gif"), "w") as f:  # «gif», который на самом деле HTML
        f.write("<html>blocked</html>")
    make_reddit_fixtures(d)


FRAG = ["-movflags", "frag_keyframe+empty_moov+default_base_moof"]


def make_reddit_fixtures(d):
    """Как v.redd.it: отдельные фрагментированные CMAF-файлы картинки и звука,
    DASH-манифест с потолком 720p (а файл 1080p при этом есть), HLS одним файлом по byte-range."""
    r = os.path.join(d, "reddit")
    os.makedirs(r)
    src = os.path.join(d, "src10.mp4")
    ff("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=10",
       "-f", "lavfi", "-i", "sine=frequency=330:duration=10:sample_rate=48000",
       "-ac", "2", "-c:v", "libx264", "-preset", "veryfast", "-g", "30", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "128k", "-shortest", src)
    ff("-i", src, "-map", "0:v", "-c", "copy", *FRAG, os.path.join(r, "CMAF_1080.mp4"))
    ff("-i", src, "-map", "0:v", "-vf", "scale=1280:720", "-c:v", "libx264", "-preset", "veryfast", "-g", "30",
       *FRAG, os.path.join(r, "CMAF_720.mp4"))
    ff("-i", src, "-map", "0:a", "-c", "copy", *FRAG, os.path.join(r, "CMAF_AUDIO_128.mp4"))
    ff("-i", src, "-map", "0:a", "-c:a", "aac", "-b:a", "64k", *FRAG, os.path.join(r, "CMAF_AUDIO_64.mp4"))
    rep = lambda rid, extra, base: (f'<Representation id="{rid}" {extra}><BaseURL>{base}</BaseURL>'
                                    f'<SegmentBase indexRange="0-0"><Initialization range="0-0"/></SegmentBase></Representation>')
    mpd = ('<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT10.000000S" type="static">'
           '<Period><AdaptationSet contentType="video" maxHeight="1080">'
           + rep(1, 'bandwidth="800000" height="360" width="640" mimeType="video/mp4"', "CMAF_360.mp4")
           + rep(2, 'bandwidth="2600000" height="720" width="1280" mimeType="video/mp4"', "CMAF_720.mp4")
           + '</AdaptationSet><AdaptationSet contentType="audio">'
           + rep(3, 'bandwidth="68000" mimeType="audio/mp4"', "CMAF_AUDIO_64.mp4")
           + rep(4, 'bandwidth="136000" mimeType="audio/mp4"', "CMAF_AUDIO_128.mp4")
           + '</AdaptationSet></Period></MPD>')
    with open(os.path.join(r, "DASHPlaylist.mpd"), "w") as f:
        f.write(mpd)
    # HLS одним файлом на дорожку (byte-range), сегменты по 4 с — как у Reddit
    h = os.path.join(d, "hlsrange")
    os.makedirs(h)
    ff("-i", src, "-map", "0:v", "-map", "0:a", "-c", "copy",
       "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4",
       "-hls_flags", "single_file", "-var_stream_map", "v:0,agroup:aud a:0,agroup:aud",
       "-master_pl_name", "HLSPlaylist.m3u8", "-hls_segment_filename", "CMAF_%v.mp4", "p%v.m3u8", cwd=h)


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".m3u8": "application/vnd.apple.mpegurl", ".mpd": "application/dash+xml",
                      ".m4s": "video/iso.segment", ".gif": "image/gif"}

    def guess_type(self, path):
        if path.endswith("page.gif"):
            return "text/html"
        return super().guess_type(path)

    def log_message(self, format, *args):  # noqa: A002 — тихий сервер
        pass


def call_host(job, env):
    """Как Chrome: stdin открыт, пока не пришёл финальный ответ (EOF = порт закрыт = отмена)."""
    data = json.dumps({"type": "download", "job": job}).encode()
    p = subprocess.Popen([sys.executable, "-I", HOST, "chrome-extension://test/"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    p.stdin.write(struct.pack("=I", len(data)) + data)
    p.stdin.flush()
    msgs = []
    while True:
        head = p.stdout.read(4)
        if len(head) < 4:
            break
        (n,) = struct.unpack("=I", head)
        msgs.append(json.loads(p.stdout.read(n)))
        if msgs[-1]["type"] in ("done", "error"):
            break
    p.stdin.close()
    p.wait(timeout=30)
    return msgs


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def main():
    tmp = tempfile.mkdtemp(prefix="rd-test-")
    www, dl = os.path.join(tmp, "www"), os.path.join(tmp, "Downloads")
    os.makedirs(www); os.makedirs(dl)
    print("фикстуры…", flush=True)
    make_fixtures(www)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=www))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    env = {**os.environ, "RD_TEST_ALLOW_LOCAL": "1", "RD_TEST_DOWNLOAD_DIR": dl}
    failures = 0

    def check(name, job, expect):
        nonlocal failures
        msgs = call_host(job, env)
        last = msgs[-1] if msgs else {}
        try:
            expect(last, msgs)
            print("ok  ", name)
        except AssertionError as e:
            failures += 1
            print("FAIL", name, "→", e, json.dumps(last, ensure_ascii=False)[:600])

    def video_ok(w, h, audio=True, warn=False):
        def f(last, msgs):
            assert last.get("type") == "done", "нет done"
            info = probe(last["path"])
            v = [s for s in info["streams"] if s["codec_type"] == "video"]
            a = [s for s in info["streams"] if s["codec_type"] == "audio"]
            assert len(v) == 1 and (v[0]["width"], v[0]["height"]) == (w, h), f"видео {[(s['width'], s['height']) for s in v]}"
            assert bool(a) == audio, f"аудио: {len(a)}"
            if audio:
                assert a[0]["channels"] == 2, "не стерео"
            dur = float(info["format"]["duration"])
            assert 3.8 < dur < 4.3, f"длительность {dur}"
            assert bool(last["warnings"]) == warn, f"предупреждения {last['warnings']}"
            assert any(m["type"] == "progress" and isinstance(m.get("percent"), (int, float)) for m in msgs), "нет прогресса в %"
        return f

    vjob = lambda cands, **kw: {"baseName": kw.get("name", "Видео тест [abc123]"), "hasAudio": kw.get("audio", True),
                               "duration": 4, "userAgent": "test", "candidates": cands}

    check("HLS: лучшее качество 1280x720 + звук", vjob([{"type": "manifest", "url": base + "/hls/HLSPlaylist.m3u8"}]),
          video_ok(1280, 720))
    check("DASH", vjob([{"type": "manifest", "url": base + "/dash/DASHPlaylist.mpd"}], name="dash [abc123]"),
          video_ok(1280, 720))
    check("битый HLS → переход на DASH", vjob([{"type": "manifest", "url": base + "/nope/HLSPlaylist.m3u8"},
                                               {"type": "manifest", "url": base + "/dash/DASHPlaylist.mpd"}], name="fb [abc123]"),
          video_ok(1280, 720))
    check("только картинка, звук ожидался → сохранить с предупреждением",
          vjob([{"type": "file", "url": base + "/CMAF_720.mp4"}], name="silent [abc123]"), video_ok(1280, 720, audio=False, warn=True))
    check("без звука, но сначала, потом со звуком → берётся со звуком, лишний удалён",
          vjob([{"type": "file", "url": base + "/CMAF_720.mp4"}, {"type": "manifest", "url": base + "/hls/HLSPlaylist.m3u8"}],
               name="order [abc123]"), video_ok(1280, 720))

    def gif_ok(last, msgs):
        assert last.get("type") == "done", "нет done"
        info = probe(last["path"])
        v = info["streams"][0]
        assert len(info["streams"]) == 1, "лишние дорожки"
        assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p", f"{v['codec_name']} {v['pix_fmt']}"
        assert (v["width"], v["height"]) == (200, 150), f"{v['width']}x{v['height']}"
        dur = float(info["format"]["duration"])
        assert 1.8 < dur < 2.2, f"длительность {dur}"
        assert int(v.get("nb_frames", 0)) == 20, f"кадров {v.get('nb_frames')}"
        assert last["path"].endswith(".mp4")

    gjob = lambda url, name="Gif тест: a/b [xyz789]": {"baseName": name, "hasAudio": False, "duration": 0,
                                                       "userAgent": "test", "candidates": [{"type": "file", "url": url}]}
    check("gif → mp4 (h264, yuv420p, чётные размеры, все кадры)", gjob(base + "/anim.gif"), gif_ok)

    def err_contains(text):
        def f(last, msgs):
            assert last.get("type") == "error", "ждали error"
            assert text in last["message"], last["message"]
        return f

    check("HTML вместо gif → понятная ошибка", gjob(base + "/page.gif", "html [aaa111]"), err_contains("веб-страница"))
    check("404 → понятная ошибка", gjob(base + "/missing.gif", "404 [aaa111]"), err_contains("HTTP 404"))
    check("чужой сайт запрещён", gjob("https://evil.example.com/a.gif", "evil [aaa111]"), err_contains("неизвестный сайт"))
    check("http запрещён", gjob("http://i.redd.it/a.gif", "http [aaa111]"), err_contains("https"))

    # Повтор того же имени → «(2)», а не перезапись
    check("повтор имени → (2)", gjob(base + "/anim.gif"),
          lambda last, m: (assert_(last.get("type") == "done"), assert_(last["path"].endswith("[xyz789] (2).mp4"), last["path"])))
    # Длинный кириллический заголовок → обрезка по байтам с сохранением [id]
    long_name = "Очень длинный заголовок " * 20 + "[lng123]"
    check("длинное имя обрезано, [id] на месте", gjob(base + "/anim.gif", long_name),
          lambda last, m: (assert_(last.get("type") == "done"),
                           assert_(len(os.path.basename(last["path"]).encode()) <= 200, len(os.path.basename(last["path"]).encode())),
                           assert_(last["path"].endswith("… [lng123].mp4"), last["path"])))

    # ---- видео «как у Reddit» (10 с)
    def full_video(w, h, abr_min, audio=True):
        def f(last, msgs):
            assert last.get("type") == "done", "нет done"
            info = probe(last["path"])
            v = [s for s in info["streams"] if s["codec_type"] == "video"]
            a = [s for s in info["streams"] if s["codec_type"] == "audio"]
            assert (v[0]["width"], v[0]["height"]) == (w, h), f"видео {v[0]['width']}x{v[0]['height']}"
            dur = float(info["format"]["duration"])
            assert 9.8 < dur < 10.3, f"длительность {dur}"
            assert bool(a) == audio, f"аудио {len(a)}"
            if audio:
                assert int(a[0]["bit_rate"]) >= abr_min, f"звук {a[0]['bit_rate']} бит/с"
                assert 9.8 < float(a[0]["duration"]) < 10.3, f"звук {a[0]['duration']} с"
            assert not last["warnings"], last["warnings"]
        return f

    rjob = lambda cands, name, audio=True: {"baseName": name, "hasAudio": audio, "duration": 10,
                                            "userAgent": "test", "candidates": cands}
    rb = base + "/reddit/"
    check("Reddit: fallback 1080p лучше манифеста (720p) + лучший звук 128k, полные 10 с",
          rjob([{"type": "reddit", "url": rb + "DASHPlaylist.mpd?a=sig&v=1&f=sd", "video": rb + "CMAF_1080.mp4?source=fallback"}], "r1080 [rrr111]"),
          full_video(1920, 1080, 120_000))
    check("Reddit: без fallback → лучшее из манифеста (720p)",
          rjob([{"type": "reddit", "url": rb + "DASHPlaylist.mpd", "video": None}], "r720 [rrr222]"),
          full_video(1280, 720, 120_000))
    check("Reddit: манифест 404 → звук по стандартному имени",
          rjob([{"type": "reddit", "url": rb + "missing.mpd", "video": rb + "CMAF_1080.mp4?source=fallback"}], "rguess [rrr333]"),
          full_video(1920, 1080, 120_000))
    check("Reddit: видео-gif без звука (hasAudio=false)",
          rjob([{"type": "reddit", "url": rb + "DASHPlaylist.mpd", "video": rb + "CMAF_1080.mp4"}], "rgif [rrr444]", audio=False),
          full_video(1920, 1080, 0, audio=False))
    check("Reddit: нет манифеста, в url сразу mp4",
          rjob([{"type": "reddit", "url": rb + "CMAF_720.mp4", "video": None}], "rmp4 [rrr555]", audio=None),
          full_video(1280, 720, 120_000))

    # Регрессия: HLS одним файлом ffmpeg «успешно» обрезает до первого сегмента — это должно ловиться
    hls_only = call_host(rjob([{"type": "manifest", "url": base + "/hlsrange/HLSPlaylist.m3u8"}], "hlstrunc [hhh111]"), env)
    last = hls_only[-1]
    if last.get("type") == "done":
        d = float(probe(last["path"])["format"]["duration"])
        print("info  HLS byte-range локально скачан целиком:", d, "с (обрезка не воспроизвелась)")
        assert_(d > 9.8, d)
    else:
        print("ok   HLS byte-range обрезан → пойман:", last.get("message"))
    check("обрезанный HLS → переход на целые файлы",
          rjob([{"type": "manifest", "url": base + "/hlsrange/HLSPlaylist.m3u8"},
                {"type": "reddit", "url": rb + "DASHPlaylist.mpd", "video": rb + "CMAF_1080.mp4"}], "hlsfb [hhh222]"),
          full_video(1920, 1080, 120_000))

    files = sorted(os.listdir(dl))
    print("\nв папке загрузок:", *files, sep="\n  ")
    leftovers = [f for f in os.listdir(os.path.expanduser("~/.cache/reddit-downloader")) if f.startswith("job-")]
    if leftovers:
        failures += 1
        print("FAIL временные папки не удалены:", leftovers)
    if any(f.endswith((".part", "source")) or f.startswith("out") for f in files):
        failures += 1
        print("FAIL мусор в загрузках")
    if "/" in "".join(files) or not any("Gif тест- a-b" in f for f in files):
        failures += 1
        print("FAIL «/» и «:» в имени не заменены")
    ping = subprocess.run([sys.executable, "-I", HOST], input=struct.pack("=I", 15) + b'{"type":"ping"}',
                          capture_output=True, env=env).stdout
    print("ping:", ping[4:].decode())
    srv.shutdown()
    shutil.rmtree(tmp)
    print("\nИТОГ:", "всё прошло" if not failures else f"провалов: {failures}")
    sys.exit(1 if failures else 0)


def assert_(cond, msg=""):
    if not cond:
        raise AssertionError(msg)


if __name__ == "__main__":
    main()
