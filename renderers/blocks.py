from __future__ import annotations

from copy import copy

from ..core.models import ContentBlock

_MEDIA = {"image", "video", "audio", "attachment"}


def truncate_content_blocks(blocks: list[ContentBlock], limit: int) -> list[ContentBlock]:
    """Copy ordered blocks and trim text recursively without mutating stored articles."""
    remaining = max(0, limit)
    result: list[ContentBlock] = []
    for block in blocks:
        clipped, remaining, keep = _clip(block, remaining)
        if keep:
            result.append(clipped)
    return result


def _clip(block: ContentBlock, remaining: int) -> tuple[ContentBlock, int, bool]:
    kind = block.type.lower()
    if kind in _MEDIA:
        return copy(block), remaining, bool(block.url)
    if block.children:
        children: list[ContentBlock] = []
        current = remaining
        for child in block.children:
            clipped, current, keep = _clip(child, current)
            if keep:
                children.append(clipped)
        if block.text and current > 0:
            text, current = _clip_text(block.text, current)
            return (
                ContentBlock(block.type, text=text, url=block.url, children=children, attrs=dict(block.attrs)),
                current,
                bool(text or children),
            )
        return (
            ContentBlock(block.type, text="", url=block.url, children=children, attrs=dict(block.attrs)),
            current,
            bool(children),
        )
    if kind == "link" and block.url:
        return copy(block), remaining, True
    if not block.text:
        return copy(block), remaining, bool(block.url)
    text, remaining = _clip_text(block.text, remaining)
    return ContentBlock(block.type, text=text, url=block.url, attrs=dict(block.attrs)), remaining, bool(text)


def _clip_text(value: str, remaining: int) -> tuple[str, int]:
    if len(value) <= remaining:
        return value, remaining - len(value)
    if remaining <= 0:
        return "", 0
    clipped = value[:remaining]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return (clipped + "…") if clipped else "", 0
