# Running, Testing, and Verifying the MCA Document Processing Agent

## 1. Normal Run — Scheduled Execution

This is the standard execution flow used by **Windows Task Scheduler**.

```powershell
cd C:\Users\aryan\Desktop\mca_agent

venv\Scripts\activate

python -m src.main
```

Alternatively, simply run:

```powershell
.\run_task.bat
```

The batch file activates the virtual environment and runs the same command.

### Expected Result

Currently, this run should effectively be a **no-op**.

All 16 documents have already been successfully processed and organized, so the agent should:

1. Scrape the configured MCA pages.
2. Discover the existing document URLs.
3. Check the manifest.
4. Recognize that all documents are already completed.
5. Skip downloading and processing them.
6. Exit normally.

The expected output should include something similar to:

```text
Downloaded 0 new document(s)
```

This is itself an important **idempotency check**.

Running the agent multiple times should not cause already completed documents to be downloaded, classified, or organized again.

---

# 2. Full Clean-Slate Run — Complete End-to-End Test

To test the entire pipeline from scratch, the existing processing state must first be cleared.

This allows the system to perform the complete flow again:

```text
Scrape
  ↓
Download
  ↓
Verify PDF
  ↓
Hash
  ↓
Extract
  ↓
OCR if required
  ↓
Classify with Gemini
  ↓
Organize
  ↓
Update Manifest
```

## Step 1: Navigate to the Project

```powershell
cd C:\Users\aryan\Desktop\mca_agent
```

## Step 2: Activate the Virtual Environment

```powershell
venv\Scripts\activate
```

## Step 3: Back Up the Existing Manifest

Before clearing the state, create a backup:

```powershell
copy data\manifest.sqlite3 data\manifest.backup.sqlite3
```

## Step 4: Remove the Current Manifest

```powershell
del data\manifest.sqlite3
```

## Step 5: Clear Processed Output Files

```powershell
Remove-Item data\output\* -Recurse -Force -Exclude .gitkeep
```

## Step 6: Run the Agent

```powershell
python -m src.main
```

---

## Important: This Is Destructive

A clean-slate run removes the existing processing state.

The agent will then:

- Re-discover all documents.
- Re-download all 16 PDFs.
- Extract text from each document.
- Run OCR where necessary.
- Classify each document using Gemini.
- Organize the documents again.

This will result in approximately:

```text
16 Gemini API calls
```

and may take a few minutes to complete.

### Restoring the Previous State

If you want to restore the old manifest afterward:

```powershell
copy data\manifest.backup.sqlite3 data\manifest.sqlite3
```

---

# 3. Checking the Result

After the run completes, inspect the manifest database with:

```powershell
python -c "import sqlite3;c=sqlite3.connect('data/manifest.sqlite3');[print(r) for r in c.execute('SELECT status,extraction_method,category,COUNT(*) FROM documents GROUP BY status,extraction_method,category')]"
```

## Expected Healthy Result

A successful run should show:

- Every document with the status:

```text
organized
```

- `extraction_method` should only contain:

```text
native
```

or:

```text
ocr
```

- There should be **no**:

```text
failed
```

or:

```text
NULL
```

extraction methods.

- The total document count should be:

```text
16
```

---

# 4. Detailed Logs

Detailed execution logs are stored under:

```text
logs/run_<timestamp>.log
```

These logs contain the full execution history for a specific run and can be used to investigate:

- Scraping failures
- Download failures
- PDF validation failures
- OCR execution
- Gemini classification issues
- Retry behavior
- File organization errors

---

# 5. Testing One Component in Isolation

## OCR Test — No Network or Gemini Calls

The extraction pipeline can be tested independently without:

- Network access
- Browser execution
- MCA scraping
- Gemini API calls

Run:

```powershell
python -c "from src.config_loader import load_config; from src.extractor import extract_text; from pathlib import Path; r=extract_text(load_config(), Path(r'data\output\Circular\Observance_of_Vigilance_Awareness_Week__2025__27_th_October__2025_to_2nd_November__2025_Providing_hyperlink_of_e-pledge_on_Ministry_s_website.pdf')); print(r.method, len(r.text))"
```

## Expected Output

```text
ocr 802
```

This confirms that:

1. The PDF is accessible.
2. Native extraction determines that the document requires OCR.
3. The OCR fallback executes successfully.
4. Text is extracted successfully.
5. The extracted result contains approximately 802 characters.

---

# 6. Current Testing Gap

Everything verified so far has been tested manually and in an ad-hoc manner.

The following behaviors have been checked:

- Manifest retry semantics
- Retry behavior after failures
- Hash deduplication during retries
- Filename truncation
- Filename collision handling
- OCR fallback behavior
- Extraction flow

However, these checks are **not currently stored as automated tests**.

That means a future code change could introduce a regression without anything automatically detecting it.

For example:

```text
Code Change
     │
     ▼
Manifest Logic Changes
     │
     ▼
already_known() Accidentally Changes
     │
     ▼
Retry Behavior Breaks
     │
     ▼
No Automated Test Detects It
```

The issue would only be discovered through manual testing or after the scheduled agent behaves incorrectly.

---

# 7. Recommended Next Step — Add a Proper Pytest Suite

A proper automated test suite should cover the critical offline logic.

## Fast Offline Tests

The following areas should be covered with `pytest`:

### Manifest State Logic

- Successfully organized documents are skipped.
- Retryable failures are processed again.
- `already_known()` only returns `True` for genuinely completed documents.
- Failed download states remain retryable.
- Failed extraction states remain retryable.
- Failed classification states remain retryable.
- Failed organization states remain retryable.

### Hash Deduplication

- Identical files from different URLs are detected.
- A document does not flag itself as a duplicate during a retry.
- Duplicate content becomes a terminal state.

### Filename Logic

- Unsafe characters are sanitized.
- Long filenames are truncated safely.
- The filename stem respects the configured maximum length.
- `.pdf` is preserved after truncation.
- Filename collisions generate incrementing suffixes.

### Extraction and OCR Logic

Where practical, tests should verify:

- Native extraction detection.
- OCR fallback threshold behavior.
- Failed extraction handling.

These tests should be:

```text
Fast
Offline
Repeatable
No Browser
No Network
No Gemini API Calls
```

---

# 8. Optional Live End-to-End Test

In addition to the offline test suite, an **opt-in integration test** can be created for the complete pipeline.

The live test could validate:

```text
MCA Website
    ↓
Chrome / Akamai Session
    ↓
Scraping
    ↓
Browser-Based Download
    ↓
PDF Verification
    ↓
Extraction / OCR
    ↓
Gemini Classification
    ↓
Organization
    ↓
Manifest Verification
```

Because this test would involve:

- Network access
- The live MCA website
- Chrome
- Potential Akamai behavior
- Gemini API usage

it should **not run by default**.

Instead, it should require an explicit command or marker, such as:

```powershell
pytest
```

for the normal offline suite, and:

```powershell
pytest -m live
```

for the optional live end-to-end test.

---

# Recommended Testing Structure

A clean structure could look like:

```text
mca_agent/
│
├── src/
│   ├── main.py
│   ├── manifest.py
│   ├── scraper.py
│   ├── downloader.py
│   ├── extractor.py
│   ├── classifier.py
│   └── organizer.py
│
├── tests/
│   ├── test_manifest.py
│   ├── test_deduplication.py
│   ├── test_filenames.py
│   ├── test_extractor.py
│   └── test_end_to_end_live.py
│
├── data/
├── logs/
├── pytest.ini
└── requirements.txt
```

---

# Final Testing Workflow

The ideal workflow becomes:

## Normal Development Validation

```powershell
pytest
```

This runs the fast, offline test suite.

## Optional Live Validation

```powershell
pytest -m live
```

This performs the real integration test and can verify the full production-like pipeline.

## Normal Scheduled Run

```powershell
.\run_task.bat
```

Windows Task Scheduler then continues to run the agent every 24 hours.

---

# Summary

The agent currently works as an idempotent scheduled pipeline:

```text
Scheduler
   ↓
run_task.bat
   ↓
main.py
   ↓
Scrape
   ↓
Skip Completed Documents
   ↓
Retry Failed Documents
   ↓
Process New Documents
   ↓
Exit
```

A normal run currently acts as a **no-op verification**, with all 16 documents already organized.

A clean-slate run provides the complete end-to-end test but is destructive and consumes Gemini API calls.

The remaining improvement is to convert the currently manual verification work into a proper **pytest suite**, giving the project a repeatable regression safety net:

```powershell
pytest
```

for fast offline validation, plus an opt-in:

```powershell
pytest -m live
```

for a real end-to-end test against the live system.
