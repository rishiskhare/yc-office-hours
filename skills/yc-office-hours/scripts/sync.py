#!/usr/bin/env python3
"""Refresh the local YC Library and Paul Graham cache.

The carousel page is committed before the backlog, and each file is written
on its own, so a killed run can resume without discarding finished work.
"""

from __future__ import annotations

import html
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from store import (
    atomic_write,
    cache_dir,
    html_title,
    html_to_text,
    library_markdown,
    load_state,
    pg_markdown,
    safe_slug,
    save_state,
)

UA = "yc-office-hours (personal cache)"
LIBRARY_URL = "https://www.ycombinator.com/library"
SITEMAP_URL = "https://www.ycombinator.com/library/sitemap.xml"
PG_INDEX_URL = "https://www.paulgraham.com/articles.html"
PG_NAV = {"index", "rss", "bio", "articles"}
WORKERS = 3
PG_MIN_CHARS = 500
PG_BANNER = re.compile(r"Want to start a startup\?\s*Get funded by\s*Y Combinator\.")


class MarkupError(Exception):
    """A page came back, but not as an article. A few are junk; many means the scrape broke."""


def fetch_url(url: str, timeout: int = 60) -> tuple[int, bytes]:
    delay = 1.0
    last_error: Exception | None = None
    for attempt in range(3):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": UA, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code == 404:
                return 404, b""
            if code in {429, 500, 502, 503, 504} and attempt < 2:
                last_error = error
                time.sleep(delay)
                delay *= 2
                continue
            if code in {429, 500, 502, 503, 504}:
                raise
            return code, b""
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    if last_error:
        raise last_error
    raise RuntimeError(f"fetch failed: {url}")


def parse_data_page(page: str) -> dict:
    match = re.search(r'data-page="([^"]*)"', page)
    if not match:
        raise ValueError("data-page missing")
    data = json.loads(html.unescape(match.group(1)))
    if not isinstance(data, dict) or "props" not in data:
        raise ValueError("data-page has no props")
    return data


def text_of(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def is_useful(article: dict) -> bool:
    return bool(text_of(article.get("transcript")) or text_of(article.get("content")))


def collect_articles(data: dict) -> tuple[dict[str, dict], dict[str, list[str]]]:
    props = data.get("props") or {}
    by_slug: dict[str, dict] = {}
    series: dict[str, list[str]] = {}
    for carousel in props.get("carousels") or []:
        name = carousel.get("name") or ""
        for article in carousel.get("articles") or []:
            slug = article.get("slug")
            if not isinstance(slug, str) or not slug:
                continue
            names = series.setdefault(slug, [])
            if name and name not in names:
                names.append(name)
            previous = by_slug.get(slug)
            if previous is None or len(text_of(article.get("transcript"))) > len(text_of(previous.get("transcript"))):
                by_slug[slug] = article
    featured = props.get("featured")
    if isinstance(featured, dict) and isinstance(featured.get("slug"), str):
        slug = featured["slug"]
        if slug not in by_slug:
            by_slug[slug] = featured
            series[slug] = [featured.get("series_name") or "Featured"]
    return by_slug, series


def parse_sitemap(xml: str) -> list[str]:
    slugs = []
    seen = set()
    for loc in re.findall(r"<loc>(.*?)</loc>", xml):
        if "/library/" not in loc or "categories=" in loc:
            continue
        slug = loc.rstrip("/").rsplit("/", 1)[-1]
        if not slug or slug in seen or slug == "library":
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


def parse_pg_index(page: str) -> list[str]:
    slugs = []
    seen = set()
    for href in re.findall(r'href="([A-Za-z0-9]+)\.html"', page):
        if href.lower() in PG_NAV or href in seen:
            continue
        seen.add(href)
        slugs.append(href)
    return slugs


def decode_pg(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("latin-1")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _age_ok(stamp: str | None, now: datetime, limit: timedelta) -> bool:
    if not stamp:
        return False
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return now - then < limit


def _fail(message: str, state: dict) -> int:
    print(f"ERROR {message}", file=sys.stderr)
    fetched = state.get("carousel_fetched_at")
    if fetched:
        print(f"fetched_at {fetched}", file=sys.stderr)
    return 2


def _commit_carousel(cache: Path, useful: dict[str, dict], series: dict[str, list[str]], state: dict) -> set[str]:
    library = cache / "library"
    library.mkdir(parents=True, exist_ok=True)
    new_slugs = set()
    for slug, article in useful.items():
        try:
            safe = safe_slug(slug)
        except ValueError:
            continue
        article = dict(article)
        article["slug"] = safe
        atomic_write(library / f"{safe}.md", library_markdown(article, series.get(slug) or ["Library"]))
        new_slugs.add(safe)
    removed = set(state.get("carousel_slugs") or []) - new_slugs
    for slug in removed:
        try:
            safe = safe_slug(slug)
        except ValueError:
            continue
        (library / f"{safe}.md").unlink(missing_ok=True)
    state["carousel_slugs"] = sorted(new_slugs)
    state["carousel_count"] = len(new_slugs)
    return removed


def _save_pg(cache: Path, slug: str, body: bytes) -> bool:
    page = decode_pg(body)
    text = PG_BANNER.sub("", html_to_text(page), count=1).strip()
    if len(text) < PG_MIN_CHARS:
        return False
    title = html_title(page) or slug
    atomic_write(cache / "pg" / f"{safe_slug(slug)}.md", pg_markdown(slug, title, text))
    return True


def _save_sitemap_item(cache: Path, slug: str, body: bytes) -> bool:
    try:
        article = parse_data_page(body.decode("utf-8", "replace"))["props"]["article"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise MarkupError(str(error)) from error
    if not isinstance(article, dict) or not is_useful(article):
        return False
    article = dict(article)
    article["slug"] = slug
    series_name = article.get("series_name")
    series = [series_name] if isinstance(series_name, str) and series_name else ["Library"]
    atomic_write(cache / "library" / f"{safe_slug(slug)}.md", library_markdown(article, series))
    return True


def _skip(state: dict, key: str, slug: str) -> None:
    skipped_key = "pg_skipped" if key == "pg_backlog" else "sitemap_skipped"
    skipped = state.setdefault(skipped_key, [])
    if slug not in skipped:
        skipped.append(slug)


def _drain(cache: Path, state: dict, key: str, url_for, save_doc, missing_path, fetch, label: str) -> bool:
    backlog = list(state.get(key) or [])
    if not backlog:
        return True
    total = len(backlog)
    lock = threading.Lock()
    done = 0
    deferred: list[str] = []

    def work(slug: str):
        try:
            status, body = fetch(url_for(slug))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            return slug, "retry", str(error)
        if status == 404:
            return slug, "missing", None
        if status != 200:
            # 429 and 5xx already retried inside fetch. A wave of 403s is a block,
            # not a finished refresh. One blocked page should not retry forever.
            if status == 403:
                return slug, "blocked", None
            if 400 <= status < 500:
                return slug, "skip", None
            return slug, "retry", f"HTTP {status}"
        return slug, "ok", body

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(work, slug) for slug in backlog]
        for future in as_completed(futures):
            slug, kind, payload = future.result()
            kept = False
            if kind == "ok":
                try:
                    kept = save_doc(cache, slug, payload)
                except MarkupError:
                    kind = "markup"
                except ValueError:
                    kept = False
                except OSError:
                    kind = "retry"
            with lock:
                current = list(state.get(key) or [])
                if slug in current:
                    current.remove(slug)
                if kind in {"retry", "markup", "blocked"}:
                    current.append(slug)
                    if kind in {"markup", "blocked"}:
                        deferred.append(slug)
                elif kind == "missing":
                    missing = state.setdefault(missing_path, [])
                    if slug not in missing:
                        missing.append(slug)
                    try:
                        target = cache / ("pg" if key == "pg_backlog" else "library") / f"{safe_slug(slug)}.md"
                    except ValueError:
                        target = None
                    if target is not None:
                        target.unlink(missing_ok=True)
                elif not kept:
                    _skip(state, key, slug)
                state[key] = current
                save_state(cache, state)
                done += 1
                print(f"{label} {done}/{total} {slug} {kind}", flush=True)

    if len(deferred) >= 5 and len(deferred) / total > 0.1:
        return False
    if deferred:
        current = [slug for slug in (state.get(key) or []) if slug not in deferred]
        for slug in deferred:
            _skip(state, key, slug)
        state[key] = current
        save_state(cache, state)
    return True


def _queue(state: dict, key: str, slugs: list[str], skip_keys: tuple[str, ...]) -> None:
    skip = set()
    for name in skip_keys:
        skip.update(state.get(name) or [])
    queued = []
    for slug in slugs:
        try:
            safe_slug(slug)
        except ValueError:
            continue
        if slug not in skip:
            queued.append(slug)
    state[key] = queued


def sync(cache: Path, *, force: bool = False, fetch=fetch_url, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    cache.mkdir(parents=True, exist_ok=True)
    state = load_state(cache)
    if force:
        for key in (
            "pg_backlog",
            "sitemap_backlog",
            "pg_not_found",
            "sitemap_not_found",
            "pg_skipped",
            "sitemap_skipped",
            "pg_fetched_at",
            "sitemap_gap_fetched_at",
        ):
            state.pop(key, None)

    library_files = list((cache / "library").glob("*.md")) if (cache / "library").exists() else []
    carousel_fresh = not force and _age_ok(state.get("carousel_fetched_at"), now, timedelta(hours=24))
    pg_due = force or not _age_ok(state.get("pg_fetched_at"), now, timedelta(days=30))
    gap_due = force or not _age_ok(state.get("sitemap_gap_fetched_at"), now, timedelta(days=30))
    pg_backlog = state.get("pg_backlog") or []
    sitemap_backlog = state.get("sitemap_backlog") or []
    if carousel_fresh and not pg_backlog and not sitemap_backlog and not pg_due and not gap_due:
        print("FRESH", flush=True)
        return 0

    if not library_files:
        print("BUILDING", flush=True)

    if not carousel_fresh:
        try:
            status, body = fetch(LIBRARY_URL, 90)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            return _fail(f"network {error}", state)
        if status != 200:
            return _fail(f"network HTTP {status}", state)
        try:
            articles, series = collect_articles(parse_data_page(body.decode("utf-8", "replace")))
        except (ValueError, json.JSONDecodeError, UnicodeError) as error:
            return _fail(f"markup {error}", state)
        useful = {slug: article for slug, article in articles.items() if is_useful(article)}
        if not useful:
            return _fail("markup no articles", state)
        previous = state.get("carousel_count")
        if isinstance(previous, int) and previous > 0 and len(useful) < previous * 0.9:
            return _fail(f"markup count drop {len(useful)} from {previous}", state)
        removed = _commit_carousel(cache, useful, series, state)
        if removed:
            # A slug can leave a carousel and remain a sitemap essay. Dropping the
            # gap timestamp makes this run fetch it back instead of waiting 30 days.
            state.pop("sitemap_gap_fetched_at", None)
            gap_due = True
        state["carousel_fetched_at"] = _iso(now)
        save_state(cache, state)
        print(f"READY {state['carousel_count']}", flush=True)
    else:
        print(f"READY {state.get('carousel_count') or 0}", flush=True)

    if pg_due and not state.get("pg_backlog"):
        try:
            status, body = fetch(PG_INDEX_URL)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            return _fail(f"network {error}", state)
        if status != 200:
            return _fail(f"network HTTP {status}", state)
        page = decode_pg(body)
        if 'href="' not in page:
            return _fail("markup pg index", state)
        slugs = parse_pg_index(page)
        if not slugs:
            return _fail("markup pg index", state)
        previous = state.get("pg_count")
        if isinstance(previous, int) and previous > 0 and len(slugs) < previous * 0.9:
            return _fail(f"markup pg count drop {len(slugs)} from {previous}", state)
        state["pg_count"] = len(slugs)
        _queue(state, "pg_backlog", slugs, ("pg_not_found", "pg_skipped"))
        save_state(cache, state)

    _drain(
        cache,
        state,
        "pg_backlog",
        lambda slug: f"https://www.paulgraham.com/{slug}.html",
        _save_pg,
        "pg_not_found",
        fetch,
        "PG",
    )
    if pg_due and not state.get("pg_backlog"):
        state["pg_fetched_at"] = _iso(now)
        save_state(cache, state)

    if gap_due and not state.get("sitemap_backlog"):
        try:
            status, body = fetch(SITEMAP_URL)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            return _fail(f"network {error}", state)
        if status != 200:
            return _fail(f"network HTTP {status}", state)
        xml = body.decode("utf-8", "replace")
        if "<urlset" not in xml:
            return _fail("markup sitemap", state)
        found = parse_sitemap(xml)
        if not found:
            return _fail("markup sitemap", state)
        slugs = [slug for slug in found if slug not in set(state.get("carousel_slugs") or [])]
        previous = state.get("sitemap_count")
        if isinstance(previous, int) and previous > 0 and len(found) < previous * 0.9:
            return _fail("markup sitemap count drop", state)
        state["sitemap_count"] = len(found)
        _queue(state, "sitemap_backlog", slugs, ("sitemap_not_found", "sitemap_skipped"))
        save_state(cache, state)

    if not _drain(
        cache,
        state,
        "sitemap_backlog",
        lambda slug: f"https://www.ycombinator.com/library/{slug}",
        _save_sitemap_item,
        "sitemap_not_found",
        fetch,
        "SITEMAP",
    ):
        return _fail("markup sitemap pages", state)
    if gap_due and not state.get("sitemap_backlog"):
        state["sitemap_gap_fetched_at"] = _iso(now)
        save_state(cache, state)

    pg_left = len(state.get("pg_backlog") or [])
    sitemap_left = len(state.get("sitemap_backlog") or [])
    if pg_left or sitemap_left:
        print(f"BACKLOG pg={pg_left} sitemap={sitemap_left}", flush=True)
    else:
        print("DONE", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return sync(cache_dir(), force="--force" in args)
    except Exception as error:
        print(f"ERROR network {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
