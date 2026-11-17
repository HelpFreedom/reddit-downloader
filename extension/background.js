// Service worker расширения Reddit Downloader.
// Клик по иконке → читаем пост из открытой вкладки → задания в очередь →
// по одному отдаём нативному помощнику (ffmpeg), который кладёт mp4 в папку загрузок.

importScripts("reddit.js");

const HOST = "io.github.helpfreedom.reddit_downloader";
const queue = [];       // задания, ждущие очереди
let current = null;     // задание, которое сейчас выполняется
let badgeResetTimer = null;

chrome.action.onClicked.addListener((tab) => {
  onClick(tab).catch((e) => notifyError("Не удалось прочитать пост", String(e?.message || e)));
});

async function onClick(tab) {
  const href = tab?.url || "";
  const id = RedditMedia.postIdFromUrl(href);
  if (!id && !RedditMedia.isShareUrl(href)) {
    notifyError("Это не пост Reddit", "Откройте страницу поста (…/comments/…) и нажмите кнопку ещё раз.");
    return;
  }
  setBadge("…", "#888888", "Читаю пост…");

  let page;
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: grabPost });
    page = res?.result;
  } catch (e) {
    throw new Error(`скрипт на странице не запустился: ${e?.message || e}`);
  }
  if (!page) throw new Error("страница ничего не вернула");

  let jobs = [];
  let post = null;
  if (page.json) {
    post = RedditMedia.postFromResponse(page.json);
    if (post) jobs = RedditMedia.extractJobs(post);
  }
  let source = "json";
  if (!jobs.length) {
    jobs = jobsFromDom(page);
    source = "dom";
  }
  console.log("[RD] post", page.id, "source", source, "status", page.status, page.jsonError || "", jobs);

  if (!jobs.length) {
    restoreBadge();
    const why = post ? "В этом посте нет видео или gif."
      : `Reddit не отдал данные поста (${page.jsonError || "HTTP " + page.status}).`;
    notifyError("Нечего скачивать", why);
    return;
  }

  let added = 0;
  for (const job of jobs) {
    job.userAgent = page.ua;
    const key = job.baseName + "|" + job.candidates[0].url;
    if (current?.key === key || queue.some((j) => j.key === key)) continue; // уже в работе
    job.key = key;
    queue.push(job);
    added++;
  }
  if (!added) {
    notify("Уже скачивается", "Этот пост уже в очереди.");
  }
  pump();
}

// Выполняется В СТРАНИЦЕ Reddit (изолированный мир контент-скрипта), поэтому
// функция самодостаточна: никаких внешних переменных. Запрос идёт с куками
// пользователя, как будто сам сайт запросил данные поста.
async function grabPost() {
  const out = { href: location.href, ua: navigator.userAgent, id: null, status: 0, json: null, jsonError: null, dom: {} };
  const re = /\/(?:comments|gallery)\/([a-z0-9]{4,12})(?:[/?#]|$)/i;
  let m = location.pathname.match(re);
  if (!m && /^\/r\/[^/]+\/s\//i.test(location.pathname)) {
    try {
      const r = await fetch(location.href, { credentials: "include" });
      m = new URL(r.url).pathname.match(re);
    } catch { /* ниже сообщим, что id не найден */ }
  }
  out.id = m ? m[1].toLowerCase() : null;
  if (out.id) {
    try {
      const r = await fetch(`${location.origin}/comments/${out.id}/.json?raw_json=1&limit=1`, {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      out.status = r.status;
      const text = await r.text();
      try { out.json = JSON.parse(text); } catch { out.jsonError = `ответ не JSON (HTTP ${r.status})`; }
    } catch (e) {
      out.jsonError = String(e);
    }
  } else {
    out.jsonError = "не найден id поста в адресе";
  }

  // Запасной путь — разметка страницы нового Reddit (если JSON недоступен).
  const post = (out.id && document.querySelector(`shreddit-post[id="t3_${out.id}"]`)) ||
    document.querySelector("shreddit-post");
  const scope = post || document;
  const player = scope.querySelector("shreddit-player-2[src], shreddit-player[src]");
  out.dom = {
    playerSrc: player ? player.getAttribute("src") : null,
    contentHref: post ? post.getAttribute("content-href") : null,
    title: (post && post.getAttribute("post-title")) || document.title || "",
  };
  return out;
}

function jobsFromDom(page) {
  const d = page.dom || {};
  const base = RedditMedia.baseNameOf({ title: d.title, id: page.id || "post" });
  const isHttps = (u) => typeof u === "string" && u.startsWith("https://");
  if (isHttps(d.playerSrc)) {
    const candidates = [];
    // Плеер даёт HLS, который ffmpeg обрезает до 4 с; рядом всегда лежит DASH-манифест (подпись не нужна).
    const vm = d.playerSrc.match(/^(https:\/\/v\.redd\.it\/[^?#]+\/)[^/?#]+\.(?:m3u8|mpd)(?:[?#]|$)/i);
    if (vm) candidates.push({ type: "reddit", url: vm[1] + "DASHPlaylist.mpd", video: null });
    candidates.push({ type: /\.(m3u8|mpd)(\?|$)/i.test(d.playerSrc) ? "manifest" : "file", url: d.playerSrc });
    return [{ label: "video", baseName: base, hasAudio: null, duration: 0, candidates }];
  }
  if (isHttps(d.contentHref) && /\.(gif|gifv|mp4)(\?|$)/i.test(new URL(d.contentHref).pathname + "?")) {
    const url = d.contentHref.replace(/\.gifv(?=$|[?#])/i, ".mp4");
    return [{ label: "gif", baseName: base, hasAudio: false, duration: 0, candidates: [{ type: "file", url }] }];
  }
  return [];
}

// ------------------------------------------------------------ очередь

function pump() {
  // Значок после последнего задания (✓ / !) сбрасывает таймер flashBadge.
  if (current || !queue.length) return;
  current = queue.shift();
  runJob(current).finally(() => {
    current = null;
    pump();
  });
}

function runJob(job) {
  return new Promise((resolve) => {
    let finished = false;
    let port;
    const finish = () => { if (!finished) { finished = true; resolve(); } };
    const { key, ...payload } = job; // key нужен только нам
    showProgress(null, "запуск");

    try {
      port = chrome.runtime.connectNative(HOST);
    } catch (e) {
      notifyError("Помощник не запустился", String(e?.message || e));
      return finish();
    }

    port.onMessage.addListener((msg) => {
      if (msg.type === "progress") {
        showProgress(msg.percent, msg.stage);
      } else if (msg.type === "done") {
        const name = msg.path.split("/").pop();
        const warn = (msg.warnings || []).join(" ");
        notify("Сохранено", `${name}${warn ? "\n⚠ " + warn : ""}`);
        flashBadge("✓", "#2e7d32");
        port.disconnect();
        finish();
      } else if (msg.type === "error") {
        notifyError("Не удалось скачать", msg.message);
        flashBadge("!", "#c62828");
        port.disconnect();
        finish();
      }
    });

    port.onDisconnect.addListener(() => {
      if (finished) return;
      const err = chrome.runtime.lastError?.message || "соединение прервано";
      const hint = /not found|forbidden/i.test(err)
        ? " — помощник не установлен: запустите install.sh из папки проекта и перезапустите Chrome."
        : "";
      notifyError("Помощник отключился", err + hint);
      flashBadge("!", "#c62828");
      finish();
    });

    port.postMessage({ type: "download", job: payload });
  });
}

// ------------------------------------------------------------ значок и уведомления

function setBadge(text, color, title) {
  clearTimeout(badgeResetTimer);
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setTitle({ title });
}

function showProgress(percent, stage) {
  const more = queue.length ? ` (ещё в очереди: ${queue.length})` : "";
  const pct = typeof percent === "number" ? `${Math.floor(percent)}%` : "…";
  setBadge(pct, "#ff4500", `${current?.baseName || ""}\n${stage || ""} ${pct}${more}`);
}

function flashBadge(text, color) {
  setBadge(text, color, "Скачать видео / gif из поста");
  badgeResetTimer = setTimeout(() => { if (!current) restoreBadge(); }, 4000);
}

function restoreBadge() {
  if (current) return;
  clearTimeout(badgeResetTimer);
  chrome.action.setBadgeText({ text: "" });
  chrome.action.setTitle({ title: "Скачать видео / gif из поста" });
}

function notify(title, message) {
  chrome.notifications.create({ type: "basic", iconUrl: "icons/128.png", title, message: String(message).slice(0, 500) });
}

function notifyError(title, message) {
  console.warn("[RD]", title, message);
  restoreBadge();
  notify(title, message);
}
