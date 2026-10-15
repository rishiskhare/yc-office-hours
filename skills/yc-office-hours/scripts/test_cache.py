#!/usr/bin/env python3
"""Offline tests for cache writes, the drop guard, and search ranking."""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import search  # noqa: E402
import sync  # noqa: E402
from store import library_markdown, parse_document  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "library-page.html"
NOW = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)
CAROUSEL_SITEMAP = b"<urlset><url><loc>https://www.ycombinator.com/library/ab-dont</loc></url></urlset>"


def item_page(slug: str, title: str, content: str) -> bytes:
    import html as html_lib

    payload = {"props": {"article": {"slug": slug, "title": title, "content": content, "author": "Y Combinator"}}}
    escaped = html_lib.escape(json.dumps(payload), quote=True)
    return f'<div data-page="{escaped}"></div>'.encode()


def pg_page(slug: str, title: str, words: str) -> bytes:
    body = " ".join([words] * 80)
    return f"<html><head><title>{title}</title></head><body><p>{body}</p></body></html>".encode()


class CacheTests(unittest.TestCase):
    def test_apostrophe_roundtrip(self):
        page = FIXTURE.read_text(encoding="utf-8")
        articles, series = sync.collect_articles(sync.parse_data_page(page))
        article = articles["ab-dont"]
        meta, body = parse_document(library_markdown(article, series["ab-dont"]))
        self.assertEqual(meta["title"], "Don't scale")
        self.assertEqual(meta["chapters"], [{"time": 0, "title": "Intro: start"}])
        self.assertIn("Don't scale", body)
        self.assertTrue(meta["has_transcript"])

    def test_count_drop_keeps_files(self):
        with self._cache() as cache:
            kept = cache / "library" / "keep-me.md"
            kept.parent.mkdir(parents=True)
            kept.write_text("original\n", encoding="utf-8")
            (cache / "state.json").write_text(
                json.dumps({"carousel_count": 10, "carousel_slugs": ["keep-me"]}),
                encoding="utf-8",
            )
            code = sync.sync(cache, fetch=lambda url, timeout=60: (200, FIXTURE.read_bytes()), now=NOW)
            self.assertEqual(code, 2)
            self.assertEqual(kept.read_text(encoding="utf-8"), "original\n")
            self.assertFalse((cache / "library" / "ab-dont.md").exists())

    def test_bad_markup_keeps_files(self):
        with self._cache() as cache:
            kept = cache / "library" / "keep-me.md"
            kept.parent.mkdir(parents=True)
            kept.write_text("original\n", encoding="utf-8")
            code = sync.sync(cache, fetch=lambda url, timeout=60: (200, b"<html>no data-page</html>"), now=NOW)
            self.assertEqual(code, 2)
            self.assertEqual(kept.read_text(encoding="utf-8"), "original\n")

    def test_gap_file_survives_carousel_refresh(self):
        with self._cache() as cache:
            gap = cache / "library" / "gap-essay.md"
            gap.parent.mkdir(parents=True)
            gap.write_text("gap\n", encoding="utf-8")
            (cache / "state.json").write_text(
                json.dumps({"carousel_count": 1, "carousel_slugs": ["ab-dont"]}),
                encoding="utf-8",
            )

            def fetch(url, timeout=60):
                if url.endswith("/library"):
                    return 200, FIXTURE.read_bytes()
                if url.endswith("articles.html"):
                    return 200, b'<a href="index.html">Home</a><a href="ds.html">Do things</a>'
                if url.endswith("sitemap.xml"):
                    return 200, CAROUSEL_SITEMAP
                if url.endswith("/ds.html"):
                    return 200, pg_page("ds", "Do Things that Don't Scale", "recruit users manually")
                raise AssertionError(url)

            code = sync.sync(cache, fetch=fetch, now=NOW)
            self.assertEqual(code, 0)
            self.assertEqual(gap.read_text(encoding="utf-8"), "gap\n")
            self.assertTrue((cache / "library" / "ab-dont.md").exists())

    def test_resume_keeps_finished_files(self):
        with self._cache() as cache:
            calls = {"ds": 0}

            def fetch(url, timeout=60):
                if url.endswith("/library"):
                    return 200, FIXTURE.read_bytes()
                if url.endswith("articles.html"):
                    return 200, b'<a href="ds.html">One</a><a href="growth.html">Two</a>'
                if url.endswith("sitemap.xml"):
                    return 200, CAROUSEL_SITEMAP
                if url.endswith("/ds.html"):
                    return 200, pg_page("ds", "Do Things that Don't Scale", "recruit users manually")
                if url.endswith("/growth.html"):
                    calls["ds"] += 1
                    if calls["ds"] == 1:
                        raise urllib.error.URLError("boom")
                    return 200, pg_page("growth", "Startup Growth", "weekly growth rate")
                raise AssertionError(url)

            first = sync.sync(cache, fetch=fetch, now=NOW)
            self.assertEqual(first, 0)
            self.assertTrue((cache / "pg" / "ds.md").exists())
            self.assertFalse((cache / "pg" / "growth.md").exists())
            state = json.loads((cache / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["pg_backlog"], ["growth"])

            second = sync.sync(cache, fetch=fetch, now=NOW)
            self.assertEqual(second, 0)
            self.assertTrue((cache / "pg" / "ds.md").exists())
            self.assertTrue((cache / "pg" / "growth.md").exists())
            state = json.loads((cache / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["pg_backlog"], [])

    def test_fresh_does_not_fetch(self):
        with self._cache() as cache:

            def fetch(url, timeout=60):
                if url.endswith("/library"):
                    return 200, FIXTURE.read_bytes()
                if url.endswith("articles.html"):
                    return 200, b'<a href="ds.html">One</a>'
                if url.endswith("sitemap.xml"):
                    return 200, CAROUSEL_SITEMAP
                if url.endswith("/ds.html"):
                    return 200, pg_page("ds", "Do Things that Don't Scale", "recruit users manually")
                raise AssertionError(url)

            self.assertEqual(sync.sync(cache, fetch=fetch, now=NOW), 0)
            calls = []

            def explode(url, timeout=60):
                calls.append(url)
                raise AssertionError(url)

            self.assertEqual(sync.sync(cache, fetch=explode, now=NOW), 0)
            self.assertEqual(calls, [])

    def test_chapter_timestamp(self):
        with self._cache() as cache:
            from store import dump_frontmatter

            meta = {
                "title": "Hiring engineers",
                "author": "Y Combinator",
                "series": ["Office Hours"],
                "source_url": "https://www.ycombinator.com/library/hire",
                "youtube_url": "https://www.youtube.com/watch?v=abc123xyz09",
                "created_at": None,
                "chapters": [{"time": 218, "title": "How to hire the first engineer"}],
                "has_transcript": True,
                "kind": "library",
            }
            path = cache / "library" / "hire.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                dump_frontmatter(meta) + "\n\n# Hiring engineers\n\nHire slowly at first.\n",
                encoding="utf-8",
            )
            hits = search.search(cache, "hire the first engineer")
            self.assertEqual(hits[0]["youtube_url"], "https://www.youtube.com/watch?v=abc123xyz09&t=218")

    def test_opening_chapter_does_not_add_a_zero_timestamp(self):
        with self._cache() as cache:
            from store import dump_frontmatter

            meta = {
                "title": "Hiring engineers",
                "author": "Y Combinator",
                "series": ["Office Hours"],
                "source_url": "https://www.ycombinator.com/library/hire",
                "youtube_url": "https://www.youtube.com/watch?v=abc123xyz09",
                "created_at": None,
                "chapters": [{"time": 0, "title": "How to hire the first engineer"}],
                "has_transcript": True,
                "kind": "library",
            }
            path = cache / "library" / "hire.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                dump_frontmatter(meta) + "\n\n# Hiring engineers\n\nHire slowly at first.\n",
                encoding="utf-8",
            )
            hits = search.search(cache, "hire the first engineer")
            self.assertEqual(hits[0]["youtube_url"], "https://www.youtube.com/watch?v=abc123xyz09")

    def test_exact_title_beats_a_title_that_contains_it(self):
        with self._cache() as cache:
            self._write(cache, "pg", "ds.md", "Do Things that Don't Scale", "Recruit users manually.", kind="pg")
            self._write(
                cache,
                "library",
                "talk.md",
                "What it really means to do things that don't scale",
                "A long discussion of the essay.",
                kind="library",
                has_transcript=True,
            )
            hits = search.search(cache, "do things that don't scale")
            self.assertEqual(Path(hits[0]["path"]).name, "ds.md")

    def test_departed_carousel_item_is_refetched_from_sitemap(self):
        with self._cache() as cache:
            old = cache / "library" / "old-essay.md"
            old.parent.mkdir(parents=True)
            old.write_text("soon deleted\n", encoding="utf-8")
            (cache / "state.json").write_text(
                json.dumps(
                    {
                        "carousel_count": 1,
                        "carousel_slugs": ["old-essay"],
                        "sitemap_gap_fetched_at": NOW.isoformat(),
                        "pg_fetched_at": NOW.isoformat(),
                        "pg_count": 1,
                    }
                ),
                encoding="utf-8",
            )

            def fetch(url, timeout=60):
                if url.endswith("/library"):
                    return 200, FIXTURE.read_bytes()
                if url.endswith("sitemap.xml"):
                    xml = "<urlset><url><loc>https://www.ycombinator.com/library/old-essay</loc></url></urlset>"
                    return 200, xml.encode()
                if url.endswith("/old-essay"):
                    return 200, item_page("old-essay", "Older essay", "Still on the sitemap and worth keeping.")
                raise AssertionError(url)

            self.assertEqual(sync.sync(cache, fetch=fetch, now=NOW), 0)
            text = (cache / "library" / "old-essay.md").read_text(encoding="utf-8")
            self.assertIn("Still on the sitemap", text)
            self.assertTrue((cache / "library" / "ab-dont.md").exists())

    def test_daily_refresh_does_not_reset_essay_clock(self):
        with self._cache() as cache:

            def fetch(url, timeout=60):
                if url.endswith("/library"):
                    return 200, FIXTURE.read_bytes()
                if url.endswith("articles.html"):
                    return 200, b'<a href="ds.html">One</a>'
                if url.endswith("sitemap.xml"):
                    return 200, CAROUSEL_SITEMAP
                if url.endswith("/ds.html"):
                    return 200, pg_page("ds", "Do Things that Don't Scale", "recruit users manually")
                raise AssertionError(url)

            self.assertEqual(sync.sync(cache, fetch=fetch, now=NOW), 0)
            stamped = json.loads((cache / "state.json").read_text(encoding="utf-8"))
            later = NOW.replace(day=17)
            calls = []

            def carousel_only(url, timeout=60):
                calls.append(url)
                if url.endswith("/library"):
                    return 200, FIXTURE.read_bytes()
                raise AssertionError(url)

            self.assertEqual(sync.sync(cache, fetch=carousel_only, now=later), 0)
            self.assertEqual(calls, [sync.LIBRARY_URL])
            again = json.loads((cache / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(again["pg_fetched_at"], stamped["pg_fetched_at"])
            self.assertEqual(again["sitemap_gap_fetched_at"], stamped["sitemap_gap_fetched_at"])

    def test_sitemap_markup_failure_keeps_the_backlog(self):
        with self._cache() as cache:
            slugs = [f"essay{i}" for i in range(10)]
            (cache / "state.json").write_text(
                json.dumps(
                    {
                        "carousel_fetched_at": NOW.isoformat(),
                        "carousel_count": 1,
                        "carousel_slugs": ["ab-dont"],
                        "pg_fetched_at": NOW.isoformat(),
                        "sitemap_backlog": slugs,
                    }
                ),
                encoding="utf-8",
            )

            def fetch(url, timeout=60):
                return 200, b"<html>not an article</html>"

            self.assertEqual(sync.sync(cache, fetch=fetch, now=NOW), 2)
            state = json.loads((cache / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(state["sitemap_backlog"]), slugs)
            self.assertNotIn("sitemap_gap_fetched_at", state)

    def test_one_junk_sitemap_page_is_skipped(self):
        with self._cache() as cache:
            (cache / "state.json").write_text(
                json.dumps(
                    {
                        "carousel_fetched_at": NOW.isoformat(),
                        "carousel_count": 1,
                        "carousel_slugs": ["ab-dont"],
                        "pg_fetched_at": NOW.isoformat(),
                        "sitemap_backlog": ["junk", "kept"],
                    }
                ),
                encoding="utf-8",
            )

            def fetch(url, timeout=60):
                if url.endswith("/junk"):
                    return 200, b"<html>not an article</html>"
                if url.endswith("/kept"):
                    return 200, item_page("kept", "Kept essay", "Worth reading.")
                raise AssertionError(url)

            self.assertEqual(sync.sync(cache, fetch=fetch, now=NOW), 0)
            state = json.loads((cache / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["sitemap_backlog"], [])
            self.assertEqual(state["sitemap_skipped"], ["junk"])
            self.assertIn("Worth reading.", (cache / "library" / "kept.md").read_text(encoding="utf-8"))

    def test_blocked_sitemap_is_not_marked_done(self):
        with self._cache() as cache:
            slugs = [f"essay{i}" for i in range(10)]
            (cache / "state.json").write_text(
                json.dumps(
                    {
                        "carousel_fetched_at": NOW.isoformat(),
                        "carousel_count": 1,
                        "carousel_slugs": ["ab-dont"],
                        "pg_fetched_at": NOW.isoformat(),
                        "sitemap_backlog": slugs,
                    }
                ),
                encoding="utf-8",
            )

            def fetch(url, timeout=60):
                return 403, b""

            self.assertEqual(sync.sync(cache, fetch=fetch, now=NOW), 2)
            state = json.loads((cache / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(state["sitemap_backlog"]), slugs)
            self.assertNotIn("sitemap_gap_fetched_at", state)

    def test_pg_banner_is_not_searchable(self):
        banner = "Want to start a startup? Get funded by\nY Combinator."
        essay = " ".join(["recruit users manually"] * 80)
        page = f"<html><head><title>Do Things that Don't Scale</title></head><body><p>{banner}</p><p>{essay}</p></body></html>"
        with self._cache() as cache:
            self.assertTrue(sync._save_pg(cache, "ds", page.encode()))
            text = (cache / "pg" / "ds.md").read_text(encoding="utf-8")
            self.assertNotIn("Get funded by", text)
            self.assertIn("recruit users manually", text)

    def test_short_token_does_not_match_inside_a_word(self):
        with self._cache() as cache:
            self._write(cache, "library", "email.md", "Email tips", "Send a clearer message.", kind="library")
            self._write(cache, "library", "ai.md", "AI hiring", "Models can screen applicants.", kind="library")
            hits = search.search(cache, "ai")
            self.assertEqual([Path(hit["path"]).name for hit in hits], ["ai.md"])

    def test_title_outranks_body(self):
        with self._cache() as cache:
            self._write(cache, "library", "hire.md", "Hiring engineers", "nothing here", kind="library")
            self._write(cache, "library", "cook.md", "Cooking", "hiring engineers is hard", kind="library")
            hits = search.search(cache, "hiring engineers")
            self.assertEqual(Path(hits[0]["path"]).name, "hire.md")

    def test_pg_original_outranks_reprint(self):
        with self._cache() as cache:
            self._write(
                cache,
                "library",
                "96-do-things.md",
                "Do things that don't scale",
                "A shortened reprint.",
                kind="library",
                has_transcript=False,
            )
            self._write(
                cache,
                "pg",
                "ds.md",
                "Do Things that Don't Scale",
                "The original essay says to recruit users manually.",
                kind="pg",
                source_url="https://www.paulgraham.com/ds.html",
            )
            hits = search.search(cache, "do things that don't scale")
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["source_url"], "https://www.paulgraham.com/ds.html")

    def test_hiring_finds_hire_and_quotes_it(self):
        with self._cache() as cache:
            self._write(
                cache,
                "library",
                "slow.md",
                "Other topic",
                ("introduction " * 80) + "we hire slowly here",
                kind="library",
            )
            hits = search.search(cache, "hiring")
            self.assertEqual(Path(hits[0]["path"]).name, "slow.md")
            self.assertIn("hire", hits[0]["snippet"])
            self.assertFalse(hits[0]["snippet"].startswith("ntro"))

    def test_apostrophe_query_finds_folded_word(self):
        with self._cache() as cache:
            self._write(cache, "pg", "ds.md", "Notes", "dont scale this process with care", kind="pg")
            hits = search.search(cache, "don't")
            self.assertEqual(Path(hits[0]["path"]).name, "ds.md")

    def test_query_operators_are_words(self):
        with self._cache() as cache:
            self._write(cache, "library", "hire.md", "Hiring engineers", "nothing here", kind="library")
            hits = search.search(cache, "NOT hire*")
            self.assertEqual(Path(hits[0]["path"]).name, "hire.md")

    def test_new_file_is_found_on_the_next_search(self):
        with self._cache() as cache:
            self._write(cache, "library", "alpha.md", "Alpha", "alphaunique remains", kind="library")
            self.assertEqual(search.search(cache, "betaunique"), [])
            self._write(cache, "library", "beta.md", "Beta", "betaunique arrives", kind="library")
            hits = search.search(cache, "betaunique")
            self.assertEqual(Path(hits[0]["path"]).name, "beta.md")

    def test_deleted_file_is_not_found_on_the_next_search(self):
        with self._cache() as cache:
            self._write(cache, "library", "alpha.md", "Alpha", "alphaunique remains", kind="library")
            self._write(cache, "library", "beta.md", "Beta", "betaunique departs", kind="library")
            self.assertEqual(Path(search.search(cache, "betaunique")[0]["path"]).name, "beta.md")
            (cache / "library" / "beta.md").unlink()
            self.assertEqual(search.search(cache, "betaunique"), [])
            self.assertEqual(Path(search.search(cache, "alphaunique")[0]["path"]).name, "alpha.md")
            (cache / "library" / "alpha.md").unlink()
            self.assertEqual(search.search(cache, "alphaunique"), [])
            self.assertFalse((cache / "search.db").exists())

    def _write(self, cache, folder, name, title, body, kind, has_transcript=False, source_url="https://example.com"):
        from store import dump_frontmatter

        meta = {
            "title": title,
            "author": "Paul Graham" if kind == "pg" else "Y Combinator",
            "series": ["Paul Graham"] if kind == "pg" else ["Library"],
            "source_url": source_url,
            "youtube_url": None,
            "created_at": None,
            "chapters": [],
            "has_transcript": has_transcript,
            "kind": kind,
        }
        path = cache / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_frontmatter(meta) + f"\n\n# {title}\n\n{body}\n", encoding="utf-8")

    def _cache(self):
        return tempfile_cache()


class tempfile_cache:
    def __enter__(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        return Path(self._tmp.name)

    def __exit__(self, *args):
        self._tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
