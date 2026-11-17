// node tests/test_reddit.js — проверка разбора постов (структура как в JSON Reddit с raw_json=1).
"use strict";
const assert = require("node:assert/strict");
const R = require("../extension/reddit.js");

let passed = 0;
function t(name, fn) {
  try { fn(); passed++; console.log("ok  ", name); }
  catch (e) { console.error("FAIL", name, "\n", e.message); process.exitCode = 1; }
}
const wrap = (post) => [{ kind: "Listing", data: { children: [{ kind: "t3", data: post }] } },
  { kind: "Listing", data: { children: [] } }];

// --- адреса
t("id из обычного адреса поста", () => {
  assert.equal(R.postIdFromUrl("https://www.reddit.com/r/videos/comments/1ab2cd3/my_first_video_post/"), "1ab2cd3");
  assert.equal(R.postIdFromUrl("https://www.reddit.com/r/gifs/comments/1ef4gh5/a_funny_gif/"), "1ef4gh5");
});
t("id из старого Reddit, комментария, галереи, redd.it", () => {
  assert.equal(R.postIdFromUrl("https://old.reddit.com/r/x/comments/abc123/t/def456/?context=3"), "abc123");
  assert.equal(R.postIdFromUrl("https://www.reddit.com/gallery/abc123"), "abc123");
  assert.equal(R.postIdFromUrl("https://redd.it/abc123"), "abc123");
  assert.equal(R.postIdFromUrl("https://www.reddit.com/r/x/comments/ABC123"), "abc123");
});
t("не-пост → null", () => {
  assert.equal(R.postIdFromUrl("https://www.reddit.com/r/videos/"), null);
  assert.equal(R.postIdFromUrl("https://example.com/comments/abc123/"), null);
  assert.equal(R.postIdFromUrl("https://notreddit.com/r/x/comments/abc123/"), null);
  assert.equal(R.postIdFromUrl("chrome://extensions"), null);
  assert.equal(R.postIdFromUrl(""), null);
});
t("короткая ссылка /s/", () => {
  assert.equal(R.isShareUrl("https://www.reddit.com/r/videos/s/AbCdEf123"), true);
  assert.equal(R.isShareUrl("https://www.reddit.com/r/videos/comments/1ab2cd3/"), false);
});

// --- видео
const videoPost = {
  id: "1ab2cd3", title: "My first   video  post", subreddit: "videos",
  url: "https://v.redd.it/k3y9abcde", url_overridden_by_dest: "https://v.redd.it/k3y9abcde",
  is_video: true, post_hint: "hosted:video",
  secure_media: { reddit_video: {
    bitrate_kbps: 5000, fallback_url: "https://v.redd.it/k3y9abcde/CMAF_1080.mp4?source=fallback",
    has_audio: true, height: 1080, width: 1920, duration: 47, is_gif: false,
    hls_url: "https://v.redd.it/k3y9abcde/HLSPlaylist.m3u8?a=1&v=1&f=sd",
    dash_url: "https://v.redd.it/k3y9abcde/DASHPlaylist.mpd?a=1&v=1&f=sd",
    transcoding_status: "completed",
  } },
  media: null, // проверяем, что secure_media берётся первым
};
t("видео: целые файлы (DASH + fallback 1080) → DASH → HLS → fallback, со звуком", () => {
  const post = R.postFromResponse(wrap(videoPost));
  const jobs = R.extractJobs(post);
  assert.equal(jobs.length, 1);
  const j = jobs[0];
  assert.equal(j.hasAudio, true);
  assert.equal(j.duration, 47);
  assert.deepEqual(j.candidates.map((c) => c.type), ["reddit", "manifest", "manifest", "file"]);
  assert.match(j.candidates[0].url, /DASHPlaylist\.mpd\?a=1&v=1/); // raw_json: без &amp;
  assert.equal(j.candidates[0].video, "https://v.redd.it/k3y9abcde/CMAF_1080.mp4?source=fallback");
  assert.match(j.candidates[1].url, /DASHPlaylist/);
  assert.match(j.candidates[2].url, /HLSPlaylist/);
  assert.equal(j.baseName, "My first video post [1ab2cd3]");
});
t("видео-gif Reddit (is_gif): звука не ждём", () => {
  const p = structuredClone(videoPost);
  p.secure_media.reddit_video.is_gif = true;
  p.secure_media.reddit_video.has_audio = false;
  assert.equal(R.extractJobs(p)[0].hasAudio, false);
});
t("видео только в media (нет secure_media)", () => {
  const p = structuredClone(videoPost);
  p.media = p.secure_media; p.secure_media = null;
  assert.equal(R.extractJobs(p).length, 1);
});
t("кросспост: медиа берётся из исходного поста, имя — из кросспоста", () => {
  const xp = { id: "zzz111", title: "Look at this", url: "/r/videos/comments/1ab2cd3/x/",
    secure_media: null, media: null, crosspost_parent_list: [videoPost] };
  const jobs = R.extractJobs(xp);
  assert.equal(jobs.length, 1);
  assert.equal(jobs[0].baseName, "Look at this [zzz111]");
  assert.match(jobs[0].candidates[0].url, /k3y9abcde/);
});

// --- gif
const gifPost = {
  id: "1ef4gh5", title: "A funny gif…", url: "https://i.redd.it/q8w7e6r5t4.gif",
  url_overridden_by_dest: "https://i.redd.it/q8w7e6r5t4.gif", post_hint: "image", is_video: false,
  secure_media: null, media: null,
  preview: { enabled: true, images: [{
    source: { url: "https://preview.redd.it/q8w7e6r5t4.gif?width=800&format=png&auto=webp&s=aaa", width: 800, height: 450 },
    variants: {
      gif: { source: { url: "https://preview.redd.it/q8w7e6r5t4.gif?s=bbb", width: 800, height: 450 } },
      mp4: { source: { url: "https://preview.redd.it/q8w7e6r5t4.gif?format=mp4&s=ccc", width: 800, height: 450 } },
    },
  }] },
};
t("gif-пост: оригинал → gif-превью → mp4-вариант", () => {
  const jobs = R.extractJobs(gifPost);
  assert.equal(jobs.length, 1);
  assert.equal(jobs[0].hasAudio, false);
  assert.deepEqual(jobs[0].candidates.map((c) => c.url), [
    "https://i.redd.it/q8w7e6r5t4.gif",
    "https://preview.redd.it/q8w7e6r5t4.gif?s=bbb",
    "https://preview.redd.it/q8w7e6r5t4.gif?format=mp4&s=ccc",
  ]);
  assert.ok(jobs[0].candidates.every((c) => c.type === "file"));
});
t("imgur .gifv → .mp4", () => {
  const p = { id: "a1b2c3", title: "x", url: "https://i.imgur.com/AbCd.gifv" };
  assert.equal(R.extractJobs(p)[0].candidates[0].url, "https://i.imgur.com/AbCd.mp4");
});

// --- галерея и медиа в тексте
const galleryPost = {
  id: "gal123", title: "Gallery", url: "https://www.reddit.com/gallery/gal123", is_gallery: true,
  gallery_data: { items: [{ media_id: "img2", id: 2 }, { media_id: "anim1", id: 1 }, { media_id: "img3", id: 3 }] },
  media_metadata: {
    anim1: { status: "valid", e: "AnimatedImage", m: "image/gif", id: "anim1",
      s: { x: 480, y: 270, gif: "https://i.redd.it/anim1.gif", mp4: "https://preview.redd.it/anim1.gif?format=mp4&s=1" } },
    img2: { status: "valid", e: "Image", m: "image/jpg", id: "img2", s: { u: "https://preview.redd.it/img2.jpg?s=1" } },
    img3: { status: "valid", e: "AnimatedImage", m: "image/gif", id: "img3",
      s: { gif: "https://preview.redd.it/img3.gif?s=2", mp4: "https://preview.redd.it/img3.gif?format=mp4&s=3" } },
  },
};
t("галерея: только анимации, в порядке галереи, с номерами", () => {
  const jobs = R.extractJobs(galleryPost);
  assert.equal(jobs.length, 2);
  assert.equal(jobs[0].label, "anim1");
  assert.equal(jobs[1].label, "img3");
  assert.equal(jobs[0].baseName, "Gallery [gal123] 1");
  assert.equal(jobs[1].baseName, "Gallery [gal123] 2");
  assert.equal(jobs[1].candidates[0].url, "https://i.redd.it/img3.gif");
});
t("галерея только из картинок → пусто", () => {
  const p = structuredClone(galleryPost);
  delete p.media_metadata.anim1; delete p.media_metadata.img3;
  assert.deepEqual(R.extractJobs(p), []);
});
t("видео в тексте поста (RedditVideo в media_metadata)", () => {
  const p = { id: "self12", title: "Text post", url: "https://www.reddit.com/r/x/comments/self12/text_post/", is_self: true,
    media_metadata: { v1: { status: "valid", e: "RedditVideo", id: "v1", x: 1280, y: 720, isGif: false,
      hlsUrl: "https://v.redd.it/link/self12/asset/v1/HLSPlaylist.m3u8?a=1", dashUrl: "https://v.redd.it/link/self12/asset/v1/DASHPlaylist.mpd?a=1" } } };
  const jobs = R.extractJobs(p);
  assert.equal(jobs.length, 1);
  assert.equal(jobs[0].hasAudio, null);
  assert.deepEqual(jobs[0].candidates.map((c) => c.type), ["reddit", "manifest", "manifest"]);
  assert.equal(jobs[0].candidates[0].video, null);
});
t("незагруженное медиа (status != valid) пропускается", () => {
  const p = { id: "x1y2z3", title: "t", url: "https://www.reddit.com/gallery/x1y2z3",
    media_metadata: { a: { status: "unprocessed", e: "AnimatedImage", id: "a" } } };
  assert.deepEqual(R.extractJobs(p), []);
});

// --- без медиа
t("текстовый пост и ссылка на статью → пусто", () => {
  assert.deepEqual(R.extractJobs({ id: "t1t1t1", title: "t", url: "https://www.reddit.com/r/x/comments/t1t1t1/t/", is_self: true }), []);
  assert.deepEqual(R.extractJobs({ id: "t2t2t2", title: "t", url: "https://github.com/foo/bar",
    preview: { images: [{ source: { url: "https://external-preview.redd.it/a.jpg" }, variants: {} }] } }), []);
});
t("внешнее видео с reddit_video_preview", () => {
  const p = { id: "ext111", title: "t", url: "https://gfycat.com/whatever",
    preview: { reddit_video_preview: { hls_url: "https://v.redd.it/abc/HLSPlaylist.m3u8", fallback_url: "https://v.redd.it/abc/DASH_720.mp4", duration: 5, is_gif: true } } };
  const j = R.extractJobs(p)[0];
  assert.equal(j.hasAudio, null);
  assert.equal(j.candidates[0].type, "reddit"); // нет dash_url → url = fallback_url
  assert.equal(j.candidates[0].url, "https://v.redd.it/abc/DASH_720.mp4");
});
t("postFromResponse: ошибка Reddit → null", () => {
  assert.equal(R.postFromResponse({ message: "Not Found", error: 404 }), null);
  assert.equal(R.postFromResponse(null), null);
});

console.log(`\n${passed} passed`);
