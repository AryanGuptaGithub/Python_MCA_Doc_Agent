"""
MCA Document Agent — single-run orchestrator.

This script does ONE full pass:
  1. Scrape MCA's Acts/Rules/Notifications/Circulars pages for PDF links
  2. Download up to `run.max_new_documents_per_run` NEW documents (dedup via manifest)
  3. Extract text from each (native first, OCR fallback for scanned PDFs)
  4. Classify each with Gemini into one of the configured categories
  5. File each into output_dir/<Category>/

Scheduling ("run every 24 hours") is intentionally NOT done inside this
script — see run_task.bat / README.md. A script that loops-and-sleeps
for 24h is fragile (dies on any crash, doesn't survive a reboot, no
visibility into whether yesterday's run actually happened). Windows Task
Scheduler already solves exactly this problem, so we let it own the
schedule and this script just does one clean run per invocation.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

# Allow running as `python src/main.py` from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_config, get_gemini_api_key
from src.logger_setup import setup_logging
from src.manifest import Manifest
from src.scraper import build_driver, scrape_all_pages
from src.downloader import download_new_documents
from src.extractor import extract_text
from src.classifier import Classifier
from src.organizer import organize


def run(config_path: str = None) -> int:
    cfg = load_config(config_path)
    logger = setup_logging(cfg.logs_dir)
    run_started_at = datetime.now().isoformat()

    logger.info("=" * 70)
    logger.info("MCA Document Agent — run starting")
    logger.info(f"Project root: {cfg.project_root}")
    logger.info(f"Output dir:   {cfg.output_dir}")
    logger.info("=" * 70)

    manifest = Manifest(cfg.manifest_db)

    pending_retries = manifest.retryable_count()
    if pending_retries:
        logger.info(f"{pending_retries} document(s) failed previously and will be retried")

    # --- 1. Scrape + 2. Download ---------------------------------------
    # Both stages share ONE browser: mca.gov.in is behind Akamai, which
    # rejects plain `requests` calls even when handed the browser's cookies,
    # so the PDFs have to be fetched from inside the live browser session
    # that scraping already established (see downloader.py).
    driver = None
    try:
        try:
            driver = build_driver(cfg)
        except Exception as e:
            logger.critical(f"Could not start the browser: {e}", exc_info=True)
            return 1

        try:
            candidates = scrape_all_pages(cfg, driver)
        except Exception as e:
            logger.critical(f"Scraping failed entirely: {e}", exc_info=True)
            return 1
        logger.info(f"Discovered {len(candidates)} PDF document(s) across all configured pages")

        downloaded = download_new_documents(cfg, manifest, candidates, driver)
        logger.info(f"Downloaded {len(downloaded)} new document(s) this run")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    if not downloaded:
        logger.info("Nothing new to process. Run complete.")
        _print_summary(logger, manifest, run_started_at)
        return 0

    # --- 3 & 4. Extract + classify + 5. organize, per document ----------
    api_key = get_gemini_api_key(cfg)
    classifier = Classifier(cfg, api_key)

    for i, doc in enumerate(downloaded, start=1):
        source_url = doc["source_url"]
        raw_path: Path = doc["raw_path"]
        title = doc["title"]

        logger.info(f"[{i}/{len(downloaded)}] Processing: {raw_path.name}")

        try:
            extraction = extract_text(cfg, raw_path)
            manifest.record_extracted(source_url, extraction.method)
        except Exception as e:
            logger.error(f"Extraction failed for {raw_path.name}: {e}", exc_info=True)
            manifest.record_failure(source_url, "failed_extract", str(e))
            continue

        classify_error = None
        try:
            category, reasoning = classifier.classify(title, extraction.text)
            manifest.record_classified(source_url, category)
            logger.info(f"  -> classified as '{category}' ({reasoning})")
        except Exception as e:
            logger.error(f"Classification failed for {raw_path.name}: {e}", exc_info=True)
            classify_error = str(e)
            category = "Other"  # still file it rather than leaving it stuck in raw/

        try:
            final_path = organize(cfg, raw_path, category)
            manifest.record_organized(source_url, str(final_path))
        except Exception as e:
            logger.error(f"Filing failed for {raw_path.name}: {e}", exc_info=True)
            manifest.record_failure(source_url, "failed_organize", str(e))
            continue

        if classify_error is not None:
            # The file is safely on disk under Other/, but the run did NOT
            # genuinely categorize it. Record the failure LAST so the final
            # status reflects that, and the next run retries the
            # classification instead of silently accepting "Other" forever.
            manifest.record_failure(source_url, "failed_classify", classify_error)

    _print_summary(logger, manifest, run_started_at)
    logger.info("Run complete.")
    return 0


def _print_summary(logger, manifest: Manifest, since_iso: str) -> None:
    rows = manifest.run_summary(since_iso)
    if not rows:
        return
    logger.info("--- Run summary ---")
    for row in rows:
        cat = row["category"] or "-"
        logger.info(f"  status={row['status']:<12} category={cat:<12} count={row['n']}")


if __name__ == "__main__":
    exit_code = run()
    sys.exit(exit_code)