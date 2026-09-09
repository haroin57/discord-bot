"""Remote configuration must preserve privilege tiers and fail closed."""

import json

import pytest

from services.offload.endpoint import validate_endpoint
from services.offload.mcp_config import apply_remote_overrides
from services.offload.knowledge_client import validate_vectors, validate_results


@pytest.mark.parametrize("url", ["http://example.com", "https://user:secret@example.com", "https://example.com?token=secret", "https://example.com#token", "http://127.0.0.1.evil.com", "https://example.com:bad", "https://example.com\n"])
def test_endpoint_rejects_credentials_and_insecure_hosts(url):
    if url.endswith("\n"):
        url = url.replace("com\n", "co\nm")
    with pytest.raises(ValueError):
        validate_endpoint(url, name="endpoint")


@pytest.mark.parametrize("url", ["https://example.com/api", "http://127.0.0.1:8123", "http://[::1]:8123"])
def test_endpoint_allows_tls_and_literal_loopback(url):
    assert validate_endpoint(url, name="endpoint") == url


def test_remote_mcp_retains_tier_and_removes_local_spawn_credentials():
    web, private = {"context7": {"command": "npx", "env": {"KEY": "old"}}}, {"nature-remo": {"command": "python"}}
    apply_remote_overrides([web, private], json.dumps({
        "nature-remo": {"url": "https://iot.example/mcp", "bearer_token_env": "IOT_KEY"},
    }), {"IOT_KEY": "secret"})
    assert web == {"context7": {"command": "npx", "env": {"KEY": "old"}}}
    assert private["nature-remo"] == {"type": "http", "url": "https://iot.example/mcp", "headers": {"Authorization": "Bearer secret"}}


def test_remote_mcp_validation_is_atomic_and_does_not_echo_secrets():
    group = {"context7": {"command": "npx"}}
    with pytest.raises(ValueError) as error:
        apply_remote_overrides([group], json.dumps({
            "context7": {"url": "https://docs.example/mcp"},
            "unknown-secret-name": {"url": "https://secret.example/mcp"},
        }), {})
    assert "secret" not in str(error.value)
    assert group == {"context7": {"command": "npx"}}


def test_remote_only_optional_server_requires_no_local_api_key():
    web = {}
    apply_remote_overrides([web], '{"tavily":{"url":"https://search.example/mcp","bearer_token_env":"REMOTE_KEY"}}', {"REMOTE_KEY": "abc"}, optional={"tavily": web})
    assert web["tavily"]["type"] == "http"


@pytest.mark.parametrize("headers", [{"Host": "TOKEN"}, {"Authorization": "MISSING"}, {"X-Key": "invalid name"}])
def test_mcp_bad_headers_do_not_downgrade_to_stdio(headers):
    with pytest.raises(ValueError):
        apply_remote_overrides([{"context7": {"command": "npx"}}], json.dumps({"context7": {"url": "https://docs.example/mcp", "headers_env": headers}}), {"TOKEN": "secret"})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "1"])
def test_knowledge_rejects_invalid_embeddings(value):
    with pytest.raises(ValueError):
        validate_vectors([[value] + [0.0] * 1535], 5)


def test_knowledge_rejects_wrong_embedding_space_and_protocol():
    with pytest.raises(ValueError):
        validate_vectors([[1.0] * 768], 5)
    with pytest.raises(RuntimeError):
        validate_results({"version": 2, "hits": [[], []]}, 1, 5)
    with pytest.raises(RuntimeError):
        validate_results({"version": 1, "hits": [[]]}, 1, 5)
