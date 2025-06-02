"""linkweb.py – hyperlink‑only crawler
=================================================
Crawls https://sustainability.ucsc.edu, grabs every internal hyperlink, and
spits out three artefacts under ./linkmap/:

• links_flat.txt   – unique URLs, one per line
• links_tree.txt   – indented filesystem‑style hierarchy of paths
• links_graph.dot  – Graphviz edge list (page → out‑links)

Run:
    python linkweb.py
Optional visualisation:
    dot -Tpng linkmap/links_graph.dot -o site_graph.png
"""

import asyncio
import pathlib
import urllib.parse
from collections import defaultdict, deque
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
BASE_URL        = "https://sustainability.ucsc.edu"
ALLOWED_PREFIX  = "/"                  # crawl entire site
OUTPUT_ROOT     = pathlib.Path("linkmap")
REQUEST_TIMEOUT = 30_000                # ms
MAX_PAGES       = 2_000                 # failsafe
HEADLESS        = True                  # flip to False for debugging

SKIP_EXT = (
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".csv", ".zip", ".tar", ".gz", ".jpg", ".jpeg", ".png", ".gif"
)

# ---------------------------------------------------------------------------
# URL HELPERS
# ---------------------------------------------------------------------------

def strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def is_internal(url: str) -> bool:
    """Return True if *url* belongs to the same domain and isn’t a binary."""
    url = strip_fragment(url)
    p   = urllib.parse.urlparse(url)
    base_netloc = urllib.parse.urlparse(BASE_URL).netloc
    if p.scheme not in {"http", "https"}:
        return False
    if p.netloc != base_netloc:
        return False
    if not p.path.startswith(ALLOWED_PREFIX):
        return False
    if p.path.lower().endswith(SKIP_EXT):
        return False
    return True


def normalise(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    return strip_fragment(urllib.parse.urljoin(BASE_URL, url))

# ---------------------------------------------------------------------------
# PLAYWRIGHT FETCH
# ---------------------------------------------------------------------------

async def fetch(page, url: str) -> list[str]:
    """Return list of *absolute* hrefs on *url*."""
    try:
        await page.goto(url, timeout=REQUEST_TIMEOUT)
        hrefs = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href)"
        )
        return [normalise(h) for h in hrefs]
    except PlaywrightTimeout:
        print(f"[timeout] {url}")
    except Exception as exc:
        print(f"[error]   {url} -> {exc}")
    return []

# ---------------------------------------------------------------------------
# TREE‑VIEW UTILITIES
# ---------------------------------------------------------------------------

def insert_path(tree: dict, url: str) -> None:
    path = urllib.parse.urlparse(url).path.lstrip("/") or "index"
    cur  = tree
    for part in path.split("/"):
        cur = cur.setdefault(part, {})


def render_tree(d: dict, prefix: str = "") -> list[str]:
    """Pretty‑print nested dict like *tree* command."""
    lines = []
    keys  = sorted(d.keys())
    for idx, name in enumerate(keys):
        connector = "└── " if idx == len(keys) - 1 else "├── "
        lines.append(prefix + connector + str(name))
        extension = "    " if idx == len(keys) - 1 else "│   "
        lines.extend(render_tree(d[name], prefix + extension))
    return lines

# ---------------------------------------------------------------------------
# MAIN CRAWLER
# ---------------------------------------------------------------------------

async def crawl() -> None:
    OUTPUT_ROOT.mkdir(exist_ok=True)

    visited: set[str]            = set()
    frontier: deque[str]         = deque([BASE_URL])
    edges:   dict[str, set[str]] = defaultdict(set)
    tree:    dict                = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context()
        page    = await context.new_page()

        while frontier and len(visited) < MAX_PAGES:
            url = frontier.popleft()
            if url in visited:
                continue
            visited.add(url)
            print(f"[visit] {url}")
            insert_path(tree, url)

            for href in await fetch(page, url):
                edges[url].add(href) 
                if is_internal(href) and href not in visited:
                    frontier.append(href)

        await browser.close()

    # ---------------- WRITE OUTPUTS ----------------
    flat_path  = OUTPUT_ROOT / "links_flat.txt"
    tree_path  = OUTPUT_ROOT / "links_tree.txt"
    graph_path = OUTPUT_ROOT / "links_graph.dot"

    flat_path.write_text("\n".join(sorted(visited)), encoding="utf-8")
    tree_path.write_text("/ (root)\n" + "\n".join(render_tree(tree)), encoding="utf-8")

    with graph_path.open("w", encoding="utf-8") as fh:
        fh.write("digraph site {\n    rankdir=LR;\n    node [shape=rectangle,fontsize=10];\n")
        for src, dsts in edges.items():
            for dst in dsts:
                fh.write(f"    \"{src}\" -> \"{dst}\";\n")
        fh.write("}\n")

    print(
        "\nFinished!\n"
        f"  pages visited : {len(visited)}\n"
        f"  edges written : {sum(len(v) for v in edges.values())}\n"
        f"  flat list     : {flat_path}\n"
        f"  tree view     : {tree_path}\n"
        f"  graph DOT     : {graph_path}\n"
    )

# ---------------------------------------------------------------------------
# ENTRY‑POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(crawl())
