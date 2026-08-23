# MCA Document Agent

An agent that runs once every 24 hours, downloads Companies Act 2013
related documents (Acts, Rules, Notifications, Circulars, Orders,
Amendments) from the MCA (Ministry of Corporate Affairs) website,
extracts their text, classifies each one with Gemini, and files it into
a category folder — stopping after 100 new documents per run.

```
Task Scheduler (every 24h)
        |
        v
   run_task.bat
        |
        v
   src/main.py  ──►  scraper.py     (Selenium: reads MCA's JS-rendered PDF listing pages)
                 ──►  downloader.py (downloads new PDFs, capped at 100/run, verifies PDF signature)
                 ──►  extractor.py  (pdfplumber text first, Tesseract OCR fallback if scanned)
                 ──►  classifier.py (Gemini: text -> one of your configured categories)
                 ──►  organizer.py  (moves file into output/<Category>/)
                        |
                        v
                 manifest.py (SQLite: dedup + status log across runs)
```

Everything you're likely to want to change lives in **`config.yaml`** —
paths, which MCA pages to scrape, the category list, OCR thresholds, the
Gemini model name, and rate limits. You shouldn't need to touch the code
in `src/` for normal use.

---

## 1. One-time setup (Windows)

### 1.1 Install Python

If you don't already have it: https://www.python.org/downloads/ (3.10+).
During install, check **"Add Python to PATH."**

### 1.2 Get the project onto your machine

Unzip this project anywhere, e.g. `C:\MCA_Agent`.

### 1.3 Create a virtual environment and install dependencies

Open a terminal (PowerShell or cmd) **in the project folder** and run:

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 1.4 Install Tesseract OCR (needed only for scanned PDFs)

Most MCA Act/Rules/Circular PDFs are native text, so OCR is a fallback
path, not the main path — but a few older scanned notifications do need
it.

Install it with winget (one command, no configuration needed):

```powershell
winget install --id UB-Mannheim.TesseractOCR
```

That is the whole OCR setup. **You do not need to edit `config.yaml`**:
that installer does not add itself to `PATH`, so the code checks `PATH`
*and* the standard Windows install locations and finds
`C:/Program Files/Tesseract-OCR/tesseract.exe` on its own. Only set
`extraction.ocr.tesseract_cmd` if you installed it somewhere unusual.

**Poppler is no longer required.** Page rendering uses PyMuPDF, a
self-contained pip wheel installed by `requirements.txt`, so Tesseract is
the only external program OCR needs. (The earlier build shelled out to
Poppler via `pdf2image`; Poppler is not on `PATH` by default on Windows,
which produced `Unable to get page count. Is poppler installed and in
PATH?` and silently downgraded every scanned PDF to title-only
classification.)

Two OCR knobs in `config.yaml`, both optional:

```yaml
extraction:
  ocr:
    render_dpi: 300   # higher = better accuracy, slower. 300 is the sweet spot.
    language: "eng"   # add packs like "eng+hin" if you install them
```

If you'd rather skip OCR entirely (scanned PDFs are still downloaded and
filed, just classified from their title alone), set
`extraction.ocr.enabled: false` — then no Tesseract install is needed.

### 1.6 Install Google Chrome

The scraper drives headless Chrome (MCA's pages are a JS-rendered
Angular app, so a plain HTTP request returns an empty page). Install
Chrome normally: https://www.google.com/chrome/ — the scraper manages
its own driver binary automatically (`webdriver-manager`), no separate
chromedriver install needed.

### 1.7 Add your Gemini API key

1. Get a free key at https://aistudio.google.com/apikey
2. Copy `.env.example` to a new file named `.env` in the same folder.
3. Edit `.env`:
   ```
   GEMINI_API_KEY=your_real_key_here
   ```
   `.env` is already in `.gitignore` — never commit it.

### 1.8 Configure where things live

Everything is controlled by `paths.project_root` in `config.yaml`:

```yaml
paths:
  project_root: "." # default: wherever config.yaml lives
```

Leave it as `"."` to keep everything inside this project folder, or
point it anywhere else, e.g.:

```yaml
paths:
  project_root: "D:/Automation/MCA_Agent"
```

All other paths (`raw_dir`, `output_dir`, `manifest_db`, `logs_dir`) are
relative to `project_root` unless you type an absolute path.

### 1.9 Test it manually before scheduling

```bat
venv\Scripts\activate
python -m src.main
```

Watch the console output (also saved under `logs/`). Check
`data/output/` for the category folders it created.

---

## 2. Scheduling — run every 24 hours

We use **Windows Task Scheduler** to own the schedule, rather than a
Python script that sleeps in a loop — a scheduled task survives reboots,
shows you a run history, and doesn't die silently if something crashes.

1. Open **Task Scheduler** → **Create Task** (not "Basic Task" — we want
   the extra options).
2. **General tab**: name it `MCA Document Agent`. Select "Run whether
   user is logged on or not" if you want it to run even when you're
   logged out.
3. **Triggers tab** → New → **Daily**, pick a start time, recur every
   **1 day**.
4. **Actions tab** → New → Action: "Start a program" → Program/script:
   browse to `run_task.bat` inside this project folder.
   Set **"Start in"** to the project folder path too (e.g. `C:\MCA_Agent`).
5. **Conditions/Settings tabs**: uncheck "Start the task only if the
   computer is on AC power" if this is a laptop, so it still runs on
   battery.
6. Save. Right-click the task → **Run** to test it fires correctly.

---

## 3. Configuration reference (`config.yaml`)

| Section          | Key                                                   | What it controls                                                        |
| ---------------- | ----------------------------------------------------- | ----------------------------------------------------------------------- |
| `paths`          | `project_root`                                        | Base folder — **the one setting to move the whole project**             |
| `paths`          | `raw_dir` / `output_dir` / `manifest_db` / `logs_dir` | Where files land, relative to `project_root`                            |
| `mca`            | `pages`                                               | Which MCA listing pages to scrape (name + URL)                          |
| `run`            | `max_new_documents_per_run`                           | Hard cap — stops downloading after this many NEW docs (default 100)     |
| `categories`     | (list)                                                | The exact folder names the classifier can choose from                   |
| `extraction`     | `min_native_chars_threshold`                          | Below this many extracted characters, a PDF is treated as scanned → OCR |
| `extraction.ocr` | `enabled` / `tesseract_cmd` / `render_dpi` / `language` | OCR fallback settings (no `poppler_path` — PyMuPDF renders the pages)   |
| `gemini`         | `model`                                               | Gemini model used for classification (see note below)                   |
| `gemini`         | `requests_per_minute`                                 | Client-side rate limit to stay under free-tier quota                    |

**About the Gemini model name:** Google renames/retires free-tier model
IDs fairly often. `config.yaml` currently points at
`gemini-2.5-flash-lite`. If a run starts failing with a `404` or
"model not found" error, check https://ai.google.dev/gemini-api/docs/models
for the current free-tier model name and update `gemini.model` — no
code change needed.

---

## 4. Dedup and re-runs

Every document the agent has ever seen is recorded in
`data/manifest.sqlite3`, keyed by source URL **and** content hash. This
means:

- Re-running the same day won't re-download or re-classify anything.
- The 100-document cap only counts genuinely **new** documents each run.
- If MCA links the same PDF from two different pages, it's only
  downloaded once.

To force a full re-scan (e.g. after changing categories), delete
`data/manifest.sqlite3` — it will be recreated empty on the next run.
This does **not** delete already-organized files in `data/output/`.

---

## 5. Security notes (why the code is written the way it is)

- **Scraped/OCR'd document text is treated as untrusted input**, not as
  instructions. The classifier prompt explicitly tells Gemini to ignore
  any instruction-like text found _inside_ a document and to only
  return a category — this guards against a malicious or corrupted PDF
  attempting a prompt injection against the classifier.
- **The classifier's output is schema-constrained** to your configured
  category list (Gemini structured output / enum) — it cannot return
  arbitrary text — and is **re-validated in code** before ever being
  used to build a folder path, so a model output can never become an
  unexpected filesystem path.
- **Downloads are cap-enforced in code** (`max_new_documents_per_run`),
  not just described in a prompt — an unbounded download/classify loop
  can't happen even if MCA's page structure changes unexpectedly.
- **Every downloaded file is checked for a real PDF signature**
  (`%PDF-` header) before being trusted — a scraped link that doesn't
  actually point at a PDF is discarded rather than silently mis-filed.
- **The Gemini API key lives only in `.env`** (git-ignored), never in
  `config.yaml` or in code.

## 6. Troubleshooting

**`SessionNotCreatedException: This version of ChromeDriver only supports
Chrome version X. Current browser version is Y`**
Your installed Chrome auto-updated itself past the driver Selenium was
using. Fix: update Selenium itself, which resolves the correct driver
for whatever Chrome version is actually installed:

```bat
venv\Scripts\activate
pip install --upgrade selenium
```

Chrome auto-updates silently in the background, so this can recur every
few weeks — if it does, re-run the command above. (This project relies
on Selenium's _built-in_ driver resolution, not a separately cached
driver, specifically so this stays a one-command fix.)

**Downloads fail with `403 Forbidden`**
mca.gov.in is behind **Akamai Bot Manager**. Three approaches were tested
against the live site:

| approach | result |
|---|---|
| plain `requests.get(pdf_url)` | 403 Forbidden |
| `requests` + the browser's cookies + `Referer` | **403 Forbidden** |
| `fetch()` run *inside* the Selenium browser | 200 OK, valid PDF |

The middle row is the intuitive fix and it does **not** work: Akamai
fingerprints the TLS/HTTP2 handshake itself, so a `requests` session is
identifiable as non-browser no matter which cookies it carries. That is
why `downloader.py` performs each download as an in-page `fetch()` and
returns the bytes to Python as base64, and why `main.py` keeps one
browser alive across both the scrape and download stages. If you see 403s
again, MCA has likely tightened Akamai further — check the
`error_message` column in `data/manifest.sqlite3`.

**A document failed once and is never retried**
It is retried. `manifest.already_known()` treats the statuses in
`Manifest.RETRYABLE_STATUSES` (`failed_download`, `failed_extract`,
`failed_classify`, `failed_organize`) as *not* known, so the next run
picks them up again. Only genuinely completed documents — and
`duplicate_content` — are skipped permanently. Each run logs
`N document(s) failed previously and will be retried` at startup.

**Scraper finds 0 documents**
The document links live in two regions of MCA's Adobe AEM markup, both
verified against the live site:

- `span.doc-link > a` — the dated "Important Updates / What's New" panel
- `div.marquee a` — the scrolling notice ticker

`mca.content_wait_selector` waits for the first of these. If MCA
redesigns the page, raise `mca.page_load_wait_seconds` /
`mca.settle_seconds` first; if that doesn't help, the selectors in
`src/scraper.py` need updating. When a page yields 0 documents the
scraper writes `debug_<page>_<timestamp>.html` plus a `.png` screenshot
into `logs/` so the real DOM can be inspected.

**Text extraction fails / documents get classified from the title only**
OCR needs the Tesseract *program* (see section 1.4 — `winget install
--id UB-Mannheim.TesseractOCR`). Poppler is NOT needed; rendering uses
PyMuPDF. If a scanned PDF still fails, the log names the reason:

- `Tesseract not found...` — install it, or set
  `extraction.ocr.tesseract_cmd` for a non-standard location.
- OCR ran but returned nothing — try raising `extraction.ocr.render_dpi`
  (300 -> 400); very low-quality scans sometimes need it.

A document whose OCR fails is still downloaded and filed; it is just
classified from its title alone, and `extraction_method` is recorded as
`failed` in `data/manifest.sqlite3` so you can find them:

```sql
SELECT title FROM documents WHERE extraction_method = 'failed';
```

**The scraper finds only the marquee links (`doc-link=0`)**
MCA has served the document panel as both `<span class="doc-link">` and
`<div class="doc-link">`. The selectors are tag-agnostic (`.doc-link a`)
for that reason, and the scraper reloads the page up to 3 times when the
panel is missing before giving up. If you still see `doc-link=0` on every
attempt, MCA has changed the markup again — check
`logs/debug_*.html`. Documents already recorded in the manifest are never
lost when this happens; they simply are not re-offered that run.

**`404` / model not found from Gemini**
Google renamed or retired the model in `gemini.model`. Check
https://ai.google.dev/gemini-api/docs/models for the current free-tier
model name and update `config.yaml` — no code change needed.

**Chrome not found / "cannot find Chrome binary"**
Selenium Manager expects Chrome installed in its normal location. If
you installed it somewhere non-standard, either reinstall normally or
set the binary location explicitly by adding to `_build_driver()` in
`src/scraper.py`: `options.binary_location = "C:/path/to/chrome.exe"`.

---

## 7. Known limitations / things to revisit

- **Only ~16 documents are reachable, and that is MCA's limit, not the
  agent's.** Every MCA page under this app renders the *same* site-wide
  document panel. Verified by scraping six different URLs — `acts-rules`,
  `ebooks`, `ebooks/notifications`, `notifications-tender/circulars`,
  `notices-circulars` and `whats-new` — all of which returned an identical
  link set. MCA's per-category tab component (the "Notice & Circular" /
  "Circulars" tabs) renders **"No results found."** to an anonymous
  headless browser, and the page makes no data API call that could be
  targeted instead. So there is no deeper archive to scrape without
  authenticating to MCA. `config.yaml` therefore lists a single page;
  adding more is harmless (the scraper de-duplicates) but gains nothing.
- **MCA's page markup can change.** The scraper targets
  `span.doc-link > a` and `div.marquee a`. If MCA redesigns these pages,
  `src/scraper.py` needs updating — check `logs/debug_*.html` if a run
  suddenly finds 0 documents.
- **Two of the current documents are scanned images.** Both are now read
  via OCR (Tesseract + PyMuPDF rendering) and classified from their real
  text — one of them, an MCA Office Memorandum on Vigilance Awareness
  Week, is correctly filed as a Circular only because OCR can read it.
  OCR runs at 300 DPI over the first `ocr_max_pages` pages.
- **Classification quality depends on extracted text quality.** A badly
  scanned document with poor OCR output may get misclassified into
  `Other` — that's the intended fail-safe behavior rather than a guess.
- **No de-duplication across category if a document type is genuinely
  ambiguous** (e.g. an amendment notification) — Gemini picks its single
  best category; there's no multi-label filing in this build.
