"""Sentence splitter and typing-delay scheduler for partner streaming.

Why this exists:
    WeChat (and many IM channels) don't support token-level streaming. The
    cleanest workaround is to collect the agent's final answer, split it
    into natural short sentences, and push each one as a separate message
    with a realistic typing delay. Downstream IM bridges then forward each
    sentence to the user as it arrives.

Two public entry points:

- :func:`split_sentences` — chop a full answer into short natural chunks.
- :class:`TypingDelay` — compute a realistic delay for each chunk.

Both are pure functions / data objects so they're trivial to unit-test and
can be reused by any channel (Feishu / WeChat / custom bots).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

_HARD_TERMINATORS: Final = set("。！？.!?")
_SOFT_TERMINATORS: Final = set("，,")
_EMOJI_OR_MENTION_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF]|@\w+"
)

_MAX_CHARS_PER_SENTENCE: Final = 40
_MIN_CHARS_PER_SENTENCE: Final = 3
_IDEAL_CHARS: Final = 18


def split_sentences(text: str) -> list[str]:
    """Split *text* into short natural sentences.

    Rules:

    1. Split on hard terminators ``。！？.!?`` + newlines — hard breaks.
    2. Split on commas ``，,`` only if the segment already contains
       >= :data:`_IDEAL_CHARS` characters.
    3. Anything still longer than :data:`_MAX_CHARS_PER_SENTENCE` is split
       by character count.
    4. Whitespace-only and tiny fragments are dropped; tiny trailing
       fragments get merged back into the previous sentence.

    Always non-empty for non-empty input — worst case returns the whole
    text as one chunk.
    """
    if not text or not text.strip():
        return []

    text = re.sub(r"[ \t]+", " ", text.strip())

    raw_parts: list[str] = []
    buffer = ""
    for ch in text:
        buffer += ch
        if ch == "\n" or ch in _HARD_TERMINATORS:
            chunk = buffer.strip()
            if chunk:
                raw_parts.append(chunk)
            buffer = ""
        elif ch in _SOFT_TERMINATORS and len(buffer.rstrip()) >= _IDEAL_CHARS:
            chunk = buffer.strip()
            if chunk:
                raw_parts.append(chunk)
            buffer = ""
    tail = buffer.strip()
    if tail:
        raw_parts.append(tail)

    result: list[str] = []
    for part in raw_parts:
        if len(part) <= _MAX_CHARS_PER_SENTENCE:
            result.append(part)
        else:
            for i in range(0, len(part), _IDEAL_CHARS):
                piece = part[i : i + _IDEAL_CHARS].strip()
                if piece:
                    result.append(piece)

    merged: list[str] = []
    for chunk in result:
        if (
            merged
            and len(chunk) < _MIN_CHARS_PER_SENTENCE
            and len(merged[-1]) + len(chunk) <= _MAX_CHARS_PER_SENTENCE
        ):
            merged[-1] += chunk
        else:
            merged.append(chunk)

    return merged


# ── Typing delay ──────────────────────────────────────────────────────

@dataclass
class TypingDelay:
    """Compute the delay before sending the next sentence.

    Calibrated so the total answer time is faster than human typing but
    slow enough to feel natural — not like a bot dumping everything at
    once. Typical range with ``base=0.25, per_char=0.04``:

    +---------+---------------+
    | 10 字   |  ~0.65s       |
    | 20 字   |  ~1.05s       |
    | 30 字   |  ~1.45s       |
    +---------+---------------+
    """

    base: float = 0.25
    per_char: float = 0.04
    min_delay: float = 0.15
    max_delay: float = 1.2

    def for_sentence(self, sentence: str) -> float:
        if not sentence:
            return 0.0
        visible = _EMOJI_OR_MENTION_RE.sub("", sentence)
        delay = self.base + len(visible) * self.per_char
        return max(self.min_delay, min(self.max_delay, delay))

    def for_batch(self, sentences: list[str]) -> list[float]:
        """Per-sentence delays; index 0 is the pre-first-message pause."""
        if not sentences:
            return []
        delays = [self.base]
        for s in sentences:
            delays.append(self.for_sentence(s))
        return delays
