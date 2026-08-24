# Document Processing Pipeline

## Overview

The system performs **one complete run from top to bottom**.

There is:

- No infinite loop
- No internal scheduler
- No sleep-based scheduling

`main.py` orchestrates a single execution, and **Windows Task Scheduler** is responsible for triggering the process every 24 hours through `run_task.bat`.

```text
Windows Task Scheduler
        │
        ▼
   run_task.bat
        │
        ▼
      main.py
        │
        ▼
   Single Pipeline Run
```

---

# High-Level Execution Flow

```text
config.yaml
    │
    ▼
load_config()
    │
    ▼
Manifest (SQLite)
opens / creates:
data/manifest.sqlite3
    │
    ▼
build_driver()
    │
    ▼
┌─────────────────────────────────────────────┐
│         ONE Headless Chrome Instance        │
│                                             │
│   scrape_all_pages(cfg, driver)             │
│                    │                        │
│                    ▼                        │
│   download_new_documents(..., driver)       │
│                                             │
└─────────────────────────────────────────────┘
    │
    ▼
driver.quit()
    │
    ▼
For each downloaded document:
    │
    ├── Extract
    ├── Classify
    └── Organize
```

## Why a Single Browser Instance Is Critical

The single-browser design is **load-bearing**.

The MCA website is protected by **Akamai**, which rejects direct HTTP requests even when attempting to replay cookies obtained from the browser.

Because of this, document downloads are performed using an in-page:

```javascript
fetch();
```

The fetch runs inside the same browser session that was already established during scraping.

Therefore, the browser must remain alive across both stages:

```text
Scraping
   │
   ▼
Established Browser Session
   │
   ▼
In-Page Document Downloads
   │
   ▼
driver.quit()
```

---

# 1. Scrape — `scraper.py`

For each page configured under:

```yaml
mca.pages
```

Currently, this contains one URL.

## Scraping Flow

### Step 1: Load the Page

```python
driver.get(url)
```

The scraper waits for up to **25 seconds** for:

```css
.doc-link a
```

to become available.

### Step 2: Allow the Page to Settle

After the element becomes available, the scraper waits an additional **6 seconds**.

This delay is intentional.

The MCA site uses AEM rendering followed by pagination JavaScript that rewrites the document panel.

Reading the DOM too early can result in capturing a **half-built document list**.

### Step 3: Extract Both Document Regions in One DOM Operation

A single:

```python
execute_script()
```

call extracts both document regions simultaneously:

```css
.doc-link a
```

and:

```css
div.marquee a
```

These represent:

- The dated document panel
- The ticker / marquee region

Using a single JavaScript operation is important because the DOM nodes can become stale during iterative traversal.

### Step 4: Recovery if the Panel Is Missing

If the document panel does not render correctly:

```text
Reload page
    │
    ├── Attempt 1
    ├── Attempt 2
    └── Attempt 3
```

The scraper retries up to **3 times**.

After the retry limit is reached, it continues with whatever content successfully rendered.

### Step 5: Filter and Deduplicate

The extracted links are then processed as follows:

- Keep only real `.pdf` paths
- Remove site furniture such as:

```text
Website_Policies.pdf
```

- Deduplicate documents by URL

## Output

The scraper produces a list of:

```text
ScrapedDocument
```

Each document contains:

```text
url
title
page
region
date
```

Conceptually:

```python
ScrapedDocument(
    url=...,
    title=...,
    page=...,
    region=...,
    date=...
)
```

---

# 2. Download — `downloader.py`

Each candidate document is processed sequentially.

## Download Flow

### Step 1: Enforce the Per-Run Limit

Processing stops when:

```text
max_new_documents_per_run = 100
```

is reached.

The limit is enforced directly in code rather than relying on the scraper.

```text
Candidate Documents
        │
        ▼
Process Sequentially
        │
        ├── 1
        ├── 2
        ├── ...
        └── 100 → Stop
```

---

### Step 2: Check the Manifest

Before downloading:

```python
manifest.already_known(url)
```

is checked.

If the document has genuinely completed processing, it is skipped.

Retryable failures are **not** considered complete.

This distinction is central to the system's idempotency.

---

### Step 3: Record Discovery and Reset Stale Failures

For documents that should be processed:

```text
record_discovered()
```

is called.

Then:

```text
reset_for_retry()
```

clears any stale failure state from a previous run.

---

### Step 4: Download Through the Browser Session

The download is performed using:

```text
_fetch_via_browser()
```

Internally, this executes:

```javascript
fetch();
```

inside the active browser page.

The PDF bytes are returned as Base64.

```text
Browser Session
      │
      ▼
In-Page fetch()
      │
      ▼
PDF Bytes
      │
      ▼
Base64
      │
      ▼
Python Downloader
```

This avoids Akamai rejecting direct HTTP requests.

---

### Step 5: Verify PDF Content

A `.pdf` extension alone is not trusted.

The downloaded bytes must begin with the PDF magic bytes:

```text
%PDF-
```

If the content does not match, the download is treated as invalid.

---

### Step 6: Calculate SHA-256

A SHA-256 hash is generated for the document.

The manifest then checks:

```python
hash_already_downloaded(hash, url)
```

The current URL is excluded from the duplicate check.

Without this exclusion, retrying the same document would incorrectly detect itself as a duplicate.

---

### Step 7: Generate a Safe Filename

The filename is sanitized before writing.

Rules include:

- Safe filename generation
- Filename stem capped at **176 characters**
- `.pdf` extension re-attached afterward
- Collision counter added when necessary

Example:

```text
Document.pdf
Document_1.pdf
Document_2.pdf
```

---

### Step 8: Save and Record

The PDF is written to:

```text
data/raw/
```

Then the manifest records the successful download:

```text
record_downloaded()
```

A politeness delay is applied before continuing to the next document.

---

# 3. Extract → Classify → Organize

Every downloaded document goes through three processing stages.

Each stage updates the manifest independently.

```text
Downloaded PDF
      │
      ▼
┌─────────────┐
│   Extract   │
└──────┬──────┘
       ▼
┌─────────────┐
│  Classify   │
└──────┬──────┘
       ▼
┌─────────────┐
│  Organize   │
└─────────────┘
```

---

# 3.1 Extract — `extractor.py`

The extraction process uses a two-stage strategy.

## Native Text Extraction

First, the system attempts extraction using:

```text
pdfplumber
```

This is:

- Fast
- Free
- Suitable for digitally generated PDFs

## OCR Fallback

If the extracted text contains fewer than:

```text
60 characters
```

the PDF is considered likely to be a scanned document.

The system then switches to OCR.

### OCR Pipeline

```text
PDF
 │
 ▼
PyMuPDF
 │
 ▼
Render Pages at 300 DPI
 │
 ▼
Tesseract OCR
 │
 ▼
Extracted Text
```

The extractor returns one of:

```text
native
ocr
failed
```

---

# 3.2 Classify — `classifier.py`

Document classification is handled using **Gemini**.

The model response is constrained using a:

```text
response_schema
```

with an enum.

This means the model can only return one of the allowed categories.

```text
Allowed Categories
        │
        ▼
Gemini Classification
        │
        ▼
One Valid Category
```

## Error Handling

The classifier:

- Throttles requests
- Backs off when receiving `429` responses
- Raises:

```text
ClassificationError
```

instead of silently returning:

```text
Other
```

This behavior is intentional.

Previously, silently returning `Other` masked an outage and resulted in **16 documents being incorrectly filed**.

A classification failure must remain visible as a failure.

---

# 3.3 Organize — `organizer.py`

Once classified, the document is moved from:

```text
data/raw/x.pdf
```

to:

```text
data/output/<Category>/x.pdf
```

## Category Validation

Before constructing the output path, the category is validated against the configured category list.

This prevents model-generated strings from directly becoming filesystem paths.

```text
Model Category
      │
      ▼
Validate Against Config
      │
      ├── Valid ──► Use Category Directory
      │
      └── Invalid ─► Reject / Fail
```

---

# Classification Failure Behavior

If classification fails, the document is still physically filed under:

```text
Other/
```

However, the manifest failure is recorded **afterward**.

Therefore, the final state remains:

```text
failed_classify
```

instead of incorrectly appearing as successfully completed.

This ensures that the next scheduled run retries classification.

---

# The State Machine

`manifest.py` acts as the persistent memory of the entire system.

```text
discovered
    │
    ▼
downloaded
    │
    ▼
extracted
    │
    ▼
classified
    │
    ▼
organized
```

`organized` is a **terminal state**.

Successfully completed documents are skipped forever.

---

## Failure States

A document may fail at any stage:

```text
discovered
    │
    ├── failed_download
    │
downloaded
    │
    ├── failed_extract
    │
extracted
    │
    ├── failed_classify
    │
classified
    │
    └── failed_organize
```

All of these failure states are:

```text
RETRYABLE
```

On the next run:

```text
already_known() → False
```

Therefore, the document is eligible for processing again.

---

# Duplicate Content

If two different URLs resolve to the same document content:

```text
SHA-256 Match
      │
      ▼
duplicate_content
```

This is a terminal state.

The duplicate document is skipped in future runs.

---

# Complete State Model

```text
                        ┌──────────────────┐
                        │    discovered    │
                        └────────┬─────────┘
                                 │
                 ┌───────────────┴───────────────┐
                 │                               │
                 ▼                               ▼
           downloaded                      failed_download
                 │                               │
                 ▼                               │
           extracted                       RETRYABLE
                 │                               │
                 ▼                               │
           classified                      failed_extract
                 │                               │
                 ▼                               │
           organized ◄──────────────────── failed_classify
                 │                               │
                 ▼                               ▼
              TERMINAL                    failed_organize
                                                  │
                                                  ▼
                                              RETRYABLE


duplicate_content
       │
       ▼
    TERMINAL
```

---

# `already_known()` — The Idempotency Pivot

The most important decision in the entire state system is:

```python
manifest.already_known(url)
```

It returns:

```text
True
```

**only when the document has genuinely completed processing**.

For retryable failures:

```text
failed_download
failed_extract
failed_classify
failed_organize
```

it returns:

```text
False
```

This distinction separates:

> **"We are done with this document."**

from:

> **"We tried, but something failed and we need to try again."**

Conflating these two states was the bug that previously caused the agent to download nothing at all.

---

# End-to-End Behavior

The complete pipeline behaves like this:

```text
┌─────────────────────┐
│ Windows Scheduler   │
│      Every 24h      │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│    run_task.bat     │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│       main.py       │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│   Load config.yaml  │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ Open SQLite Manifest│
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ Build Headless      │
│ Chrome              │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ Scrape MCA Pages    │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ Download Documents  │
│ Inside Browser      │
│ Session             │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│    driver.quit()    │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ Extract Documents   │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ Classify Documents  │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ Organize Documents  │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ Update Manifest     │
└─────────────────────┘
```

---

# Final Result: Idempotent and Self-Healing

The pipeline is designed to converge over repeated executions.

Running it any number of times produces the following behavior:

- **New documents** → discovered and processed
- **Completed documents** → skipped
- **Failed documents** → retried
- **Duplicate content** → detected and skipped
- **Successful work** → never processed unnecessarily again

In short:

> **Run it once, run it daily, or run it repeatedly — the system converges toward a fully processed and consistent state.**

The SQLite manifest provides the memory, the state machine provides retry behavior, and the single persistent browser session allows the system to reliably work within MCA's Akamai-protected environment.
