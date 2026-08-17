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
    r"[\U0001F300-\U0001FAFF☀-✿]|@\w+"
)

# URL 必须先抽成占位符再分句: 链接里的 ? ! . / 都会被当终止符或硬切,
# 导致直播链接被截断。占位符在分句全程保持原子性, 最后还原。
_URL_RE = re.compile(r"https?://[^\s，,。！？；；））\]]+")
_URL_PLACEHOLDER = "⟦URL{idx}⟧"
_URL_PLACEHOLDER_RE = re.compile(r"⟦URL(\d+)⟧")

# 含链接的整行 (如直播推送 "第125期 【第一课】规划！...：https://...")
# 要作为一条完整消息推送, 标题里的 ！! ， 不能触发分句。
# 所以把"含 URL 占位符的整行"再抽成行级占位符, 分句全程不碰。
_LINE_PLACEHOLDER = "⟦LINE{idx}⟧"
_LINE_PLACEHOLDER_RE = re.compile(r"⟦LINE(\d+)⟧")
# 硬切时同时保护 URL / LINE 两类占位符, 都不被拦腰切断
_ANY_PLACEHOLDER_RE = re.compile(r"⟦(?:URL|LINE)\d+⟧")

_MAX_CHARS_PER_SENTENCE: Final = 40
_MIN_CHARS_PER_SENTENCE: Final = 3
_IDEAL_CHARS: Final = 18


def _hard_cut_keep_urls(part: str) -> list[str]:
    """超长片段按字数硬切, 但 URL/LINE 占位符永远保持原子、不被拦腰切断."""
    segs = _ANY_PLACEHOLDER_RE.split(part)  # [text0, text1, ..., textN]
    phs = _ANY_PLACEHOLDER_RE.findall(part)  # [ph0, ph1, ..., phN-1]
    pieces: list[str] = []
    current = ""
    for i, text_seg in enumerate(segs):
        # 纯文本部分先按 _IDEAL_CHARS 切段
        while len(current) + len(text_seg) > _IDEAL_CHARS and text_seg:
            take = _IDEAL_CHARS - len(current)
            if take <= 0:
                pieces.append(current)
                current = ""
                take = _IDEAL_CHARS
            current += text_seg[:take]
            text_seg = text_seg[take:]
            if len(current) >= _IDEAL_CHARS:
                pieces.append(current)
                current = ""
        current += text_seg
        # 占位符(完整 URL / 含链接整行)拼进当前句, 且当前句到此为止收口
        if i < len(phs):
            current += phs[i]
            pieces.append(current)
            current = ""
    if current.strip():
        pieces.append(current)
    return [p.strip() for p in pieces if p.strip()]


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

    # 第一步: 把所有 URL 抽成原子占位符, 分句全程不碰链接
    urls: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        urls.append(m.group(0))
        return _URL_PLACEHOLDER.format(idx=len(urls) - 1)

    text = _URL_RE.sub(_stash, text)
    text = re.sub(r"[ \t]+", " ", text.strip())

    # 第二步: 含 URL 占位符的整行抽成行级占位符 ——
    # 直播推送 "第125期 【第一课】规划！...：⟦URL0⟧" 要整行作为一条消息,
    # 标题里的 ！! ， 不能触发分句。
    lines: list[str] = []

    def _stash_line(line: str) -> str:
        if _URL_PLACEHOLDER_RE.search(line):
            lines.append(line)
            return _LINE_PLACEHOLDER.format(idx=len(lines) - 1)
        return line

    text = "\n".join(_stash_line(ln) for ln in text.split("\n"))

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
        # 整行占位符(含链接行)无论多长都保持完整, 不进硬切
        if _LINE_PLACEHOLDER_RE.fullmatch(part.strip()):
            result.append(part.strip())
        elif len(part) <= _MAX_CHARS_PER_SENTENCE:
            result.append(part)
        else:
            result.extend(_hard_cut_keep_urls(part))

    merged: list[str] = []
    for chunk in result:
        if (
            merged
            and len(chunk) < _MIN_CHARS_PER_SENTENCE
            and len(merged[-1]) + len(chunk) <= _MAX_CHARS_PER_SENTENCE
            and not _LINE_PLACEHOLDER_RE.search(chunk)
            and not _LINE_PLACEHOLDER_RE.search(merged[-1])
        ):
            merged[-1] += chunk
        else:
            merged.append(chunk)

    # 还原: 先行级占位符(内含 URL 占位符), 再还原 URL → 完整链接
    def _restore_line(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        return lines[idx] if idx < len(lines) else ""

    def _restore_url(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        return urls[idx] if idx < len(urls) else ""

    out: list[str] = []
    for chunk in merged:
        chunk = _LINE_PLACEHOLDER_RE.sub(_restore_line, chunk)
        chunk = _URL_PLACEHOLDER_RE.sub(_restore_url, chunk)
        out.append(chunk)
    return out


# ── Typing delay ──────────────────────────────────────────────────────

@dataclass
class TypingDelay:
    """模拟真实中文拼音输入速度的打字延迟计算器。

    参数调至普通人正常聊天节奏（约 3.3 字/秒），避免机器人一下子刷屏
    也避免慢得让人等不及。注意：URL 链接整段只算 1 个字符，防止一条
    直播链接把延迟拉到几十秒。

    参数 base=0.50, per_char=0.30 下的典型值（已排除 URL 计权）：

    +-----------+---------------+
    | 10 字     |  ~3.50s       |
    | 20 字     |  ~6.50s       |
    | 30 字     |  ~9.50s       |
    | 40 字     | ~12.00s (cap) |
    +-----------+---------------+
    """

    base: float = 0.50
    per_char: float = 0.30
    min_delay: float = 0.30
    max_delay: float = 12.0

    def for_sentence(self, sentence: str) -> float:
        if not sentence:
            return 0.0
        # URL 整段替换为单字符占位符，按 1 个字符计权（否则一条直播链接就 50+ 字）
        text = _URL_RE.sub("█", sentence)
        # emoji / @mention 不计入打字长度
        text = _EMOJI_OR_MENTION_RE.sub("", text)
        delay = self.base + len(text) * self.per_char
        return max(self.min_delay, min(self.max_delay, delay))

    def for_batch(self, sentences: list[str]) -> list[float]:
        """Per-sentence delays; index 0 is the pre-first-message pause."""
        if not sentences:
            return []
        delays = [self.base]
        for s in sentences:
            delays.append(self.for_sentence(s))
        return delays
