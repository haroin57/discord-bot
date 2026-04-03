"""Tests for utils/discord_helpers.py — split_message, strip_mention, build_mention_regex."""

from config import BOT_ROLE_IDS
from utils.discord_helpers import build_mention_regex, split_message, strip_mention


class TestSplitMessage:
    def test_short_message(self):
        assert split_message("hello") == ["hello"]

    def test_splits_on_newline(self):
        text = "a" * 1800 + "\n" + "b" * 400  # 2201 chars, must split
        chunks = split_message(text, limit=2000)
        assert len(chunks) == 2
        assert len(chunks[0]) <= 2000

    def test_splits_on_space(self):
        text = "word " * 500  # 2500 chars
        chunks = split_message(text, limit=2000)
        assert all(len(c) <= 2000 for c in chunks)

    def test_hard_split_no_separator(self):
        text = "x" * 3000
        chunks = split_message(text, limit=2000)
        assert len(chunks) == 2
        assert chunks[0] == "x" * 2000

    def test_empty_string(self):
        assert split_message("") == [""]

    def test_exact_limit(self):
        text = "a" * 2000
        assert split_message(text, limit=2000) == [text]


class TestStripMention:
    def test_strips_direct_mention(self):
        BOT_ROLE_IDS.clear()
        build_mention_regex(123456)
        result = strip_mention("<@123456> hello", 123456)
        assert result == "hello"

    def test_strips_nick_mention(self):
        BOT_ROLE_IDS.clear()
        build_mention_regex(123456)
        result = strip_mention("<@!123456> hello", 123456)
        assert result == "hello"

    def test_strips_role_mention(self):
        BOT_ROLE_IDS.clear()
        BOT_ROLE_IDS.add(789)
        build_mention_regex(123456)
        result = strip_mention("<@&789> hello", 123456)
        assert result == "hello"

    def test_strips_keyword(self):
        BOT_ROLE_IDS.clear()
        build_mention_regex(123456)
        # "かなえ" is in BOT_KEYWORDS by default
        result = strip_mention("かなえ、元気？", 123456)
        assert "かなえ" not in result
        assert "元気" in result

    def test_preserves_other_text(self):
        BOT_ROLE_IDS.clear()
        build_mention_regex(123456)
        result = strip_mention("<@123456> これは大事なメッセージ", 123456)
        assert result == "これは大事なメッセージ"
