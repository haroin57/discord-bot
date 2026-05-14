"""Unit tests for services.vscode_sync.redactor."""

from services.vscode_sync.redactor import redact


def test_webhook_url_redacted():
    s = "post to https://discord.com/api/webhooks/1234567890/abcDEF-secret_token here"
    out = redact(s)
    assert "discord.com/api/webhooks" not in out
    assert "<webhook>" in out


def test_discordapp_webhook_redacted():
    out = redact("url: https://discordapp.com/api/webhooks/999/xyz123")
    assert "<webhook>" in out


def test_bearer_token_redacted():
    s = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz1234"
    out = redact(s)
    assert "abcdefghijklmnopqrstuvwxyz1234" not in out
    assert "Bearer <redacted>" in out


def test_anthropic_api_key_redacted():
    out = redact("key=sk-ant-api03-abc123def456ghi789jkl0")
    assert "sk-ant-api03" not in out
    assert "<api_key>" in out


def test_openai_api_key_redacted():
    out = redact("OPENAI_API_KEY=sk-proj_abcdefghijklmnopqrst")
    assert "<api_key>" in out


def test_github_pat_redacted():
    out = redact("TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1")
    assert "<github_token>" in out


def test_github_fine_grained_pat_redacted():
    out = redact("TOKEN=github_pat_11abcdefghijklmnopqrstuvwx")
    assert "<github_token>" in out


def test_slack_token_redacted():
    out = redact("slack=xoxb-1234-5678-abcdefghij")
    assert "<slack_token>" in out


def test_user_mention_redacted():
    assert redact("hey <@1037285234200297522> look here") == "hey <mention> look here"


def test_role_mention_redacted():
    assert redact("<@&999999> all of you") == "<mention> all of you"


def test_everyone_redacted():
    assert redact("@everyone please") == "<mention> please"


def test_here_redacted():
    assert redact("@here team") == "<mention> team"


def test_plain_text_unchanged():
    s = "this is just regular text with no secrets"
    assert redact(s) == s


def test_empty_string():
    assert redact("") == ""


def test_multiple_patterns_in_one_string():
    s = "Bearer abc123def456ghi789jkl012mno3 and @everyone with ghp_abcdefghijklmnopqrstuvwxyz9"
    out = redact(s)
    assert "Bearer <redacted>" in out
    assert "<mention>" in out
    assert "<github_token>" in out
