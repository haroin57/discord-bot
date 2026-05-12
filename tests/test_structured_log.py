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

    def test_unserializable_extras_repr_fallback(self):
        obj = object()
        record = _make_record("repr test", extra={"obj": obj})
        # Must not raise
        output = self.fmt.format(record)
        data = json.loads(output)
        assert "obj" in data
        assert isinstance(data["obj"], str)


    def test_correlation_import_error_fallback(self):
        """Line 50-51: ImportError -> rid defaults to '-'."""
        with mock.patch.dict(sys.modules, {"services.correlation": None}):
            from services.structured_log import JsonFormatter
            fmt = JsonFormatter()
            record = _make_record("no correlation")
            output = fmt.format(record)
        data = json.loads(output)
        assert data.get("rid") == "-"

    def test_exc_info_included(self):
        """Line 62: exc_info -> 'exc' key in output."""
        try:
            raise ValueError("boom")
        except ValueError:
            import sys as _sys
            exc_info = _sys.exc_info()

        logger = logging.getLogger("test.structured")
        record = logger.makeRecord(
            name="test.structured",
            level=logging.ERROR,
            fn="test_file.py",
            lno=1,
            msg="error occurred",
            args=(),
            exc_info=exc_info,
        )
        output = self.fmt.format(record)
        data = json.loads(output)
        assert "exc" in data
        assert "ValueError" in data["exc"]


class TestInstallStructuredLogging:
    def teardown_method(self):
        # Restore root logger to a clean state after each test
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)

    def test_install_replaces_handlers(self):
        from services.structured_log import install_structured_logging, JsonFormatter

        # Pre-install a dummy handler
        dummy = logging.StreamHandler(sys.stdout)
        logging.getLogger().addHandler(dummy)

        install_structured_logging(level=logging.DEBUG)

        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.DEBUG
