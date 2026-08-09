"""Readable-text extraction from HTML mail bodies (stdlib only)."""

from __future__ import annotations

import re
from html.parser import HTMLParser


class _HTMLToText(HTMLParser):
    _SKIP = {"script", "style", "head", "title"}
    _BLOCK = {"p", "div", "tr", "table", "ul", "ol", "blockquote", "section", "article",
              "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "br":
            self._chunks.append("\n")
        elif tag == "li":
            self._chunks.append("\n- ")
        elif tag in self._BLOCK:
            self._chunks.append("\n")
        elif tag == "a":
            self._hrefs.append(dict(attrs).get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self._chunks.append("\n")
        elif tag == "a" and self._hrefs:
            href = self._hrefs.pop()
            if href and not href.startswith(("mailto:", "#")):
                self._chunks.append(f" <{href}>")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        raw = re.sub(r"[ \t]+", " ", raw)
        lines = [line.strip() for line in raw.split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> str:
    parser = _HTMLToText()
    parser.feed(html)
    parser.close()
    return parser.text() or "(empty HTML body)"
