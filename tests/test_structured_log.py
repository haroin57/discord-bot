"""Tests for services.structured_log (SRE-H3)."""

import json
import logging
import sys
import unittest.mock as mock

import pytest


def _make_record(msg="hello", level=logging.INFO, extra=None):
    logger = logging.getLogger("test.structured")
    record = logger.makeRecord(
        name="test.structured",
        level=level,
        fn="test_file.py",
        lno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


class TestJsonFormatter:
    def setup_method(self):
        from services.structured_log import JsonFormatter

        self.fmt = JsonFormatter()

    def test_json_formatter_outputs_valid_json(self):
        record = _make_record("test message")
        output = self.fmt.format(record)
        data = json.loads(output)
        assert "ts" in data
        assert "level" in data
        assert "logger" in data
        assert "msg" in data
        assert data["msg"] == "test message"
        assert data["level"] == "INFO"

    def test_request_id_included(self):
        # Stub out services.correlation with a known request ID
        fake_rid = "req-abc-123"
        fake_correlation = mock.MagicMock()
        fake_correlation.get_request_id.return_value = fake_rid

        with mock.patch.dict(sys.modules, {"services.correlation": fake_correlation}):
            # Re-import to pick up the mock
            from services.structured_log import JsonFormatter

            fmt = JsonFormatter()
            record = _make_record("rid test")
            output = fmt.format(record)

        data = json.loads(output)
        assert "rid" in data
        assert data["rid"] == fake_rid

    def test_extra_fields_serialized(self):
        record = _make_record("extra test", extra={"user_id": "X", "channel_id": 42})
        output = self.fmt.format(record)
        data = json.loads(output)
        assert data.get("user_id") == "X"
        assert data.get("channel_id") == 42

    def test_extra_cannot_override_fixed_payload_fields(self):
        fake_correlation = mock.MagicMock()
        fake_correlation.get_request_id.return_value = "actual-rid"
        record = _make_record(
            "fixed fields",
            level=logging.WARNING,
            extra={
                "rid": "spoofed-rid",
                "ts": "spoofed-ts",
                "level": "DEBUG",
                "logger": "spoofed.logger",
                "user_id": "X",
            },
        )

        with mock.patch.dict(sys.modules, {"services.correlation": fake_correlation}):
            data = json.loads(self.fmt.format(record))

        assert data["rid"] == "actual-rid"
        assert data["ts"] != "spoofed-ts"
        assert data["level"] == "WARNING"
        assert data["logger"] == "test.structured"
        assert data["user_id"] == "X"

    def test_exc_is_generated_only_from_exc_info(self):
        record = _make_record("no exception", extra={"exc": "spoofed", "user_id": "X"})
        data = json.loads(self.fmt.format(record))

        assert "exc" not in data
        assert data["user_id"] == "X"

        try:
            raise ValueError("actual failure")
        except ValueError:
            record = _make_record(
                "real exception",
                extra={"exc": "spoofed", "user_id": "X"},
            )
            record.exc_info = sys.exc_info()

        data = json.loads(self.fmt.format(record))
        assert "ValueError: actual failure" in data["exc"]
        assert data["exc"] != "spoofed"
        assert data["user_id"] == "X"

    def test_unserializable_extras_repr_fallback(self):
        obj = object()
        record = _make_record("repr test", extra={"obj": obj})
        # Must not raise
        output = self.fmt.format(record)
        data = json.loads(output)
        assert "obj" in data
        assert isinstance(data["obj"], str)

    def test_recursively_redacts_sensitive_keys_and_auth_headers(self):
        record = _make_record(
            "Authorization: Bearer message-secret",
            extra={
                "discord_token": "token-secret",
                "nested": {
                    "password": "password-secret",
                    "headers": {"Cookie": "session=cookie-secret"},
                    "note": "api_key=embedded-secret",
                },
            },
        )

        output = self.fmt.format(record)
        data = json.loads(output)

        assert "message-secret" not in output
        assert "token-secret" not in output
        assert "password-secret" not in output
        assert "cookie-secret" not in output
        assert "embedded-secret" not in output
        assert data["discord_token"] == "[REDACTED]"

    def test_redacts_quoted_json_and_whitespace_secret_values(self):
        secrets = (
            "json secret with spaces",
            "bearer secret with spaces",
            "quoted api secret",
            "plain password with spaces",
        )
        message = (
            '{"token": "json secret with spaces", '
            '"Authorization": "Bearer bearer secret with spaces"}; '
            "api_key='quoted api secret'; "
            "password=plain password with spaces"
        )

        output = self.fmt.format(_make_record(message))

        for secret in secrets:
            assert secret not in output
        assert output.count("[REDACTED]") >= 4

    def test_runtime_settings_repr_omits_discord_token(self, tmp_path):
        from services.runtime_bootstrap import build_runtime_settings

        environ = {
            "DISCORD_TOKEN": "never-in-repr",
            "BOT_STATE_DIR": str(tmp_path / "state"),
            "BOT_ATTACHMENT_DIR": str(tmp_path / "attachments"),
            "MERMAID_OUTPUT_DIR": str(tmp_path / "mermaid"),
            "TABLE_RENDERS_DIR": str(tmp_path / "table"),
            "MATH_RENDERS_DIR": str(tmp_path / "math"),
        }
        settings = build_runtime_settings(environ, bot_dir=str(tmp_path))

        assert "never-in-repr" not in repr(settings)
        assert "discord_token" not in repr(settings)

    def test_formatter_failure_points_use_fixed_placeholders(self):
        class BadMessage:
            def __str__(self):
                raise RuntimeError("message conversion failed")

        class BadRepr:
            def __repr__(self):
                raise RuntimeError("repr failed")

        record = _make_record(BadMessage(), extra={"bad": BadRepr()})
        fake_correlation = mock.MagicMock()
        fake_correlation.get_request_id.side_effect = RuntimeError("rid failed")
        with mock.patch.dict(sys.modules, {"services.correlation": fake_correlation}):
            data = json.loads(self.fmt.format(record))

        assert data["msg"] == "[message unavailable]"
        assert data["rid"] == "[unavailable]"
        assert data["bad"] == "[unavailable]"

    def test_baseexception_from_user_conversions_is_contained(self):
        class FatalMessage:
            def __str__(self):
                raise KeyboardInterrupt("hostile str")

        class FatalRepr:
            def __repr__(self):
                raise SystemExit("hostile repr")

        record = _make_record(FatalMessage(), extra={"bad": FatalRepr()})

        data = json.loads(self.fmt.format(record))

        assert data["msg"] == "[message unavailable]"
        assert data["bad"] == "[unavailable]"

    def test_format_exception_failure_uses_placeholder(self):
        record = _make_record("exception")
        record.exc_info = (ValueError, ValueError("Bearer exception-secret"), None)
        with mock.patch.object(
            self.fmt,
            "formatException",
            side_effect=RuntimeError("formatter failed"),
        ):
            data = json.loads(self.fmt.format(record))
        assert data["exc"] == "[exception unavailable]"

    def test_nested_values_have_depth_and_length_limits(self):
        nested = {"value": "x" * 5000}
        for _ in range(8):
            nested = {"next": nested}
        output = self.fmt.format(_make_record("bounded", extra={"nested": nested}))
        assert len(output) < 6000
        assert "[max-depth]" in output


class TestInstallStructuredLogging:
    def teardown_method(self):
        # Remove only the handler owned by this module. Pytest and application
        # handlers are external ownership domains.
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, "_discord_bot_logging_owner", None) == (
                "discord-bot-structured-json-v1"
            ):
                root.removeHandler(h)
                h.close()

    def test_install_reuses_owned_handler_and_preserves_external_state(self):
        from services.structured_log import install_structured_logging, JsonFormatter
        from services.runtime_bootstrap import RootLifecycle

        root = logging.getLogger()
        original_level = root.level

        class ExternalHandler(logging.StreamHandler):
            def __init__(self):
                super().__init__(sys.stdout)
                self.was_closed = False

            def close(self):
                self.was_closed = True
                super().close()

        external = ExternalHandler()
        root.addHandler(external)
        root.setLevel(logging.WARNING)
        lifecycle = RootLifecycle()
        try:
            handler = install_structured_logging(level=logging.DEBUG)
            lifecycle.own_logging(logger=root, handler=handler)
            same_handler = install_structured_logging(level=logging.INFO)
            lifecycle.own_logging(logger=root, handler=same_handler)

            owned = [
                candidate
                for candidate in root.handlers
                if getattr(candidate, "_discord_bot_logging_owner", None)
                == "discord-bot-structured-json-v1"
            ]
            assert owned == [handler]
            assert same_handler is handler
            assert isinstance(handler.formatter, JsonFormatter)
            assert external in root.handlers
            assert external.was_closed is False
            assert root.level == logging.INFO

            lifecycle.close()
            lifecycle.close()
            assert handler not in root.handlers
            assert external in root.handlers
            assert external.was_closed is False
            assert root.level == logging.WARNING
        finally:
            if not lifecycle.closed:
                lifecycle.close()
            root.removeHandler(external)
            external.close()
            root.setLevel(original_level)

    def test_close_preserves_external_level_change_after_install(self):
        from services.runtime_bootstrap import RootLifecycle
        from services.structured_log import install_structured_logging

        root = logging.getLogger()
        original_level = root.level
        root.setLevel(logging.WARNING)
        lifecycle = RootLifecycle()
        try:
            handler = install_structured_logging(level=logging.INFO)
            lifecycle.own_logging(logger=root, handler=handler)
            root.setLevel(logging.ERROR)

            lifecycle.close()

            assert handler not in root.handlers
            assert root.level == logging.ERROR
        finally:
            if not lifecycle.closed:
                lifecycle.close()
            root.setLevel(original_level)

    def test_nested_lifecycles_restore_level_once_and_preserve_external_handler(self):
        from services.runtime_bootstrap import RootLifecycle
        from services.structured_log import install_structured_logging

        root = logging.getLogger()
        original_level = root.level
        external = logging.NullHandler()
        root.addHandler(external)
        root.setLevel(logging.WARNING)
        outer = RootLifecycle()
        inner = RootLifecycle()
        try:
            handler = install_structured_logging(level=logging.DEBUG)
            outer.own_logging(logger=root, handler=handler)
            same_handler = install_structured_logging(level=logging.INFO)
            inner.own_logging(logger=root, handler=same_handler)

            inner.close()
            assert handler in root.handlers
            assert getattr(handler, "_closed", False) is False
            assert root.level == logging.DEBUG

            outer.close()

            assert handler is same_handler
            assert handler not in root.handlers
            assert external in root.handlers
            assert root.level == logging.WARNING
        finally:
            if not inner.closed:
                inner.close()
            if not outer.closed:
                outer.close()
            root.removeHandler(external)
            external.close()
            root.setLevel(original_level)

    def test_nested_lifecycles_support_out_of_order_close(self):
        from services.runtime_bootstrap import RootLifecycle
        from services.structured_log import install_structured_logging

        root = logging.getLogger()
        original_level = root.level
        root.setLevel(logging.WARNING)
        outer = RootLifecycle()
        inner = RootLifecycle()
        try:
            handler = install_structured_logging(level=logging.DEBUG)
            outer.own_logging(logger=root, handler=handler)
            install_structured_logging(level=logging.INFO)
            inner.own_logging(logger=root, handler=handler)

            outer.close()
            assert handler in root.handlers
            assert getattr(handler, "_closed", False) is False
            assert root.level == logging.INFO

            inner.close()
            assert handler not in root.handlers
            assert root.level == logging.WARNING
        finally:
            if not inner.closed:
                inner.close()
            if not outer.closed:
                outer.close()
            root.setLevel(original_level)

    def test_nested_lifecycles_preserve_external_level_change(self):
        from services.runtime_bootstrap import RootLifecycle
        from services.structured_log import install_structured_logging

        root = logging.getLogger()
        original_level = root.level
        root.setLevel(logging.WARNING)
        outer = RootLifecycle()
        inner = RootLifecycle()
        try:
            handler = install_structured_logging(level=logging.DEBUG)
            outer.own_logging(logger=root, handler=handler)
            install_structured_logging(level=logging.INFO)
            inner.own_logging(logger=root, handler=handler)
            root.setLevel(logging.ERROR)

            inner.close()
            assert handler in root.handlers
            assert root.level == logging.ERROR

            outer.close()
            assert handler not in root.handlers
            assert root.level == logging.ERROR
        finally:
            if not inner.closed:
                inner.close()
            if not outer.closed:
                outer.close()
            root.setLevel(original_level)

    def test_nested_lifecycles_preserve_external_level_matching_remaining_owner(self):
        from services.runtime_bootstrap import RootLifecycle
        from services.structured_log import install_structured_logging

        root = logging.getLogger()
        original_level = root.level
        root.setLevel(logging.WARNING)
        outer = RootLifecycle()
        inner = RootLifecycle()
        try:
            handler = install_structured_logging(level=logging.DEBUG)
            outer.own_logging(logger=root, handler=handler)
            install_structured_logging(level=logging.INFO)
            inner.own_logging(logger=root, handler=handler)

            # This external value happens to equal the remaining owner's level.
            root.setLevel(logging.DEBUG)
            inner.close()
            assert handler in root.handlers
            assert root.level == logging.DEBUG

            outer.close()
            assert handler not in root.handlers
            assert root.level == logging.DEBUG
        finally:
            if not inner.closed:
                inner.close()
            if not outer.closed:
                outer.close()
            root.setLevel(original_level)

    def test_listener_and_downstream_close_only_with_last_owner(self):
        from services.runtime_bootstrap import RootLifecycle

        events = []

        class CountingHandler(logging.Handler):
            def __init__(self, name):
                super().__init__()
                self.name = name
                self.close_count = 0

            def close(self):
                self.close_count += 1
                events.append(self.name)
                super().close()

        class Listener:
            def __init__(self):
                self._thread = object()
                self.stop_count = 0

            def stop(self):
                self.stop_count += 1
                events.append("listener")
                self._thread = None

        logger = logging.Logger("leased-logging")
        logger.setLevel(logging.WARNING)
        external = logging.NullHandler()
        handler = CountingHandler("handler")
        downstream = CountingHandler("downstream")
        listener = Listener()
        logger.addHandler(external)
        logger.addHandler(handler)
        setattr(handler, "_discord_bot_previous_level", logging.WARNING)
        outer = RootLifecycle()
        inner = RootLifecycle()
        try:
            logger.setLevel(logging.DEBUG)
            outer.own_logging(
                logger=logger,
                handler=handler,
                listener=listener,
                downstream=(downstream,),
            )
            logger.setLevel(logging.INFO)
            inner.own_logging(
                logger=logger,
                handler=handler,
                listener=listener,
                downstream=(downstream,),
            )

            outer.close()
            assert handler in logger.handlers
            assert listener.stop_count == 0
            assert handler.close_count == 0
            assert downstream.close_count == 0

            inner.close()
            assert listener.stop_count == 1
            assert handler.close_count == 1
            assert downstream.close_count == 1
            assert events == ["listener", "handler", "downstream"]
            assert handler not in logger.handlers
            assert external in logger.handlers
            assert logger.level == logging.WARNING
        finally:
            if not inner.closed:
                inner.close()
            if not outer.closed:
                outer.close()
            logger.removeHandler(external)
            external.close()


class TestConfigureNoisyLoggers:
    """OPT-B: third-party log-volume hygiene (httpx/aiohttp.access/httpcore)."""

    def _snapshot(self):
        names = ("httpx", "httpcore", "aiohttp.access")
        state = {}
        for name in names:
            lg = logging.getLogger(name)
            state[name] = (lg.level, list(lg.filters))
        return state

    def _restore(self, state):
        import services.structured_log as sl

        for name, (level, filters) in state.items():
            lg = logging.getLogger(name)
            lg.setLevel(level)
            for extra in list(lg.filters):
                if extra not in filters:
                    lg.removeFilter(extra)
        sl.flush_http_counters()

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        state = self._snapshot()
        for key in ("BOT_HTTP_COUNTER", "BOT_LOG_LEVEL_HTTPX",
                    "BOT_LOG_LEVEL_AIOHTTP_ACCESS", "BOT_LOG_LEVEL_HTTPCORE"):
            monkeypatch.delenv(key, raising=False)
        yield
        self._restore(state)

    def test_aiohttp_and_httpcore_raised_to_warning(self):
        from services.structured_log import configure_noisy_loggers

        configure_noisy_loggers()
        assert logging.getLogger("aiohttp.access").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING

    def test_counter_disabled_sets_httpx_warning(self, monkeypatch):
        from services.structured_log import configure_noisy_loggers

        monkeypatch.setenv("BOT_HTTP_COUNTER", "0")
        result = configure_noisy_loggers()
        assert result is None
        httpx_logger = logging.getLogger("httpx")
        assert httpx_logger.level == logging.WARNING
        assert not any(
            getattr(f, "_discord_bot_http_counter_filter", False)
            for f in httpx_logger.filters
        )

    def test_env_override_level(self, monkeypatch):
        from services.structured_log import configure_noisy_loggers

        monkeypatch.setenv("BOT_LOG_LEVEL_AIOHTTP_ACCESS", "ERROR")
        configure_noisy_loggers()
        assert logging.getLogger("aiohttp.access").level == logging.ERROR

    def test_counter_filter_suppresses_request_line(self):
        from services.structured_log import configure_noisy_loggers

        counter = configure_noisy_loggers()
        assert counter is not None
        # A request line is dropped (filter returns False) but counted...
        record = _make_record(
            msg='HTTP Request: POST http://localhost:8000/collections/x/get '
            '"HTTP/1.1 200 OK"'
        )
        assert counter.filter(record) is False
        # ...while a WARNING is never swallowed.
        warn = _make_record(msg="connection reset", level=logging.WARNING)
        assert counter.filter(warn) is True

    def test_counter_is_idempotent(self):
        from services.structured_log import configure_noisy_loggers

        first = configure_noisy_loggers()
        second = configure_noisy_loggers()
        assert first is second
        installed = [
            f
            for f in logging.getLogger("httpx").filters
            if getattr(f, "_discord_bot_http_counter_filter", False)
        ]
        assert len(installed) == 1

    def test_counts_emitted_on_rid_change_and_flush(self):
        import services.structured_log as sl
        from services.structured_log import configure_noisy_loggers, flush_http_counters

        counter = configure_noisy_loggers()
        assert counter is not None
        emitted: list[tuple[str, dict[str, int]]] = []
        orig = sl._emit_http_counter
        sl._emit_http_counter = lambda rid, counts: emitted.append((rid, dict(counts)))
        try:
            with mock.patch("services.correlation.get_request_id", return_value="aaaa1111"):
                counter.filter(
                    _make_record(msg='HTTP Request: POST http://h/collections/x/get "HTTP/1.1 200 OK"')
                )
                counter.filter(
                    _make_record(msg='HTTP Request: POST http://h/collections/x/query "HTTP/1.1 200 OK"')
                )
                counter.filter(
                    _make_record(msg='HTTP Request: POST http://api/v1/embeddings "HTTP/1.1 200 OK"')
                )
                counter.filter(
                    _make_record(msg='HTTP Request: PUT http://r2/objects "HTTP/1.1 200 OK"')
                )
            # rid change flushes the first request's aggregate.
            with mock.patch("services.correlation.get_request_id", return_value="bbbb2222"):
                counter.filter(
                    _make_record(msg='HTTP Request: POST http://h/collections/x/get "HTTP/1.1 200 OK"')
                )
            assert emitted, "rid change should flush previous counts"
            rid, counts = emitted[0]
            assert rid == "aaaa1111"
            assert counts == {"get": 1, "query": 1, "embeddings": 1, "put": 1, "other": 0}
            emitted.clear()
            flush_http_counters()
            rid2, counts2 = emitted[0]
            assert rid2 == "bbbb2222"
            assert counts2["get"] == 1
        finally:
            sl._emit_http_counter = orig
