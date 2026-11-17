// Разбор JSON поста Reddit → список заданий на скачивание.
// Файл подключается в service worker через importScripts и в тестах через require.
//
// Задание (job):
//   { label, baseName, hasAudio, duration, candidates: [{ type, url }] }
//   type: "reddit"   — видео Reddit: url = DASH-манифест, video = fallback_url (часто лучше манифеста, до 1080p);
//                      помощник берёт лучшую картинку и лучший звук целыми файлами и склеивает без перекодирования;
//         "manifest" — HLS/DASH-плейлист целиком отдаётся ffmpeg (запасной путь: HLS Reddit ffmpeg обрезает до 4 с);
//         "file"     — прямой файл (gif / mp4 / webm); gif перекодируется в mp4, остальное перепаковывается в mp4.
//   Кандидаты перебираются по порядку, пока один не сработает.

(function (root) {
  "use strict";

  const POST_ID_RE = /\/(?:comments|gallery)\/([a-z0-9]{4,12})(?:[/?#]|$)/i;

  // ID поста из адреса вкладки. Возвращает null, если это не страница поста.
  function postIdFromUrl(href) {
    let u;
    try { u = new URL(href); } catch { return null; }
    const host = u.hostname.toLowerCase();
    if (host === "redd.it") {
      const m = u.pathname.match(/^\/([a-z0-9]{4,12})\/?$/i);
      return m ? m[1].toLowerCase() : null;
    }
    if (host !== "reddit.com" && !host.endsWith(".reddit.com")) return null;
    const m = u.pathname.match(POST_ID_RE);
    return m ? m[1].toLowerCase() : null;
  }

  // Короткая ссылка вида /r/<sub>/s/<code>: id появится только после редиректа.
  function isShareUrl(href) {
    try { return /^\/r\/[^/]+\/s\/[^/]+/i.test(new URL(href).pathname); } catch { return false; }
  }

  function str(v) { return typeof v === "string" && v.length > 0 ? v : null; }

  function pathExt(url) {
    try {
      const m = new URL(url).pathname.toLowerCase().match(/\.([a-z0-9]{2,5})$/);
      return m ? m[1] : "";
    } catch { return ""; }
  }

  function videoJob(rv, label) {
    const candidates = [];
    if (str(rv.dash_url) || str(rv.fallback_url)) {
      candidates.push({ type: "reddit", url: str(rv.dash_url) || rv.fallback_url, video: str(rv.fallback_url) });
    }
    if (str(rv.dash_url)) candidates.push({ type: "manifest", url: rv.dash_url });
    if (str(rv.hls_url)) candidates.push({ type: "manifest", url: rv.hls_url });
    // fallback_url — только картинка, без звука: последний вариант.
    if (str(rv.fallback_url)) candidates.push({ type: "file", url: rv.fallback_url });
    if (!candidates.length) return null;
    return {
      label,
      // is_gif: Reddit сам превратил gif в видео — звука там нет и не ждём.
      hasAudio: rv.is_gif ? false : rv.has_audio !== false,
      duration: Number(rv.duration) || 0,
      candidates,
    };
  }

  // Элемент media_metadata (галерея или gif/видео, вставленные в текст поста).
  function metadataJob(m, label) {
    if (!m || m.status !== "valid") return null;
    if (m.e === "RedditVideo") {
      const candidates = [];
      if (str(m.dashUrl)) candidates.push({ type: "reddit", url: m.dashUrl, video: null });
      if (str(m.dashUrl)) candidates.push({ type: "manifest", url: m.dashUrl });
      if (str(m.hlsUrl)) candidates.push({ type: "manifest", url: m.hlsUrl });
      if (!candidates.length) return null;
      return { label, hasAudio: m.isGif ? false : null, duration: 0, candidates };
    }
    if (m.e === "AnimatedImage") {
      const s = m.s || {};
      const candidates = [];
      // Оригинал gif лежит на i.redd.it/<id>.gif — лучшее качество для перекодирования.
      if (str(m.id) && (m.m === "image/gif" || str(s.gif))) {
        candidates.push({ type: "file", url: `https://i.redd.it/${m.id}.gif` });
      }
      if (str(s.gif)) candidates.push({ type: "file", url: s.gif });
      if (str(s.mp4)) candidates.push({ type: "file", url: s.mp4 });
      if (!candidates.length) return null;
      return { label, hasAudio: false, duration: 0, candidates };
    }
    return null; // обычные картинки не скачиваем
  }

  function uniqByUrl(cands) {
    const seen = new Set();
    return cands.filter((c) => (seen.has(c.url) ? false : (seen.add(c.url), true)));
  }

  // Медиа одного объекта поста (без учёта кросспоста).
  function mediaOf(p) {
    const jobs = [];

    // 1. Видео, загруженное на Reddit.
    const rv = p.secure_media?.reddit_video || p.media?.reddit_video;
    if (rv) {
      const j = videoJob(rv, "video");
      if (j) return [j];
    }

    // 2. Галерея / медиа внутри текста поста.
    const meta = p.media_metadata;
    if (meta && typeof meta === "object") {
      let ids;
      if (Array.isArray(p.gallery_data?.items)) {
        ids = p.gallery_data.items.map((it) => it.media_id).filter((id) => meta[id]);
      } else {
        ids = Object.keys(meta);
      }
      for (const id of ids) {
        const j = metadataJob(meta[id], id);
        if (j) jobs.push(j);
      }
      if (jobs.length) return jobs;
    }

    const url = str(p.url_overridden_by_dest) || str(p.url) || "";
    const ext = pathExt(url);
    const img = p.preview?.images?.[0];
    const mp4Variant = str(img?.variants?.mp4?.source?.url);
    const gifVariant = str(img?.variants?.gif?.source?.url);
    const previewVideo = p.preview?.reddit_video_preview;

    // 3. Ссылка прямо на gif (i.redd.it или внешний сайт).
    if (ext === "gif") {
      const c = [{ type: "file", url }];
      if (gifVariant) c.push({ type: "file", url: gifVariant });
      if (mp4Variant) c.push({ type: "file", url: mp4Variant });
      return [{ label: "gif", hasAudio: false, duration: 0, candidates: uniqByUrl(c) }];
    }

    // 4. Imgur .gifv — это mp4 под другим расширением.
    if (ext === "gifv") {
      const c = [{ type: "file", url: url.replace(/\.gifv(?=$|[?#])/i, ".mp4") }];
      if (previewVideo) c.push(...(videoJob(previewVideo, "gif")?.candidates || []));
      return [{ label: "gif", hasAudio: null, duration: 0, candidates: uniqByUrl(c) }];
    }

    // 5. Прямая ссылка на видеофайл.
    if (ext === "mp4" || ext === "webm" || ext === "mov") {
      return [{ label: "video", hasAudio: null, duration: 0, candidates: [{ type: "file", url }] }];
    }

    // 6. Reddit сделал своё видео-превью (обычно для внешних gif/видео) — без звука.
    if (previewVideo) {
      const j = videoJob(previewVideo, "video");
      if (j) { j.hasAudio = null; return [j]; }
    }

    // 7. mp4-вариант превью (анимированная картинка без отдельного оригинала).
    if (mp4Variant) {
      return [{ label: "gif", hasAudio: false, duration: 0, candidates: [{ type: "file", url: mp4Variant }] }];
    }

    return [];
  }

  // Главная функция: post — объект data поста из JSON Reddit.
  function extractJobs(post) {
    let jobs = mediaOf(post);
    // Кросспост: медиа лежит в исходном посте.
    if (!jobs.length && Array.isArray(post.crosspost_parent_list)) {
      for (const parent of post.crosspost_parent_list) {
        jobs = mediaOf(parent);
        if (jobs.length) break;
      }
    }
    const base = baseNameOf(post);
    jobs.forEach((j, i) => {
      j.baseName = jobs.length > 1 ? `${base} ${i + 1}` : base;
    });
    return jobs;
  }

  // Имя файла без расширения: «Заголовок [id]». Окончательная чистка — в помощнике.
  function baseNameOf(post) {
    const title = String(post.title || "").replace(/\s+/g, " ").trim() || "reddit";
    return `${title} [${post.id || "post"}]`;
  }

  // Находит объект поста в ответе /comments/<id>/.json.
  function postFromResponse(json) {
    const listing = Array.isArray(json) ? json[0] : json;
    const child = listing?.data?.children?.find((c) => c?.kind === "t3");
    return child?.data || null;
  }

  const api = { postIdFromUrl, isShareUrl, extractJobs, postFromResponse, baseNameOf };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.RedditMedia = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
