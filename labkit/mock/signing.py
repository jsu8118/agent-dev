"""Thinking-block signatures, with the binding rules of the current API.

The real API signs every thinking block and rejects a request whose thinking blocks were edited or
fabricated.  On current models the signature also records **which model produced the block** and **the
conversation prefix it was produced in** ("preserved thinking"):

* a block is readable only by models of the same or a newer family (model binding): other models have
  it dropped before generation, unbilled;
* on models that enforce the prefix check, editing anything *before* the block (system prompt, tool set,
  earlier turns) invalidates it: the request is rejected, or the block is dropped when the request opts
  into `thinking.block_binding.prefix_mismatch_behavior: "drop_block"`.

The mock does the same with an HMAC.  A signature looks like
``mock-sig-<model>.<prefix digest>.<mac>``; `parse()` recovers the model and prefix digest, `verify()`
checks the MAC (so an edited thinking text or a hand-written signature is caught), and the validator in
`validate.py` compares the recorded prefix digest with the prefix it can see in the request.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

_SECRET = b"labkit-mock-thinking-signature"
SIGNATURE_PREFIX = "mock-sig-"
TOOL_ID_PREFIX = "toolu_mock_"
SERVER_TOOL_ID_PREFIX = "srvtoolu_mock_"


def _mac(thinking_text: str, model: str, prefix: str) -> str:
    payload = f"{model}\x1f{prefix}\x1f{thinking_text}".encode("utf-8")
    return hmac.new(_SECRET, payload, hashlib.sha256).hexdigest()[:40]


def sign(thinking_text: str, *, model: str = "", prefix: str = "") -> str:
    """Sign a thinking block produced by `model` in a conversation whose prefix digest is `prefix`."""
    return f"{SIGNATURE_PREFIX}{model}.{prefix[:16]}.{_mac(thinking_text, model, prefix[:16])}"


def parse(signature: str) -> dict[str, str] | None:
    """Split a mock signature into its parts, or None if it isn't one."""
    if not isinstance(signature, str) or not signature.startswith(SIGNATURE_PREFIX):
        return None
    parts = signature[len(SIGNATURE_PREFIX):].split(".")
    if len(parts) != 3:
        return None
    return {"model": parts[0], "prefix": parts[1], "mac": parts[2]}


def verify(thinking_text: str, signature: str) -> bool:
    """True when the signature was produced by the mock for exactly this thinking text."""
    parts = parse(signature)
    if parts is None:
        return False
    return hmac.compare_digest(_mac(thinking_text, parts["model"], parts["prefix"]), parts["mac"])


# ------------------------------------------------------------------------------------------ prefix digests
def _canonical(value: Any) -> str:
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k != "cache_control"}
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _message_bytes(message: dict) -> str:
    """A message without its cache markers; thinking blocks contribute only their signature (the chain)."""
    content = message.get("content")
    if isinstance(content, str):
        blocks: list[Any] = [{"type": "text", "text": content}]
    else:
        blocks = []
        for block in content or []:
            if isinstance(block, dict) and block.get("type") in ("thinking", "redacted_thinking"):
                blocks.append({"type": block["type"], "signature": block.get("signature", "")})
            else:
                blocks.append(block)
    extra = {k: v for k, v in message.items() if k not in ("content", "role")}
    return _canonical({"role": message.get("role"), "content": blocks, **extra})


def prefix_digest(body: dict, upto: int) -> str:
    """Digest of the conversation prefix a thinking block in message `upto` is bound to.

    Covers the top-level system prompt, the tool set (as a name-sorted set, so reordering is harmless and adding
    a `defer_loading` tool nothing referenced yet is harmless too) and every message before `upto`.  When the
    history contains a compaction block, the prefix starts at the message that carries the most recent one.
    """
    messages = body.get("messages") or []
    start = 0
    for i, m in enumerate(messages[:upto + 1]):
        content = m.get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "compaction" for b in content):
            start = i                      # a block in the compaction turn itself sees no earlier message
    tools = body.get("tools") or []
    loaded = sorted((_canonical(t) for t in tools if isinstance(t, dict) and not t.get("defer_loading")))
    digest = hashlib.sha256()
    digest.update(_canonical(body.get("system") or "").encode("utf-8"))
    digest.update(_canonical(loaded).encode("utf-8"))
    for m in messages[start:upto]:
        digest.update(_message_bytes(m).encode("utf-8"))
    return digest.hexdigest()[:16]
