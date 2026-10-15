"""Cache layout for the YC Office Hours skill. No network."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

FRONTMATTER_KEYS = (
    "title",
    "author",
    "series",
    "source_url",
    "youtube_url",
    "created_at",
    "chapters",
    "has_transcript",
    "kind",
)

_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{6,32}")


def cache_dir() -> Path:
    return Path.home() / ".cache" / "yc-office-hours"


def safe_slug(slug: str) -> str:
    if not _SLUG.fullmatch(slug or ""):
        raise ValueError(f"unsafe slug: {slug!r}")
    return slug


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_state(cache: Path) -> dict:
    path = cache / "state.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(cache: Path, state: dict) -> None:
    atomic_write(cache / "state.json", json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def dump_frontmatter(meta: dict) -> str:
    lines = ["---"]
    for key in FRONTMATTER_KEYS:
        lines.append(f"{key}: {json.dumps(meta.get(key), ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def parse_document(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("missing frontmatter")
    end = None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            end = i
            break
    if end is None:
        raise ValueError("unclosed frontmatter")
    meta = {}
    for line in lines[1:end]:
        if ": " not in line:
            continue
        key, raw = line.split(": ", 1)
        meta[key] = json.loads(raw)
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body


def _chapters(raw) -> list[dict]:
    chapters = []
    for item in raw or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            seconds = int(float(item[0]))
        except (TypeError, ValueError):
            continue
        chapters.append({"time": seconds, "title": str(item[1])})
    return chapters


def library_markdown(article: dict, series: list[str]) -> str:
    slug = safe_slug(article["slug"])
    title = (article.get("title") or slug).replace("\n", " ").strip()
    transcript = article.get("transcript") if isinstance(article.get("transcript"), str) else ""
    content = article.get("content") if isinstance(article.get("content"), str) else ""
    youtube_id = article.get("youtube_id")
    youtube_url = (
        f"https://www.youtube.com/watch?v={youtube_id}"
        if isinstance(youtube_id, str) and _YOUTUBE_ID.fullmatch(youtube_id)
        else None
    )
    meta = {
        "title": title,
        "author": article.get("author"),
        "series": series,
        "source_url": f"https://www.ycombinator.com/library/{slug}",
        "youtube_url": youtube_url,
        "created_at": article.get("created_at"),
        "chapters": _chapters(article.get("video_chapters")),
        "has_transcript": bool(transcript.strip()),
        "kind": "library",
    }
    parts = [dump_frontmatter(meta), "", f"# {title}", ""]
    author = article.get("author")
    if isinstance(author, str) and author.strip():
        parts.extend([f"**{author.strip()}**", ""])
    parts.extend([f"[Source]({meta['source_url']})", ""])
    if content.strip():
        parts.extend([content.strip(), ""])
    if transcript.strip():
        parts.extend(["## Transcript", "", transcript.strip(), ""])
    return "\n".join(parts)


def pg_markdown(slug: str, title: str, text: str) -> str:
    slug = safe_slug(slug)
    title = title.replace("\n", " ").strip() or slug
    meta = {
        "title": title,
        "author": "Paul Graham",
        "series": ["Paul Graham"],
        "source_url": f"https://www.paulgraham.com/{slug}.html",
        "youtube_url": None,
        "created_at": None,
        "chapters": [],
        "has_transcript": False,
        "kind": "pg",
    }
    return "\n".join([dump_frontmatter(meta), "", f"# {title}", "", text.strip(), ""])


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
        if tag in {"p", "br", "h1", "h2", "h3", "li", "tr", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
        if tag in {"p", "h1", "h2", "h3", "li", "tr", "div"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    kept: list[str] = []
    blank = 0
    for line in lines:
        if not line:
            blank += 1
            if blank <= 1 and kept:
                kept.append("")
            continue
        blank = 0
        kept.append(line)
    return "\n".join(kept).strip()


def html_title(html: str) -> str | None:
    match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title or None
