"""Thinking-block signatures.

The real API signs every thinking block and rejects a request whose thinking blocks
were edited or fabricated.  The mock does the same with an HMAC, which catches a
classic agent-loop bug: rebuilding assistant turns by hand instead of appending
`response.content` verbatim.
"""

from __future__ import annotations

import hashlib
import hmac

_SECRET = b"labkit-mock-thinking-signature"
SIGNATURE_PREFIX = "mock-sig-"
TOOL_ID_PREFIX = "toolu_mock_"


def sign(thinking_text: str) -> str:
    digest = hmac.new(_SECRET, thinking_text.encode("utf-8"), hashlib.sha256).hexdigest()[:40]
    return SIGNATURE_PREFIX + digest


def verify(thinking_text: str, signature: str) -> bool:
    return hmac.compare_digest(sign(thinking_text), signature or "")
