"""High-throughput rule pre-filter to detect noise before calling AI."""

from __future__ import annotations

import re
from typing import Optional, Tuple
from models import IncomingMessage, PriorityClassification

# Common trivial responses and chatter (in English and common transliterations)
NOISE_PATTERNS = {
    "ok", "okay", "k", "kk", "yes", "yep", "yeah", "yup", "no", "nope",
    "thanks", "thx", "ty", "thank you", "tq", "np", "welcome",
    "haha", "hahaha", "hahahaha", "lol", "lmao", "rofl", "hehe", "xd",
    "hi", "hello", "hey", "gm", "gn", "good morning", "good night", "bye",
    "cool", "nice", "great", "wow", "gg", "done", "got it", "noted",
    "👍", "🙏", "❤️", "😂", "🤣", "🔥", "🎉", "💯", "👀", "✅", "🙌", "👏",
}

# Regex for strings made entirely of emojis, repeated punctuation, or laughter
EMOJI_OR_LAUGHTER_REGEX = re.compile(
    r"^[\s\U00010000-\U0010ffff\u2600-\u26ff\u2700-\u27bf\.\,\!\?\:\;\)\(\-\_]+$"
)
LAUGHTER_REGEX = re.compile(r"^(ha|he|ja|55|lol|xd)+$", re.IGNORECASE)


def evaluate_prefilter(msg: IncomingMessage, vip_senders: list[str]) -> Optional[PriorityClassification]:
    """
    Evaluates if a message is obvious noise (P3) and can skip AI classification.
    Returns PriorityClassification if prefiltered as P3 noise, or None if AI evaluation is required.
    """
    # If sender is VIP, never pre-filter (always let AI evaluate context)
    sender_name_lower = (msg.sender_name or "").lower()
    sender_user_lower = (msg.sender_username or "").lower()
    for vip in vip_senders:
        vip_clean = vip.lower().lstrip("@")
        if vip_clean in sender_name_lower or vip_clean in sender_user_lower:
            return None

    raw_text = (msg.text or "").strip()
    raw_lower = raw_text.lower()

    # 1. Sticker or media without caption
    if msg.media_type == "sticker":
        return PriorityClassification(
            priority="P3",
            score=5,
            reason="Telegram sticker without text content.",
            needs_action=False,
            action=None,
            deadline=None,
            category="noise",
            summary=f"Sticker from {msg.sender_name}",
        )

    # 2. Empty text
    if not raw_text:
        return PriorityClassification(
            priority="P3",
            score=10,
            reason="Media without text caption.",
            needs_action=False,
            action=None,
            deadline=None,
            category="noise",
            summary=f"Media from {msg.sender_name}",
        )

    # 3. Exact match trivial phrase
    clean_punct = re.sub(r"[^\w\s]", "", raw_lower).strip()
    if clean_punct in NOISE_PATTERNS or raw_lower in NOISE_PATTERNS:
        return PriorityClassification(
            priority="P3",
            score=15,
            reason="Generic reaction / acknowledgment phrase.",
            needs_action=False,
            action=None,
            deadline=None,
            category="noise",
            summary=f"{msg.sender_name}: '{raw_text}'",
        )

    # 4. Emoji-only or laughter-only message
    if EMOJI_OR_LAUGHTER_REGEX.match(raw_text) or LAUGHTER_REGEX.match(clean_punct):
        return PriorityClassification(
            priority="P3",
            score=10,
            reason="Emoji-only or laughter reaction.",
            needs_action=False,
            action=None,
            deadline=None,
            category="noise",
            summary=f"{msg.sender_name} reacted with emojis/laughter",
        )

    # If none of the fast filters match, message requires AI classification
    return None
