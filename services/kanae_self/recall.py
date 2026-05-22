"""Interest-driven recall — augment search queries with top interest topics.

Phase 2: the bot's mem0 search for kanae's own namespace runs on the
user's raw message. If kanae cares about UK Garage and the user says
"あれ覚えてる？", the raw query has no anchor for the retriever to use.
This module appends the top-k interest topics to the query so the
embedding search has something concrete to match against.

Kept tiny on purpose — the actual augmentation is a string join, but
isolating it here lets the caller test it without spinning up the bot
event loop, and gives Phase 3 a single place to swap in a smarter
rewrite (e.g. weighted by salience) without re-touching bot.py.
"""

from __future__ import annotations

from services.kanae_self.interests import top_interests
from services.kanae_self.state import KanaeSelfState

# Cap the topics injected into one query. Beyond this the embedding
# starts to dilute — adding noise rather than signal.
DEFAULT_TOPIC_COUNT = 3


def augment_query_with_interests(
    query: str,
    state: KanaeSelfState | None,
    *,
    k: int = DEFAULT_TOPIC_COUNT,
    channel_id: str = "",
) -> str:
    """Return `query` with top-k interest topics appended.

    Topics are joined as "...query... | topics: a / b / c". Returns the
    original query unchanged when `state` is None, has no interests, or
    `k <= 0`. When ``channel_id`` is provided, same-channel topics are
    boosted via ``top_interests`` so the embedding rewrite steers toward
    local context.
    """
    if not query or not state or k <= 0:
        return query or ""
    topics = [i.topic for i in top_interests(state, k=k, channel_id=channel_id)]
    if not topics:
        return query
    return f"{query} | topics: {' / '.join(topics)}"


def top_interest_topics(
    state: KanaeSelfState | None,
    *,
    k: int = DEFAULT_TOPIC_COUNT,
    channel_id: str = "",
) -> list[str]:
    """Return up to `k` canonical topic strings, highest salience first.

    Useful for callers that want the bare list (e.g. to add to a
    metadata tag) rather than the augmented query string. When
    ``channel_id`` is provided, same-channel topics are prioritized.
    """
    if not state or k <= 0:
        return []
    return [i.topic for i in top_interests(state, k=k, channel_id=channel_id)]
