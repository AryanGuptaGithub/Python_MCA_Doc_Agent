"""
Downloads discovered PDFs, enforcing:
  - the configured max-new-documents-per-run cap (unbounded-download
    protection — enforced in code, not left as an instruction the scraper
    "should" follow)
  - a real PDF signature check before trusting a downloaded file (a scraped
    .pdf-looking link isn't guaranteed to actually be one)
  - dedup both by source URL (via the manifest) and by content hash (in case
    the same document is linked from two different pages/URLs)

WHY DOWNLOADS GO THROUGH THE BROWSER
mca.gov.in is behind Akamai Bot Manager. Three approaches were tested
against the live site:

  1. plain `requests.get(pdf_url)`                     -> 403 Forbidden
  2. `requests` + the browser's cookies + Referer      -> 403 Forbidden
  3. `fetch()` executed INSIDE the Selenium browser    -> 200 OK, valid PDF

Approach 2 is the intuitive fix and it does not work: Akamai fingerprints the
TLS/HTTP2 handshake, so a `requests` session is identifiable as non-browser no
matter which cookies it carries (the relevant cookies here are `ak_bmsc` and
`bm_sv`, both Akamai's). Only a genuine browser request is served, so we run
the download as an in-page `fetch()` and hand the bytes back to Python as
base64. That keeps the exact TLS stack, cookie jar and header order the site
already accepted during scraping.

The base64 round-trip costs ~33% memory overhead per document, which is fine
for MCA circulars (tens to hundreds of KB); `max_download_mb` guards against
a pathologically large file blowing up the browser tab.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import time
from pathlib import Path

from .manifest import Manifest
from .scraper import ScrapedDocument

logger = logging.getLogger("mca_agent.downloader")

PDF_MAGIC = b"%PDF-"

# Runs in the page's own context, so it inherits the session Akamai accepted.
# Chunked String.fromCharCode avoids blowing the argument limit on big files.
_FETCH_JS = """
const url = arguments[0], maxBytes = arguments[1], done = arguments[arguments.length - 1];
fetch(url, {credentials: 'include'})
  .then(r => {
    if (!r.ok) { done({ok: false, status: r.status}); return null; }
    return r.arrayBuffer().then(buf => {
      const bytes = new Uint8Array(buf);
      if (bytes.length > maxBytes) {
        done({ok: false, status: r.status, tooBig: bytes.length});
        return;
      }
      let binary = '';
      const CHUNK = 0x8000;
      for (let i = 0; i < bytes.length; i += CHUNK) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
      }
      done({ok: true, status: r.status, b64: btoa(binary), len: bytes.length});
    });
  })
  .catch(e => done({ok: false, err: String(e)}));
"""


def _safe_filename(title: str, source_url: str) -> str:
    base = title.strip() or source_url.rsplit("/", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9._\- ]+", "_", base).strip()
    base = re.sub(r"\s+", "_", base)
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    # Truncate the STEM, then re-attach the extension. Truncating afterwards
    # chopped ".pdf" off any long title, leaving extensionless files that
    # Windows and the extractor both refuse to treat as PDFs.
    return base[:176] + ".pdf"  # keep filesystem-safe length


def _fetch_via_browser(driver, url: str, max_bytes: int) -> bytes:
    """
    Fetches `url` from inside the browser and returns its bytes.
    Raises RuntimeError with a readable reason on any failure.
    """
    result = driver.execute_async_script(_FETCH_JS, url, max_bytes)
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected fetch result: {result!r}")
    if not result.get("ok"):
        if result.get("tooBig"):
            raise RuntimeError(
                f"file is {result['tooBig']} bytes, over the max_download_mb limit"
            )
        if result.get("err"):
            raise RuntimeError(f"browser fetch error: {result['err']}")
        raise RuntimeError(f"HTTP {result.get('status')} from site")
    try:
        return base64.b64decode(result["b64"])
    except (binascii.Error, KeyError) as e:
        raise RuntimeError(f"could not decode downloaded bytes: {e}") from e


def download_new_documents(
    cfg,
    manifest: Manifest,
    candidates: list[ScrapedDocument],
    driver,
) -> list[dict]:
    """
    Downloads up to run.max_new_documents_per_run NEW documents (skipping
    anything already completed per the manifest). Returns a list of dicts
    describing each successfully downloaded document, ready for extraction.

    `driver` must be the live Selenium driver used for scraping — see the
    module docstring for why downloads cannot use `requests` here.
    """
    max_new = cfg.get("run", "max_new_documents_per_run", default=100)
    delay = cfg.get("run", "download_delay_seconds", default=1.0)
    max_bytes = int(cfg.get("run", "max_download_mb", default=50) * 1024 * 1024)
    raw_dir: Path = cfg.raw_dir

    downloaded: list[dict] = []
    skipped_known = 0

    for doc in candidates:
        if len(downloaded) >= max_new:
            logger.info(
                f"Reached max_new_documents_per_run cap ({max_new}). Stopping downloads."
            )
            break

        if manifest.already_known(doc.source_url):
            skipped_known += 1
            continue  # completed in a previous run

        # Either brand new, or a previous run failed on it — in the latter case
        # clear the stale failure so it re-enters the pipeline cleanly.
        manifest.record_discovered(doc.source_url, doc.source_page, doc.title)
        manifest.reset_for_retry(doc.source_url)

        try:
            content = _fetch_via_browser(driver, doc.source_url, max_bytes)
        except Exception as e:
            logger.warning(f"Download failed for {doc.source_url}: {e}")
            manifest.record_failure(doc.source_url, "failed_download", str(e))
            continue

        if not content.startswith(PDF_MAGIC):
            msg = "Downloaded content is not a valid PDF (missing %PDF signature)"
            logger.warning(f"{msg}: {doc.source_url}")
            manifest.record_failure(doc.source_url, "failed_download", msg)
            continue

        file_hash = hashlib.sha256(content).hexdigest()
        if manifest.hash_already_downloaded(file_hash, doc.source_url):
            logger.info(f"Duplicate content (same PDF under another URL): {doc.source_url}")
            manifest.record_failure(
                doc.source_url, "duplicate_content", "same file_hash as an existing document"
            )
            continue

        # avoid collisions if two different docs produce the same safe filename
        dest = raw_dir / _safe_filename(doc.title, doc.source_url)
        counter = 1
        while dest.exists():
            dest = raw_dir / f"{dest.stem}_{counter}{dest.suffix}"
            counter += 1

        dest.write_bytes(content)
        manifest.record_downloaded(doc.source_url, str(dest), file_hash)
        logger.info(
            f"Downloaded ({len(downloaded) + 1}/{max_new}): {dest.name} "
            f"({len(content):,} bytes)"
        )

        downloaded.append(
            {
                "source_url": doc.source_url,
                "title": doc.title,
                "source_page": doc.source_page,
                "raw_path": dest,
                "file_hash": file_hash,
            }
        )

        time.sleep(delay)

    if skipped_known:
        logger.info(f"Skipped {skipped_known} document(s) already processed in earlier runs")
    return downloaded
