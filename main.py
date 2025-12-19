import re
import time
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://pixelbypixel.studio/emotes"

app = FastAPI(title="PBPS Emote Search")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Cache now stores tiles (name + imageUrl), not just names
_emote_list_cache = {"updated_at": 0.0, "tiles": []}  # type: ignore
_detail_cache: Dict[str, Dict[str, Any]] = {}


def norm(s: str) -> str:
    # "golden goat" -> "goldengoat"
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _render_page_html(url: str, wait_for_text: Optional[str] = None) -> str:
    """
    Render with a real browser and return the final DOM HTML.
    `wait_for_text` is best-effort: it waits briefly for that text to appear.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)

        if wait_for_text:
            try:
                page.wait_for_selector(f"text={wait_for_text}", timeout=7_000)
            except Exception:
                pass

        html = page.content()
        browser.close()
        return html


def fetch_emote_tiles_rendered() -> List[Dict[str, Any]]:
    """
    Renders the emotes grid and extracts:
      - emote name from the link query (?emoteName=...)
      - imageUrl from <img> or CSS background-image (best-effort)
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)

        # Wait until emote links exist in the rendered DOM
        page.wait_for_selector('a[href*="emoteName="]', timeout=30_000)

        tiles = page.eval_on_selector_all(
            'a[href*="emoteName="]',
            r"""els => els.map(a => {
              const u = new URL(a.href);
              const name = u.searchParams.get("emoteName");

              // Try <img> inside the anchor first
              const img = a.querySelector("img");
              let imageUrl = img ? (img.currentSrc || img.src) : null;

              // Fallback: try CSS background-image on the anchor
              if (!imageUrl) {
                const bg = getComputedStyle(a).backgroundImage || "";
                const m = bg.match(/url\(["']?(.*?)["']?\)/);
                if (m && m[1]) imageUrl = m[1];
              }

              return { name, imageUrl };
            }).filter(x => x.name)"""
        )

        browser.close()

    # De-duplicate by name
    seen = set()
    out: List[Dict[str, Any]] = []
    for t in tiles:
        n = (t.get("name") or "").strip()
        if not n or n in seen:
            continue
        seen.add(n)
        out.append({"name": n, "imageUrl": t.get("imageUrl")})

    out.sort(key=lambda x: x["name"].lower())
    return out


def ensure_emote_list(max_age_seconds: int = 6 * 60 * 60) -> None:
    now = time.time()

    # Only reuse cache if non-empty and fresh
    if _emote_list_cache["tiles"] and (now - float(_emote_list_cache["updated_at"]) < max_age_seconds):
        return

    try:
        tiles = fetch_emote_tiles_rendered()
    except (PlaywrightTimeoutError, PlaywrightError) as e:
        raise HTTPException(
            status_code=500,
            detail=f"Playwright failed to render emotes list: {type(e).__name__}: {e}",
        )

    if not tiles:
        _emote_list_cache["updated_at"] = 0.0
        raise HTTPException(status_code=502, detail="Parsed 0 emote tiles after rendering.")

    _emote_list_cache["tiles"] = tiles
    _emote_list_cache["updated_at"] = now


def parse_emote_details(emote_name: str, html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    # Prefer a Twitch link that has visible text (channel name)
    channel_url = None
    channel_label = None
    for a in soup.select('a[href*="twitch.tv/"]'):
        href = a.get("href")
        txt = a.get_text(strip=True)
        if href and txt:
            channel_url = href
            channel_label = txt
            break

    # Fallback: derive channel from URL path if link has no text
    if not channel_label and channel_url:
        try:
            path = urlparse(channel_url).path.strip("/")
            if path:
                channel_label = path.split("/")[0]
        except Exception:
            pass

    lines = [x.strip() for x in soup.get_text("\n").splitlines() if x.strip()]
    joined = "\n".join(lines)

    tier: Optional[str] = None
    source: Optional[str] = None
    expires: Optional[str] = None

    not_found = "Emote not found" in joined

    # Typical sequence near emote name:
    # name, channel, source, tier, "Expires:", value
    if emote_name in lines:
        i = lines.index(emote_name)

        if i + 2 < len(lines):
            source = lines[i + 2]
        if i + 3 < len(lines):
            tier = lines[i + 3]

        try:
            exp_i = lines.index("Expires:", i)
            if exp_i + 1 < len(lines):
                expires = lines[exp_i + 1]
        except ValueError:
            pass
    else:
        # If name isn't found, still try to find Expires:
        try:
            exp_i = lines.index("Expires:")
            if exp_i + 1 < len(lines):
                expires = lines[exp_i + 1]
        except ValueError:
            pass

    return {
        "emoteName": emote_name,
        "channel": channel_label,
        "channelUrl": channel_url,
        "source": source,
        "tier": tier,
        "expires": expires,
        "detailsUrl": f"{BASE_URL}?emoteName={emote_name}",
        "notFound": not_found,
    }


@app.get("/")
def home():
    return FileResponse("static/index.html")


@app.get("/api/emotes")
def api_emotes(
    q: str = Query(default="", description="Partial search (supports spaces)"),
    limit: int = Query(default=300, ge=1, le=2000),
):
    ensure_emote_list()
    tiles: List[Dict[str, Any]] = _emote_list_cache["tiles"]

    if q:
        terms = [norm(t) for t in q.split() if t.strip()]
        tiles = [t for t in tiles if all(term in norm(t["name"]) for term in terms)]

    return {
        "count": len(tiles),
        "updatedAt": _emote_list_cache["updated_at"],
        "items": tiles[:limit],  # now objects: {name, imageUrl}
    }


@app.post("/api/refresh")
def api_refresh():
    _emote_list_cache["updated_at"] = 0.0
    _emote_list_cache["tiles"] = []
    ensure_emote_list(max_age_seconds=0)
    return {
        "ok": True,
        "count": len(_emote_list_cache["tiles"]),
        "updatedAt": _emote_list_cache["updated_at"],
    }


@app.get("/api/emotes/{emote_name}")
def api_emote_detail(emote_name: str):
    cached = _detail_cache.get(emote_name)
    if cached and (time.time() - cached["cachedAt"] < 24 * 60 * 60):
        return cached["data"]

    url = f"{BASE_URL}?emoteName={emote_name}"

    try:
        # Render the details page so channel/tier/expires exist in DOM
        html = _render_page_html(url, wait_for_text="Expires:")
    except (PlaywrightTimeoutError, PlaywrightError) as e:
        raise HTTPException(
            status_code=500,
            detail=f"Playwright failed to render emote details: {type(e).__name__}: {e}",
        )

    data = parse_emote_details(emote_name, html)

    # Attach imageUrl from list cache (fast, no extra scraping)
    try:
        ensure_emote_list()
        img = next(
            (t.get("imageUrl") for t in _emote_list_cache["tiles"] if t.get("name") == emote_name),
            None,
        )
        data["imageUrl"] = img
    except Exception:
        data["imageUrl"] = None

    _detail_cache[emote_name] = {"cachedAt": time.time(), "data": data}
    return data


@app.exception_handler(Exception)
def all_exception_handler(_, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})
