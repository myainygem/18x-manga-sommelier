# Installation & Setup Guide

Three ways to run 18x Manga Sommelier:

1. **From source (recommended for building your personal library)** — full CLI pipeline
2. **Windows installer (`MangaRecV1-Setup-1.0.0.exe`)** — windowed app, empty library seed
3. **Portable folder** — same as the installer, but everything stays in one folder

---

## 1. From source (Windows)

### 1.1 Prerequisites

- Windows 10/11
- Python 3.10 – 3.13
- Microsoft Edge (or WebView2 runtime) for the native-window mode (`cli.py app`)
- Optional: a running HTTP proxy (e.g. Clash on `127.0.0.1:7897`) if the sites
  are blocked on your network

### 1.2 Setup

```bat
git clone https://github.com/myainygem/18x-manga-sommelier.git
cd 18x-manga-sommelier
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.json config.json
```

### 1.3 Configuration

Open `config.json`:

| What | Where |
|---|---|
| DeepSeek API key (optional) | `ai.api_key` (or env var `DEEPSEEK_API_KEY`, which wins) |
| HTTP proxy | `"proxy": "http://127.0.0.1:7897"` |
| Recommendation batch sizes | `recommend.quota_precision` / `quota_trending` / `quota_series` |
| Taste filters (Chinese-only, exclusions, penalties) | `filters.*` |
| Site switches | `sites.*` |

### 1.4 The pipeline

```bat
python cli.py check          # sanity check: proxy, sites, AI key (use --ai to verify key)
python cli.py parse          # parse folder_list.txt (GBK)
python cli.py lookup         # match entries to E-Hentai metadata — long, resumable
python cli.py auto-resolve   # optional AI pass for entries the rules missed
python cli.py manual-pick L  # review leftovers: data/manual_review.txt
python cli.py profile        # build the taste profile
python cli.py recommend      # generate a recommendation batch → reports/
python cli.py serve          # browser UI  http://127.0.0.1:8765
python cli.py app            # native window (WebView2)
```

`folder_list.txt` format: one folder name per line, GBK-encoded, same text as
the actual folders:

```
[circle (artist)] Title CH.1-2 [Chinese] [Digital]
(artist) Another Title [Chinese] [無修正]
```

---

## 2. Windows installer

1. Download `MangaRecV1-Setup-1.0.0.exe` from
   [Releases](https://github.com/myainygem/18x-manga-sommelier/releases).
2. Run it — no admin rights needed. Choose the install folder.
3. Optional installer task **"Portable mode"**: keeps all data inside the
   install folder (a `portable.flag` file is created). Without it, data goes to
   `%APPDATA%\MangaRecommender`.
4. Launch `MangaRecV1` from the start menu / desktop.

The installer ships a **clean seed** (empty library, empty API key). Edit
`config.json` next to the app to fill in your key/proxy. Re-running the
installer never overwrites your config or data (`onlyifdoesntexist`).

> The packaged app is the UI only — building a personal library (parse/lookup)
> is a CLI step, so new users should follow section 1 for the full flow.

## 3. Portable folder

Copy the `dist\MangaRecV1\` folder anywhere, drop an empty file named
`portable.flag` next to `MangaRecV1.exe`:

```
MangaRecV1\
├─ MangaRecV1.exe
├─ portable.flag     ← data/config stay in this folder
├─ config.json       ← edit this
└─ data\
```

Uninstall = delete the folder. Nothing is written to `%APPDATA%` or the
registry in portable mode.

---

## Cookies (optional, 18comic)

18comic anonymous search works out of the box via the jmcomic API client. If
you need a logged-in session, place a cookie file in the data directory
(`data\jm_cookies.json` as a JSON array, or `data\jm_cookies.txt` in Netscape
format) — the dev CLI `python cli.py jm-login` can also extract cookies from
your local Chrome profile interactively.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| A site shows "不可用" in the UI | Proxy not running / wrong port; check `config.json → proxy` |
| nhentai 403 | Should not happen (curl_cffi impersonation); update `curl_cffi` |
| AI features off | `ai.api_key` empty — fill it or set `DEEPSEEK_API_KEY` |
| Window mode shows a blank page | Install Microsoft Edge / WebView2 runtime |
| Rate-limited on E-Hentai | Keep the default 7 s interval; runs are meant to be slow |

## Building the installer yourself

```bat
pip install -r requirements-build.txt
pyinstaller manga_rec_v1.spec --noconfirm
"<Inno Setup 6>\ISCC.exe" installer\manga_rec_v1_release.iss
```

> The build bundles the **clean seed only** — make sure `config.json` /
> `folder_list.txt` / `data\` contain no personal data before building (they
> are the seed source; see `.gitignore`).
