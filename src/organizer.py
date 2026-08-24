"""
Moves a classified PDF from the raw staging folder into
output_dir/<Category>/filename.pdf

The category value here has already been validated against the
configured allow-list in classifier.py before it ever reaches this
function — this module additionally re-checks against the same list
before creating a folder from it, so a folder path is never built
directly from unvalidated model output even if this function is
called from somewhere else in the future.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger("mca_agent.organizer")


def organize(cfg, raw_path: Path, category: str) -> Path:
    allowed_categories = cfg.get("categories", default=["Other"])
    safe_category = category if category in allowed_categories else "Other"

    dest_folder = cfg.output_dir / safe_category
    dest_folder.mkdir(parents=True, exist_ok=True)

    dest_path = dest_folder / raw_path.name
    counter = 1
    while dest_path.exists():
        dest_path = dest_folder / f"{raw_path.stem}_{counter}{raw_path.suffix}"
        counter += 1

    shutil.move(str(raw_path), str(dest_path))
    logger.info(f"Filed -> {safe_category}/{dest_path.name}")
    return dest_path
