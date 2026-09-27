"""Small printing helpers so lab output is readable in a terminal and in CI logs."""

from __future__ import annotations

import json
import textwrap
from typing import Any

from .pricing import usage_summary

WIDTH = 100


def header(title: str) -> None:
    print("\n" + "=" * WIDTH + f"\n{title}\n" + "=" * WIDTH)


def step(number: int | str, title: str) -> None:
    print(f"\n--- Step {number}: {title} " + "-" * max(0, WIDTH - len(str(title)) - len(str(number)) - 14))


def wrap(text: str, indent: str = "  ") -> str:
    out = []
    for para in str(text).splitlines() or [""]:
        out.append(textwrap.fill(para, WIDTH, initial_indent=indent, subsequent_indent=indent) if para else "")
    return "\n".join(out)


def text_of(message: Any) -> str:
    """Concatenate the text blocks of a Message (ignores thinking/tool_use blocks)."""
    return "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", None) == "text")


def print_json(obj: Any) -> None:
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def show_message(message: Any, *, show_usage: bool = True, max_text: int | None = 2000) -> None:
    """Print every content block of a Message with its type, then stop_reason + usage."""
    for block in message.content:
        kind = getattr(block, "type", "?")
        if kind == "text":
            text = block.text if max_text is None or len(block.text) <= max_text else block.text[:max_text] + " ..."
            print("[text]\n" + wrap(text))
        elif kind == "thinking":
            summary = block.thinking or "(thinking happened; its text is omitted by default - request "\
                                        "thinking={'type': 'adaptive', 'display': 'summarized'} to see a summary)"
            print("[thinking]\n" + wrap(summary))
        elif kind == "tool_use":
            print(f"[tool_use] {block.name}({json.dumps(block.input, ensure_ascii=False)})  id={block.id}")
        elif kind == "fallback":
            print(f"[fallback] {block.from_.model} declined -> {block.to.model} answered")
        elif kind == "compaction":
            print("[compaction]\n" + wrap(block.content or ""))
        else:
            print(f"[{kind}]")
    if show_usage:
        print(f"stop_reason={message.stop_reason}  model={message.model}  {usage_summary(message)}")
