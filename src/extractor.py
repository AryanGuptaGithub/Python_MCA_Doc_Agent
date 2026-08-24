"""
Extracts enough text from each PDF to classify it.

Strategy (per your choice): try native text extraction first (fast, free,
accurate — most MCA Act/Rules/Circular PDFs are digitally generated, not
scanned). Only fall back to OCR when native extraction comes back mostly
empty, which is the signature of a scanned/image-only PDF.

Both paths are capped to the first few pages (configurable) — classifying
a document doesn't need its full text, and capping keeps OCR (the slow
path) fast and keeps what we send to Gemini small.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pdfplumber

logger = logging.getLogger("mca_agent.extractor")


@dataclass
class ExtractionResult:
    text: str
    method: str  # 'native' | 'ocr' | 'failed'


def _extract_native(pdf_path: Path, max_pages: int) -> str:
    chunks = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:max_pages]:
            page_text = page.extract_text() or ""
            chunks.append(page_text)
    return "\n".join(chunks).strip()


def _find_tesseract(configured: str) -> str:
    """
    Locates tesseract.exe: an explicit config value wins, then PATH, then the
    standard Windows install locations.

    The fallback list matters because the UB-Mannheim installer (the standard
    Windows build) does NOT add itself to PATH, so a correct install would
    otherwise still look "missing" and silently downgrade every scanned
    document to title-only classification.
    """
    if configured:
        return configured

    found = shutil.which("tesseract")
    if found:
        return found

    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tesseract-OCR" / "tesseract.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return ""


def _extract_ocr(cfg, pdf_path: Path, max_pages: int) -> str:
    """
    Rasterizes the first `max_pages` pages and runs Tesseract over them.

    Rendering uses PyMuPDF rather than pdf2image because pdf2image shells out
    to Poppler, a separate system install that is not on PATH by default on
    Windows — that was the actual cause of "Unable to get page count. Is
    poppler installed and in PATH?". PyMuPDF is a self-contained wheel, so
    Tesseract is now the only external program OCR needs.
    """
    try:
        import pymupdf  # PyMuPDF >= 1.24.3 module name
    except ImportError:  # older wheels only expose the legacy `fitz` name
        import fitz as pymupdf
    import pytesseract
    from PIL import Image

    configured = cfg.get("extraction", "ocr", "tesseract_cmd", default="") or ""
    tesseract_cmd = _find_tesseract(configured)
    if not tesseract_cmd:
        raise RuntimeError(
            "Tesseract not found. Install it (winget install UB-Mannheim.TesseractOCR) "
            "or set extraction.ocr.tesseract_cmd in config.yaml"
        )
    pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    dpi = cfg.get("extraction", "ocr", "render_dpi", default=300)
    lang = cfg.get("extraction", "ocr", "language", default="eng")
    # 72 is PDF user-space DPI, so this scales the page up to the target DPI.
    zoom = pymupdf.Matrix(dpi / 72, dpi / 72)

    chunks = []
    with pymupdf.open(pdf_path) as doc:
        for page in doc[:max_pages]:
            pixmap = page.get_pixmap(matrix=zoom)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            chunks.append(pytesseract.image_to_string(image, lang=lang))
    return "\n".join(chunks).strip()


def extract_text(cfg, pdf_path: Path) -> ExtractionResult:
    max_pages = cfg.get("extraction", "max_pages_to_read", default=5)
    min_chars = cfg.get("extraction", "min_native_chars_threshold", default=60)
    ocr_enabled = cfg.get("extraction", "ocr", "enabled", default=True)
    ocr_max_pages = cfg.get("extraction", "ocr", "ocr_max_pages", default=3)

    try:
        native_text = _extract_native(pdf_path, max_pages)
    except Exception as e:
        logger.warning(f"Native extraction failed for {pdf_path.name}: {e}")
        native_text = ""

    if len(native_text) >= min_chars:
        return ExtractionResult(text=native_text, method="native")

    logger.info(
        f"{pdf_path.name}: native extraction returned {len(native_text)} chars "
        f"(< threshold {min_chars}) -> looks scanned, trying OCR"
    )

    if not ocr_enabled:
        logger.warning(f"{pdf_path.name}: OCR disabled in config, skipping (will classify as best-effort)")
        return ExtractionResult(text=native_text, method="failed" if not native_text else "native")

    try:
        ocr_text = _extract_ocr(cfg, pdf_path, ocr_max_pages)
    except Exception as e:
        logger.error(f"OCR failed for {pdf_path.name}: {e}")
        return ExtractionResult(text=native_text, method="failed")

    if not ocr_text.strip():
        return ExtractionResult(text="", method="failed")

    return ExtractionResult(text=ocr_text, method="ocr")
