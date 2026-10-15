#!/usr/bin/env python3
"""Rank the local YC cache with SQLite FTS5 BM25 and print the best snippets as JSON."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from store import cache_dir, parse_document

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for", "from",
    "how", "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "what", "with",
}
SCHEMA_VERSION = 1
_WORD = re.compile(r"[A-Za-z0-9']+")


class SearchError(Exception):
    """The search index cannot be built. main() turns this into exit code 2."""


def normalize(text: str) -> str:
    lowered = text.lower().replace("'", "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", lowered)).strip()


def tokens_of(query: str) -> list[str]:
    return [token for token in normalize(query).split() if len(token) > 1 and token not in STOPWORDS]


def _words(text: str) -> list[str]:
    words = []
    for match in _WORD.finditer(text):
        word = normalize(match.group())
        if word:
            words.append(word)
    return words


def _raise_if_no_fts5(error: sqlite3.OperationalError) -> None:
    if "fts5" in str(error).lower() or "no such module" in str(error).lower():
        raise SearchError("fts5 unavailable") from error


def _markdown_files(cache: Path) -> list[Path]:
    files = []
    for folder in ("library", "pg"):
        root = cache / folder
        if not root.exists():
            continue
        files.extend(path for path in root.glob("*.md") if path.is_file())
    return sorted(files, key=lambda path: path.relative_to(cache).as_posix())


def _fingerprint(cache: Path, files: list[Path]) -> str:
    parts = []
    for path in files:
        stat = path.stat()
        relative = path.relative_to(cache).as_posix()
        parts.append(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}")
    return "\n".join(parts)


def _unlink_sidecars(path: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _remove_db(cache: Path) -> None:
    for name in ("search.db", "search.db.building"):
        path = cache / name
        path.unlink(missing_ok=True)
        _unlink_sidecars(path)


def _load_documents(files: list[Path]) -> list[dict]:
    docs = []
    for path in files:
        try:
            meta, body = parse_document(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        meta["_path"] = path
        meta["_body"] = body
        docs.append(meta)
    return docs


def _is_reprint(doc: dict, pg_titles: set[str]) -> bool:
    title = normalize(doc.get("title") or "")
    return doc.get("kind") == "library" and not doc.get("has_transcript") and title in pg_titles


def _create_tables(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE docs USING fts5(
                title,
                series,
                author,
                body,
                title_text UNINDEXED,
                body_text UNINDEXED,
                path UNINDEXED,
                source_url UNINDEXED,
                youtube_url UNINDEXED,
                chapters UNINDEXED,
                series_json UNINDEXED,
                tokenize = 'porter'
            )
            """
        )
    except sqlite3.OperationalError as error:
        _raise_if_no_fts5(error)
        raise
    conn.execute("CREATE TABLE meta(fingerprint TEXT NOT NULL, version INTEGER NOT NULL)")


def _insert_documents(conn: sqlite3.Connection, docs: list[dict]) -> None:
    pg_titles = {normalize(doc.get("title") or "") for doc in docs if doc.get("kind") == "pg"}
    rows = []
    for doc in docs:
        if _is_reprint(doc, pg_titles):
            continue
        series = doc.get("series") if isinstance(doc.get("series"), list) else []
        chapters = doc.get("chapters") if isinstance(doc.get("chapters"), list) else []
        author = doc.get("author") if isinstance(doc.get("author"), str) else ""
        title = doc.get("title") or ""
        body = doc.get("_body") or ""
        rows.append(
            (
                normalize(title),
                normalize(" ".join(str(item) for item in series)),
                normalize(author),
                normalize(body),
                title,
                body,
                str(doc["_path"]),
                doc.get("source_url") or "",
                doc.get("youtube_url") or "",
                json.dumps(chapters),
                json.dumps(series),
            )
        )
    conn.executemany(
        """
        INSERT INTO docs(
            title, series, author, body, title_text, body_text, path, source_url,
            youtube_url, chapters, series_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _build(cache: Path, files: list[Path], fingerprint: str) -> None:
    temporary = cache / "search.db.building"
    temporary.unlink(missing_ok=True)
    _unlink_sidecars(temporary)
    conn = sqlite3.connect(temporary)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        _create_tables(conn)
        _insert_documents(conn, _load_documents(files))
        conn.execute(
            "INSERT INTO meta(fingerprint, version) VALUES (?, ?)",
            (fingerprint, SCHEMA_VERSION),
        )
        conn.commit()
    except Exception:
        conn.close()
        temporary.unlink(missing_ok=True)
        _unlink_sidecars(temporary)
        raise
    conn.close()
    _unlink_sidecars(temporary)
    destination = cache / "search.db"
    _unlink_sidecars(destination)
    os.replace(temporary, destination)
    _unlink_sidecars(destination)


def _open_index(cache: Path) -> sqlite3.Connection | None:
    files = _markdown_files(cache)
    if not files:
        _remove_db(cache)
        return None
    fingerprint = _fingerprint(cache, files)
    path = cache / "search.db"
    if path.exists():
        conn = None
        try:
            conn = sqlite3.connect(path)
            row = conn.execute("SELECT fingerprint, version FROM meta").fetchone()
            if row and row[0] == fingerprint and row[1] == SCHEMA_VERSION:
                found = conn
                conn = None
                return found
        except sqlite3.DatabaseError:
            pass
        finally:
            if conn is not None:
                conn.close()
    _build(cache, files, fingerprint)
    return sqlite3.connect(cache / "search.db")


def _porter_stems(words: list[str]) -> dict[str, str]:
    cleaned = []
    seen = set()
    for word in words:
        word = normalize(word)
        if word and word not in seen:
            seen.add(word)
            cleaned.append(word)
    if not cleaned:
        return {}
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE stems USING fts5(word, tokenize = 'porter')")
        conn.executemany("INSERT INTO stems(rowid, word) VALUES (?, ?)", enumerate(cleaned, start=1))
        conn.execute("CREATE VIRTUAL TABLE stem_terms USING fts5vocab(stems, 'instance')")
        mapped = {word: word for word in cleaned}
        used = set()
        for term, doc, _column, _offset in conn.execute("SELECT term, doc, col, offset FROM stem_terms"):
            if doc in used or not 1 <= doc <= len(cleaned):
                continue
            used.add(doc)
            mapped[cleaned[doc - 1]] = term
        return mapped
    except sqlite3.OperationalError as error:
        _raise_if_no_fts5(error)
        raise
    finally:
        conn.close()


def _snippet(body: str, tokens: list[str], stems: dict[str, str]) -> str:
    haystack = re.sub(r"\s+", " ", body).strip()
    if not haystack:
        return ""
    query_stems = {stems.get(token, token) for token in tokens}
    index = -1
    for match in _WORD.finditer(haystack):
        word = normalize(match.group())
        if word and stems.get(word, word) in query_stems:
            index = match.start()
            break
    if index == -1:
        return haystack[:400]
    start = max(0, index - 160)
    if start > 0:
        boundary = haystack.find(" ", start, index)
        if boundary != -1:
            start = boundary + 1
    end = min(len(haystack), start + 400)
    if end < len(haystack):
        boundary = haystack.rfind(" ", index, end)
        if boundary != -1:
            end = boundary
    return haystack[start:end].strip()


def _youtube(url: str, chapters: list, tokens: list[str], stems: dict[str, str]) -> str | None:
    if not url:
        return None
    query_stems = {stems.get(token, token) for token in tokens}
    best = None
    best_hits = 0
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        words = _words(chapter.get("title") or "")
        hits = sum(1 for word in words if stems.get(word, word) in query_stems)
        if hits > best_hits:
            best_hits = hits
            best = chapter
    if not best:
        return url
    try:
        seconds = int(best["time"])
    except (KeyError, TypeError, ValueError):
        return url
    if seconds <= 0:
        return url
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}t={seconds}"


def _match_query(tokens: list[str]) -> str:
    return " OR ".join(f'"{token}"' for token in tokens)


def _phrase_rank(title: str, phrase: str) -> int:
    normalized = normalize(title)
    if " " not in phrase or phrase not in normalized:
        return 2
    if phrase == normalized:
        return 0
    return 1


def _rows(conn: sqlite3.Connection, tokens: list[str]) -> list[dict]:
    try:
        # Column order is title, series, author, body.
        cursor = conn.execute(
            """
            SELECT title_text, body_text, path, source_url, youtube_url, chapters, series_json,
                   bm25(docs, 8.0, 3.0, 3.0, 1.0) AS score
            FROM docs
            WHERE docs MATCH ?
            """,
            (_match_query(tokens),),
        )
    except sqlite3.OperationalError as error:
        _raise_if_no_fts5(error)
        raise
    rows = []
    for title, body, path, source_url, youtube_url, chapters, series_json, rank in cursor:
        try:
            series = json.loads(series_json)
        except (TypeError, json.JSONDecodeError):
            series = []
        try:
            chapter_list = json.loads(chapters)
        except (TypeError, json.JSONDecodeError):
            chapter_list = []
        rows.append(
            {
                "title": title or "",
                "body": body or "",
                "path": path,
                "source_url": source_url or "",
                "youtube_url": youtube_url or "",
                "chapters": chapter_list if isinstance(chapter_list, list) else [],
                "series": series if isinstance(series, list) else [],
                "score": rank,
            }
        )
    return rows


def search(cache: Path, query: str, limit: int = 5) -> list[dict]:
    tokens = tokens_of(query)
    if not tokens:
        return []
    conn = _open_index(cache)
    if conn is None:
        return []
    try:
        rows = _rows(conn, tokens)
    finally:
        conn.close()
    phrase = normalize(query)
    rows.sort(key=lambda row: (_phrase_rank(row["title"], phrase), row["score"], row["title"]))
    chosen = rows[:limit]
    words = list(tokens)
    for row in chosen:
        words.extend(_words(row["body"]))
        words.extend(_words(row["title"]))
        for chapter in row["chapters"]:
            if isinstance(chapter, dict):
                words.extend(_words(chapter.get("title") or ""))
    stems = _porter_stems(words)
    hits = []
    for row in chosen:
        hits.append(
            {
                "path": row["path"],
                "title": row["title"],
                "series": row["series"],
                "source_url": row["source_url"],
                "youtube_url": _youtube(row["youtube_url"], row["chapters"], tokens, stems),
                "snippet": _snippet(row["body"], tokens, stems),
            }
        )
    return hits


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: search.py QUERY", file=sys.stderr)
        return 2
    try:
        hits = search(cache_dir(), " ".join(args))
    except SearchError as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 2
    json.dump(hits, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
