<p align="center"><img src="extension/icons/128.png" width="96" alt=""></p>

<h1 align="center">Reddit Downloader</h1>

<p align="center"><a href="README.md">Русский</a> · <b>English</b></p>

A Chrome extension: one toolbar button downloads the video or gif from the Reddit post open in the
current tab straight into your Downloads folder. Videos are saved **with sound, in the best available
quality**; gifs are **converted to mp4** right away.

Merging and conversion are done by the system `ffmpeg` through a small Python helper (native
messaging). That keeps the extension tiny and fast: a video is merged without re-encoding in seconds.

> Works on **Linux** (tested on Arch Linux with Google Chrome). macOS and Windows are untested: the helper
> is registered in different places there.
>
> The extension's own messages (notifications, tooltip) are currently in Russian only.

## Features

| Post | What happens |
|---|---|
| Reddit video (v.redd.it) | the best picture (often 1080p) and the best audio are downloaded as whole files and merged **without re-encoding** |
| Gif (i.redd.it or a link to a `.gif`) | the original is downloaded and converted to mp4 (H.264, CRF 18), frame timing is preserved exactly |
| Gallery, gifs and videos embedded in a text post | every animation and video is downloaded; still images are skipped |
| Crosspost | media is taken from the original post |
| Imgur `.gifv` | the matching `.mp4` is downloaded |

- You can click the button on several posts in a row: jobs are queued, and only one `ffmpeg` runs at a
  time, at low priority (`nice -n 19`, `-threads 4`). Your computer stays responsive.
- File name: `Post title [id].mp4`; galleries get `… [id] 1.mp4`, `… [id] 2.mp4`. Existing files are never
  overwritten: the new one gets a ` (2)` suffix.
- Progress is shown on the button (`…` → percent → ✓ or !), and a notification appears when done.

Videos hosted elsewhere (YouTube etc.) are not supported.

## Requirements

- Linux, Google Chrome / Chromium / Brave 116 or newer
- `python3` (3.8+)
- `ffmpeg` and `ffprobe` (5.1+, with `libx264`)

Arch Linux: `sudo pacman -S ffmpeg python` · Debian/Ubuntu: `sudo apt install ffmpeg python3`

## Installation

```bash
git clone https://github.com/HelpFreedom/reddit-downloader.git
cd reddit-downloader
./install.sh
```

`install.sh` registers the helper with every Chrome, Chromium and Brave profile it finds: it writes one JSON
file to `~/.config/<browser>/NativeMessagingHosts/`. No root needed.

Then in the browser:
1. Open `chrome://extensions` and turn on **Developer mode** (top right).
2. Click **Load unpacked** and select the `extension/` folder of this repository.
3. Make sure the extension ID is `ppffdkmkldfdncbpjannmploigpjdjgn`. It is fixed by the `key` field in
   the manifest, and the helper only answers this ID.
4. Pin the button to the toolbar (puzzle icon → pin).

> Don't move the folder after installing: the registration stores the helper's path. If you moved it,
> run `./install.sh` again and reload the extension.

## Usage

Open a Reddit post (a `…/comments/…` URL) and click the button. The file appears in your Downloads folder
(`xdg-user-dir DOWNLOAD`, usually `~/Downloads`).

## Updating

```bash
git pull
```

Then click ⟳ on the extension's card at `chrome://extensions`.

## Troubleshooting

| Symptom | What to do |
|---|---|
| «Помощник отключился … not found» (helper disconnected) | run `./install.sh` and restart the browser |
| «… forbidden» | the extension ID is not `ppffdkmkldfdncbpjannmploigpjdjgn`: load the `extension/` folder from this repository with `manifest.json` unchanged |
| «Нечего скачивать» (nothing to download) | the post has no video or gif, or it links to an external site |
| Any other error | check the helper log `~/.cache/reddit-downloader/host.log` and the extension console: `chrome://extensions` → "Service worker" → lines starting with `[RD]` |

## How it works

```
button → background.js ──(post JSON from the tab)──► reddit.js: list of jobs
                     └──(native messaging)──► host/reddit_downloader_host.py ──► ffmpeg ──► ~/Downloads
```

- **`extension/reddit.js`** parses the post JSON into jobs, each with a list of fallback methods.
- **`extension/background.js`** fetches `/comments/<id>/.json` from inside the open tab, using your Reddit
  session. If the JSON is unavailable, it looks for the player in the page markup. Jobs are queued.
- **`host/reddit_downloader_host.py`** is the helper; the browser starts it on demand:
  - **Video:** reads the DASH manifest, picks the best picture (the post's `fallback_url`, which is often
    better than anything in the manifest) and the best audio, and merges them with `ffmpeg -c copy`.
  - **Gif:** converts to mp4 (`libx264`, CRF 18, `yuv420p`, even dimensions, `-fps_mode vfr`).
  - **Duration check:** after every method, the result's duration is compared with the expected one,
    and a truncated file is never saved.
  - **Queue:** a global lock, `~/.cache/reddit-downloader/ffmpeg.lock`, ensures only one `ffmpeg` runs
    at a time.
  - **Browser closed:** if you close the browser mid-job, `ffmpeg` is stopped and partial files are
    removed.

**Why not HLS.** Reddit's HLS playlists are byte ranges of a single CMAF file. `ffmpeg` (tested with 9.0)
reads only the first segment, 4 seconds, and still exits without an error. On top of that, Reddit's HLS
tops out at 720p with 64 kbps audio.

## Security and privacy

- The extension collects nothing and sends nothing anywhere. Network requests go only to Reddit itself
  (post data) and to media servers.
- The helper accepts messages only from the extension with ID `ppffdkmkldfdncbpjannmploigpjdjgn` (enforced
  by the browser), and downloads only over `https` and only from Reddit addresses (`redd.it`,
  `reddit.com`, `redditmedia.com`, `redditstatic.com`) and a few gif hosts (`imgur.com`, `giphy.com`,
  `gfycat.com`, `redgifs.com`, `tenor.com`). When files are downloaded, redirects are checked too.
- `ffmpeg` runs with a `-protocol_whitelist` of network protocols only, so it won't open a local file even
  if a manifest points to one.
- Extension permissions: `scripting` and access to `*.reddit.com` (read the post from the tab),
  `nativeMessaging` (talk to the helper), `notifications`.

## Tests

```bash
node tests/test_reddit.js        # post parsing (17 checks)
python3 -I tests/test_host.py    # helper: local HLS/DASH/CMAF/gif → mp4 (~10 s)
```

`test_host.py` starts an HTTP server on `127.0.0.1` and builds its test media with `ffmpeg` (one at a time,
with `nice`). It writes nothing to your Downloads folder.

## Uninstalling

```bash
./uninstall.sh
```

Then remove the extension at `chrome://extensions`. The helper log in `~/.cache/reddit-downloader` can
be deleted by hand.

## Authors

- **Black Triangle** ([@HelpFreedom](https://github.com/HelpFreedom)), project owner
- **Claude** (Anthropic), code and documentation

## License

[GPL-3.0](LICENSE). Not affiliated with Reddit, Inc.; "Reddit" is a trademark of Reddit, Inc.
