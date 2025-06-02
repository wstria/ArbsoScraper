import requests
import playwright
import asyncio
import pathlib
import urllib.parse
from collections import deque
from html import unescape
from bs4 import BeautifulSoup, NavigableString, Tag
from trafilatura import extract as trafilatura_extract
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

"""
Dynamic scraper for the UCSC Sustainability Office site.

• RAW   → scraped/…/page.txt          (full HTML)
• CLEAN → scraped/…/page.clean.md     (Markdown that preserves headings, **bold**, lists, etc.)

Copy‑pasting the *.clean.md* file into Google Docs (or letting Drive convert it) keeps
formatting almost identical to the live page.
"""
# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
BASE_URL        = "https://sustainability.ucsc.edu"
ALLOWED_PREFIXES = ("/",)         # crawl only these sub‑trees
OUTPUT_ROOT     = pathlib.Path("scraped")
REQUEST_TIMEOUT = 30_000                 # ms
MAX_PAGES       = 1_000

SKIP_EXT = (
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".csv", ".zip", ".tar", ".gz", ".jpg", ".jpeg", ".png", ".gif"
)

BULLET = "- "        # Markdown bullet
INDENT = "  "        # two‑space indent per level

# ---------------------------------------------------------------------------
# URL HELPERS
# ---------------------------------------------------------------------------

def strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]

def is_allowed(url: str) -> bool:
    """Within domain, under an allowed prefix, and not a binary asset."""
    url = strip_fragment(url)
    p   = urllib.parse.urlparse(url)
    if p.scheme not in {"http", "https"}:                            return False
    if p.netloc != urllib.parse.urlparse(BASE_URL).netloc:              return False
    if p.path.lower().endswith(SKIP_EXT):                               return False
    return any(p.path.startswith(pref) for pref in ALLOWED_PREFIXES)

def url_to_path(url: str, *, suffix: str) -> pathlib.Path:
    """Map a URL to OUTPUT_ROOT / … / file{suffix}."""
    p = urllib.parse.urlparse(strip_fragment(url))
    path = p.path or "/"
    if path.endswith("/"):   # directory → index
        path += "index"
    rel = path.lstrip("/") + suffix
    return OUTPUT_ROOT / rel

# ---------------------------------------------------------------------------
# PLAYWRIGHT FETCH
# ---------------------------------------------------------------------------

async def fetch_page(page, url: str) -> str | None:
    try:
        await page.goto(url, timeout=REQUEST_TIMEOUT)
        return await page.content()
    except PlaywrightTimeout:
        print(f"[timeout] {url}")
    except Exception as exc:
        print(f"[error]   {url} -> {exc}")
    return None

# ---------------------------------------------------------------------------
# MARKDOWN CONVERSION
# ---------------------------------------------------------------------------

def inline_md(node: Tag | NavigableString) -> str:
    """Render inline content with **bold**, *italic*, and links."""
    if isinstance(node, NavigableString):
        return unescape(str(node))

    name = node.name.lower()
    if name in {"strong", "b"}:
        return f"**{''.join(inline_md(c) for c in node.children)}**"
    if name in {"em", "i"}:
        return f"*{''.join(inline_md(c) for c in node.children)}*"
    if name == "a":
        text = ''.join(inline_md(c) for c in node.children).strip()
        href = node.get("href", "").strip()
        if href and href.startswith("http") and href != text:
            return f"[{text}]({href})"
        return text
    if name == "br":
        return "\n"
    # fallback: concat children
    return ''.join(inline_md(c) for c in node.children)

def block_md(node: Tag | NavigableString, depth: int = 0) -> str:
    """Recursively convert block‑level elements to Markdown."""
    if isinstance(node, NavigableString):
        return inline_md(node)

    name = node.name.lower()

    # headings --------------------------------------------------------------
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(name[1])
        return "#" * level + " " + inline_md(node) + "\n\n"

    # paragraphs ------------------------------------------------------------
    if name in {"p", "div", "section"}:
        if any(isinstance(c, Tag) and c.name.lower() in {"p", "div", "ul", "ol", "li", "section"} for c in node.children):
            return ''.join(block_md(c, depth) for c in node.children)
        return inline_md(node).strip() + "\n\n"

    # lists -----------------------------------------------------------------
    if name == "ul":
        out = []
        for li in node.find_all("li", recursive=False):
            out.append(INDENT * depth + BULLET + block_md(li, depth + 1).lstrip())
        return "\n".join(out) + "\n\n"

    if name == "ol":
        out = []
        for i, li in enumerate(node.find_all("li", recursive=False), 1):
            out.append(INDENT * depth + f"{i}. " + block_md(li, depth + 1).lstrip())
        return "\n".join(out) + "\n\n"

    if name == "li":
        parts = []
        for child in node.children:
            md = block_md(child, depth)
            parts.append(md)
        return ''.join(parts).strip()

    # generic container: recurse -------------------------------------------
    return ''.join(block_md(c, depth) for c in node.children)

def clean_html(html: str) -> str:
    """Extract #main/.main-content and convert to Markdown preserving style."""
    soup = BeautifulSoup(html, "lxml")
    body = soup.select_one("#main") or soup.select_one(".main-content") or soup.body
    if not body:
        return trafilatura_extract(html, include_links=False) or ""

    md = block_md(body).rstrip()
    if len(md) < 200:
        md = trafilatura_extract(html, include_links=False) or md
    return md

# ---------------------------------------------------------------------------
# CRAWLER
# ---------------------------------------------------------------------------
async def crawl():
    OUTPUT_ROOT.mkdir(exist_ok=True)

    pages_scraped = links_seen = chars_raw = chars_clean = 0
    visited: set[str] = set()
    frontier: deque[str] = deque(
        [urllib.parse.urljoin(BASE_URL, p) for p in ALLOWED_PREFIXES]
    )

    async with async_playwright() as pw:
        browser  = await pw.chromium.launch(headless=True)
        context  = await browser.new_context()
        page     = await context.new_page()

        while frontier and len(visited) < MAX_PAGES:
            url = strip_fragment(frontier.popleft())
            if url in visited:
                continue
            visited.add(url)

            html = await fetch_page(page, url)
            if html is None:
                continue

            # RAW ------------------------------------------------------------
            raw_path = url_to_path(url, suffix=".txt")
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(html, encoding="utf-8")
            chars_raw += len(html)

            # CLEAN (Markdown) ----------------------------------------------
            md = clean_html(html)
            clean_path = url_to_path(url, suffix=".clean.md")
            clean_path.parent.mkdir(parents=True, exist_ok=True)
            clean_path.write_text(md, encoding="utf-8")
            chars_clean += len(md)

            pages_scraped += 1
            print(f"[saved] {url} → {raw_path.name}, {clean_path.name}")

            # LINK DISCOVERY -------------------------------------------------
            try:
                links = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            except Exception:
                links = []
            links_seen += len(links)

            for link in links:
                link = strip_fragment(link)
                if is_allowed(link) and link not in visited:
                    frontier.append(link)

        await browser.close()

    print(
        "\nFinished!\n"
        f"  pages scraped   : {pages_scraped}\n"
        f"  unique links     : {len(visited)}\n"
        f"  links discovered : {links_seen}\n"
        f"  raw chars        : {chars_raw:,}\n"
        f"  markdown chars   : {chars_clean:,}\n"
        f"Artefacts saved in '{OUTPUT_ROOT}/'\n"
    )

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    asyncio.run(crawl())
