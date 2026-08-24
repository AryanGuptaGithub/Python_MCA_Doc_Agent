"""
Scrapes MCA's listing pages for downloadable PDF documents.

Why Selenium and not requests+BeautifulSoup: mca.gov.in sits behind Akamai
Bot Manager. A plain HTTP GET is answered with 403 Forbidden — and, verified
by testing, replaying the browser's cookies into a `requests` session does
NOT help, because Akamai fingerprints the TLS/HTTP2 stack itself, not just
the cookie jar. Only a real browser gets served. That is also why downloads
happen through the browser (see downloader.py) rather than over `requests`.

WHERE THE LINKS ACTUALLY COME FROM (verified against the live site):
MCA's pages are Adobe AEM. The document links live in two rendered regions:

  * .doc-link > a        — the "Important Updates / What's New" panel. This is
                           the real, dated document list (each entry is paired
                           with a sibling .doc-date).
                           NOTE the selector is deliberately tag-agnostic: MCA
                           has served this element as BOTH <span class="doc-link">
                           and <div class="doc-link">. Pinning it to one tag
                           silently dropped the entire panel when they switched,
                           leaving only the marquee ticker.
  * div.marquee a        — the scrolling ticker of current notices.

Both regions are SITE-WIDE: they render identically on every MCA page. That
was verified across six different URLs — acts-rules, ebooks, ebooks/notifications,
notifications-tender/circulars, notices-circulars and whats-new all returned the
exact same link set. MCA's per-category tab component (the tabs labelled
"Notice & Circular" / "Circulars") renders "No results found." for an
anonymous headless session, so there is no additional per-category data to
scrape. Consequently, configuring several URLs adds nothing but runtime —
config.yaml is set to a single page for that reason, and the cross-page dedup
below is what makes adding more pages harmless if you want to try others.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger("mca_agent.scraper")

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# Pure site furniture — published as PDFs, but they are website policy pages
# rather than MCA regulatory documents, so they are not worth downloading or
# spending a Gemini classification call on.
_SITE_FURNITURE_SLUGS = (
    "website_policies",
    "terms_and_conditions",
    "privacy_policy",
    "copyright_policy",
    "hyperlinking_policy",
    "accessibility",
    "disclaimer",
)

# Reads both document regions out of the rendered DOM in one hop. Doing this
# in a single execute_script (rather than a Python loop over Selenium
# elements) matters: the page re-renders these nodes as its pagination JS
# settles, and an element-by-element walk trips over
# StaleElementReferenceException partway through.
_COLLECT_JS = """
const out = [];
const push = (a, region) => {
  const href = a.getAttribute('href');
  if (!href) return;
  const p = a.querySelector('p');
  const title = ((p ? p.textContent : a.textContent) || '').trim();
  // the doc-date element is a sibling of the .doc-link wrapper
  let date = '';
  const wrap = a.closest('.doc-link');
  if (wrap && wrap.parentElement) {
    const d = wrap.parentElement.querySelector('.doc-date');
    if (d) date = (d.textContent || '').trim();
  }
  out.push({href: href, title: title, region: region, date: date});
};
document.querySelectorAll('.doc-link a[href]').forEach(a => push(a, 'doc-link'));
document.querySelectorAll('div.marquee a[href]').forEach(a => push(a, 'marquee'));
return out;
"""


@dataclass
class ScrapedDocument:
    source_url: str    # absolute URL to the PDF
    title: str         # document title as shown on the page
    source_page: str   # which configured page this came from (name)
    page_url: str      # the listing page it was found on (used as Referer)
    region: str        # 'doc-link' or 'marquee' — which part of the page it came from
    doc_date: str      # publication date shown next to the link, if any


def build_driver(cfg) -> webdriver.Chrome:
    """
    Builds the headless Chrome used for BOTH scraping and downloading.

    The same driver is reused for downloads because the live browser session
    is what satisfies Akamai — see downloader.py.
    """
    headless = cfg.get("scraper", "headless", default=True)
    chromedriver_path = cfg.get("scraper", "chromedriver_path", default="") or ""

    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={BROWSER_USER_AGENT}")

    if chromedriver_path:
        from selenium.webdriver.chrome.service import Service
        service = Service(executable_path=chromedriver_path)
        driver = webdriver.Chrome(service=service, options=options)
    else:
        # No explicit driver path: let Selenium Manager (bundled since Selenium
        # 4.6) resolve a driver matching the Chrome actually installed here.
        driver = webdriver.Chrome(options=options)

    # Downloads run as async scripts inside this driver, so the script timeout
    # has to comfortably exceed the per-document HTTP timeout.
    driver.set_script_timeout(cfg.get("run", "http_timeout_seconds", default=30) + 90)
    return driver


def _is_pdf(href: str) -> bool:
    return urlparse(href).path.lower().endswith(".pdf")


def _is_site_furniture(href: str) -> bool:
    name = urlparse(href).path.rsplit("/", 1)[-1].lower()
    return any(slug in name for slug in _SITE_FURNITURE_SLUGS)


def scrape_all_pages(cfg, driver) -> list[ScrapedDocument]:
    """
    Visits every page configured under mca.pages and returns every PDF
    document link found, de-duplicated across pages.
    """
    pages = cfg.get("mca", "pages", default=[])
    wait_seconds = cfg.get("mca", "page_load_wait_seconds", default=20)
    wait_selector = cfg.get("mca", "content_wait_selector", default=".doc-link a")
    settle_seconds = cfg.get("mca", "settle_seconds", default=6)
    logs_dir: Path = cfg.logs_dir

    all_docs: list[ScrapedDocument] = []
    for page in pages:
        name, url = page["name"], page["url"]
        logger.info(f"Scraping '{name}' -> {url}")
        try:
            docs = _scrape_one_page(
                driver, name, url, wait_seconds, wait_selector, settle_seconds, logs_dir
            )
        except (TimeoutException, WebDriverException) as e:
            logger.error(f"  failed to scrape '{name}': {e}")
            continue

        by_region: dict[str, int] = {}
        for d in docs:
            by_region[d.region] = by_region.get(d.region, 0) + 1
        detail = ", ".join(f"{k}={v}" for k, v in sorted(by_region.items())) or "none"
        logger.info(f"  found {len(docs)} PDF document(s) on '{name}' ({detail})")
        if not docs:
            logger.warning(
                f"  0 documents on '{name}' — diagnostic HTML/screenshot written to {logs_dir}"
            )
        all_docs.extend(docs)

    seen: set[str] = set()
    unique_docs: list[ScrapedDocument] = []
    for d in all_docs:
        if d.source_url not in seen:
            seen.add(d.source_url)
            unique_docs.append(d)

    dropped = len(all_docs) - len(unique_docs)
    if dropped:
        logger.info(
            f"Dropped {dropped} duplicate link(s) — MCA renders the same document "
            f"panel on every page"
        )
    return unique_docs


def _dump_diagnostics(driver, name: str, logs_dir: Path) -> None:
    """Saves rendered HTML + screenshot so selectors can be checked against the real DOM."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    try:
        (logs_dir / f"debug_{name}_{timestamp}.html").write_text(
            driver.page_source, encoding="utf-8"
        )
        driver.save_screenshot(str(logs_dir / f"debug_{name}_{timestamp}.png"))
        logger.info(f"  diagnostics written: debug_{name}_{timestamp}.html / .png")
    except Exception as e:
        logger.warning(f"  could not save diagnostics for '{name}': {e}")


def _scrape_one_page(
    driver,
    name: str,
    url: str,
    wait_seconds: int,
    wait_selector: str,
    settle_seconds: int,
    logs_dir: Path,
    attempts: int = 3,
) -> list[ScrapedDocument]:
    # MCA's document panel is genuinely flaky: the same URL sometimes renders
    # the full dated list and sometimes only the marquee ticker, depending on
    # which cached AEM variant is served. Silently accepting a marquee-only
    # page loses real documents, so reload and try again before settling for
    # whatever rendered.
    raw = []
    for attempt in range(1, attempts + 1):
        driver.get(url)
        panel_found = True
        try:
            WebDriverWait(driver, wait_seconds).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector))
            )
        except TimeoutException:
            panel_found = False
            logger.warning(
                f"  '{wait_selector}' never appeared on {url} within {wait_seconds}s "
                f"(attempt {attempt}/{attempts})"
            )

        # AEM renders the panel, then its pagination JS rewrites it. Reading too
        # early catches a partially-rendered list, so let it settle first.
        time.sleep(settle_seconds)

        try:
            raw = driver.execute_script(_COLLECT_JS) or []
        except WebDriverException as e:
            logger.error(f"  could not read links from '{name}': {e}")
            raw = []

        if panel_found and any(r.get("region") == "doc-link" for r in raw):
            break
        if attempt < attempts:
            logger.info(f"  document panel missing — reloading '{name}' and retrying")
    else:
        logger.warning(
            f"  '{name}': document panel never rendered after {attempts} attempts; "
            f"continuing with whatever was found (previously-seen documents from that "
            f"panel are NOT lost — they stay recorded in the manifest)"
        )

    docs: list[ScrapedDocument] = []
    skipped_furniture = 0
    for item in raw or []:
        href = item.get("href") or ""
        if not _is_pdf(href):
            continue  # these panels also carry links to ordinary HTML pages
        if _is_site_furniture(href):
            skipped_furniture += 1
            continue
        absolute = urljoin(url, href)
        title = (item.get("title") or "").strip() or absolute.rsplit("/", 1)[-1]
        docs.append(
            ScrapedDocument(
                source_url=absolute,
                title=title[:300],
                source_page=name,
                page_url=url,
                region=item.get("region") or "unknown",
                doc_date=(item.get("date") or "").strip(),
            )
        )

    if skipped_furniture:
        logger.info(f"  skipped {skipped_furniture} website-policy PDF(s) (not MCA documents)")
    if not docs:
        _dump_diagnostics(driver, name, logs_dir)
    return docs
