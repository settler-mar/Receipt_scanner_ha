"""Minimal HTML tree for adapters that scrape a store's rendered cabinet.

Only the class-based lookups the scraping adapters actually need (vencon.py,
liki.py) — no dependency on lxml/bs4, which the project deliberately doesn't
ship. Adapters that only need one table or one attribute keep using their own
small HTMLParser subclass (metro.py); this is for the ones that walk a card
layout by CSS class.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    own_text: list[str] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def text(self) -> str:
        chunks = [*self.own_text]
        for child in self.children:
            chunks.append(child.text())
        return " ".join(" ".join(chunks).split())

    def descendants(self):
        for child in self.children:
            yield child
            yield from child.descendants()

    def find_class(self, class_name: str) -> "Node | None":
        return next((node for node in self.descendants() if class_name in node.classes), None)

    def find_all_class(self, class_name: str) -> list["Node"]:
        return [node for node in self.descendants() if class_name in node.classes]


class _TreeParser(HTMLParser):
    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in self._void:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.stack[-1].own_text.append(data)


def tree(html_text: str) -> Node:
    parser = _TreeParser()
    parser.feed(html_text)
    return parser.root


def text_of(fragment: str) -> str:
    """Tags stripped, entities decoded, whitespace collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def number_in(fragment: str | None) -> float | None:
    """First number in a text fragment, tolerating "1 234,56" thousand groups."""
    if not fragment:
        return None
    match = re.search(r"-?\d[\d\s ]*(?:[,.]\d+)?", fragment)
    if not match:
        return None
    return float(match.group(0).replace(" ", "").replace(" ", "").replace(",", "."))
