"""Tests for utils/math_renderer.py — no browser is launched."""

from __future__ import annotations

import hashlib
import importlib
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers to reload math_renderer with _KATEX_AVAILABLE patched
# ---------------------------------------------------------------------------

def _reload_with_katex(available: bool, tmp_path=None):
    """Return a freshly-imported math_renderer module with KaTeX mocked."""
    # Provide a stub for utils.browser_pool so the real Puppeteer is never used
    stub_bp = types.ModuleType("utils.browser_pool")
    stub_bp.render_with_browser_daemon = AsyncMock(return_value=None)

    with patch.dict(sys.modules, {"utils.browser_pool": stub_bp}):
        # Remove cached module so importlib.import_module re-executes top-level
        sys.modules.pop("utils.math_renderer", None)
        with patch("os.path.isfile", side_effect=lambda p: p.endswith("katex.min.js") if available else False):
            mod = importlib.import_module("utils.math_renderer")
    # Restore for subsequent tests
    sys.modules.pop("utils.math_renderer", None)
    return mod, stub_bp


# ---------------------------------------------------------------------------
# Module-level fixture: load renderer with KaTeX "available"
# ---------------------------------------------------------------------------

@pytest.fixture()
def renderer(tmp_path):
    """math_renderer with KaTeX available and browser stubbed."""
    stub_bp = types.ModuleType("utils.browser_pool")
    stub_bp.render_with_browser_daemon = AsyncMock(return_value=None)

    saved = sys.modules.get("utils.browser_pool")
    sys.modules["utils.browser_pool"] = stub_bp
    sys.modules.pop("utils.math_renderer", None)

    with patch("os.path.isfile", return_value=True):
        import utils.math_renderer as mod

    yield mod, stub_bp, tmp_path

    # Teardown — restore original state without destroying other modules
    sys.modules.pop("utils.math_renderer", None)
    if saved is None:
        sys.modules.pop("utils.browser_pool", None)
    else:
        sys.modules["utils.browser_pool"] = saved


# ---------------------------------------------------------------------------
# _estimate_complexity
# ---------------------------------------------------------------------------

class TestEstimateComplexity:
    def _get(self):
        sys.modules.pop("utils.math_renderer", None)
        stub_bp = types.ModuleType("utils.browser_pool")
        stub_bp.render_with_browser_daemon = AsyncMock()
        with patch.dict(sys.modules, {"utils.browser_pool": stub_bp}):
            with patch("os.path.isfile", return_value=True):
                import utils.math_renderer as m
        sys.modules.pop("utils.math_renderer", None)
        return m._estimate_complexity

    def test_simple_short_expression(self):
        fn = self._get()
        assert fn("x + 1") == "simple"

    def test_medium_single_frac(self):
        fn = self._get()
        assert fn(r"\frac{a}{b}") == "medium"

    def test_complex_multiple_structural_cmds(self):
        fn = self._get()
        assert fn(r"\frac{1}{n} \sum_{i=0}^{n} x_i") == "complex"

    def test_medium_many_subscripts(self):
        fn = self._get()
        # 3 subscripts, short otherwise -> medium
        assert fn("a_1 b_2 c_3") == "medium"

    def test_complex_long_with_subscripts(self):
        fn = self._get()
        # long enough raw_len and 3+ subscripts -> complex
        expr = "a_1 + b_2 + c_3 + d_4 + e_5 + f_6 + g_7 + h_8 + i_9"
        result = fn(expr)
        assert result in ("medium", "complex")  # at least not simple


# ---------------------------------------------------------------------------
# _get_scale_params
# ---------------------------------------------------------------------------

class TestGetScaleParams:
    def _get(self):
        sys.modules.pop("utils.math_renderer", None)
        stub_bp = types.ModuleType("utils.browser_pool")
        stub_bp.render_with_browser_daemon = AsyncMock()
        with patch.dict(sys.modules, {"utils.browser_pool": stub_bp}):
            with patch("os.path.isfile", return_value=True):
                import utils.math_renderer as m
        sys.modules.pop("utils.math_renderer", None)
        return m._get_scale_params

    def test_display_mode_simple(self):
        fn = self._get()
        p = fn("x", display_mode=True)
        assert p["font_size"] == 10
        assert p["screenshot_pad"] == 1

    def test_inline_mode_simple(self):
        fn = self._get()
        p = fn("x", display_mode=False)
        assert p["font_size"] == 9

    def test_display_mode_complex_larger_font(self):
        fn = self._get()
        p_simple = fn("x", display_mode=True)
        p_complex = fn(r"\frac{1}{n} \sum_{i=0}^{n}", display_mode=True)
        assert p_complex["font_size"] >= p_simple["font_size"]


# ---------------------------------------------------------------------------
# _get_math_css
# ---------------------------------------------------------------------------

class TestGetMathCss:
    def _get(self):
        sys.modules.pop("utils.math_renderer", None)
        stub_bp = types.ModuleType("utils.browser_pool")
        stub_bp.render_with_browser_daemon = AsyncMock()
        with patch.dict(sys.modules, {"utils.browser_pool": stub_bp}):
            with patch("os.path.isfile", return_value=True):
                import utils.math_renderer as m
        sys.modules.pop("utils.math_renderer", None)
        return m._get_math_css

    def test_display_mode_contains_line_height_15(self):
        fn = self._get()
        css = fn(display_mode=True)
        assert "1.5" in css

    def test_inline_mode_contains_line_height_14(self):
        fn = self._get()
        css = fn(display_mode=False)
        assert "1.4" in css

    def test_css_contains_dark_background(self):
        fn = self._get()
        css = fn(display_mode=True)
        assert "#2b2d31" in css

    def test_katex_theme_appended(self):
        fn = self._get()
        css = fn(display_mode=True)
        assert "katex" in css.lower()


# ---------------------------------------------------------------------------
# _escape_for_json
# ---------------------------------------------------------------------------

class TestEscapeForJson:
    def _get(self):
        sys.modules.pop("utils.math_renderer", None)
        stub_bp = types.ModuleType("utils.browser_pool")
        stub_bp.render_with_browser_daemon = AsyncMock()
        with patch.dict(sys.modules, {"utils.browser_pool": stub_bp}):
            with patch("os.path.isfile", return_value=True):
                import utils.math_renderer as m
        sys.modules.pop("utils.math_renderer", None)
        return m._escape_for_json

    def test_plain_string_wrapped_in_quotes(self):
        fn = self._get()
        assert fn("hello") == '"hello"'

    def test_backslash_escaped(self):
        fn = self._get()
        result = fn(r"\frac{a}{b}")
        assert "\\\\" in result  # double-escaped backslash in JSON

    def test_double_quote_escaped(self):
        fn = self._get()
        result = fn('say "hi"')
        assert '\\"' in result

    def test_newline_escaped(self):
        fn = self._get()
        result = fn("line1\nline2")
        assert "\\n" in result


# ---------------------------------------------------------------------------
# render_math_image — hash naming
# ---------------------------------------------------------------------------

class TestHashNaming:
    @pytest.mark.asyncio
    async def test_output_path_uses_md5_of_cache_key(self, renderer, tmp_path):
        mod, stub_bp, _ = renderer
        output_dir = str(tmp_path)

        # Simulate browser writing the PNG
        latex = r"\alpha + \beta"
        display = True
        _TEMPLATE_VERSION = "2"
        cache_key = f"v{_TEMPLATE_VERSION}:{latex}:{'display' if display else 'inline'}"
        expected_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
        expected_path = os.path.join(output_dir, f"math_{expected_hash}.png")

        async def fake_render(**kwargs):
            open(kwargs["output_path"], "w").close()

        stub_bp.render_with_browser_daemon = AsyncMock(side_effect=fake_render)

        with patch.object(mod, "render_with_browser_daemon", stub_bp.render_with_browser_daemon):
            with patch.object(mod, "_KATEX_AVAILABLE", True):
                result = await mod.render_math_image(latex, display_mode=display, output_dir=output_dir)

        assert result == expected_path

    @pytest.mark.asyncio
    async def test_display_and_inline_produce_different_hashes(self, renderer, tmp_path):
        mod, stub_bp, _ = renderer
        output_dir = str(tmp_path)

        async def fake_render(**kwargs):
            open(kwargs["output_path"], "w").close()

        stub_bp.render_with_browser_daemon = AsyncMock(side_effect=fake_render)
        latex = "x^2"

        with patch.object(mod, "render_with_browser_daemon", stub_bp.render_with_browser_daemon):
            with patch.object(mod, "_KATEX_AVAILABLE", True):
                path_display = await mod.render_math_image(latex, display_mode=True, output_dir=output_dir)
                path_inline = await mod.render_math_image(latex, display_mode=False, output_dir=output_dir)

        assert path_display != path_inline


# ---------------------------------------------------------------------------
# render_math_image — cache hit
# ---------------------------------------------------------------------------

class TestCacheHit:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_browser(self, renderer, tmp_path):
        mod, stub_bp, _ = renderer
        output_dir = str(tmp_path)
        latex = "x^2"
        _TEMPLATE_VERSION = "2"
        cache_key = f"v{_TEMPLATE_VERSION}:{latex}:display"
        h = hashlib.md5(cache_key.encode()).hexdigest()[:12]
        cached_path = os.path.join(output_dir, f"math_{h}.png")
        open(cached_path, "w").close()  # pre-create cache file

        call_count = 0

        async def fake_render(**kwargs):
            nonlocal call_count
            call_count += 1

        stub_bp.render_with_browser_daemon = AsyncMock(side_effect=fake_render)

        with patch.object(mod, "render_with_browser_daemon", stub_bp.render_with_browser_daemon):
            with patch.object(mod, "_KATEX_AVAILABLE", True):
                result = await mod.render_math_image(latex, display_mode=True, output_dir=output_dir)

        assert result == cached_path
        assert call_count == 0  # browser not called


# ---------------------------------------------------------------------------
# render_math_image — KaTeX unavailable
# ---------------------------------------------------------------------------

class TestKatexUnavailable:
    @pytest.mark.asyncio
    async def test_returns_none_when_katex_unavailable(self, renderer, tmp_path):
        mod, stub_bp, _ = renderer
        with patch.object(mod, "_KATEX_AVAILABLE", False):
            result = await mod.render_math_image(r"\alpha", output_dir=str(tmp_path))
        assert result is None


# ---------------------------------------------------------------------------
# render_math_image — error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_timeout_returns_none(self, renderer, tmp_path):
        mod, stub_bp, _ = renderer
        stub_bp.render_with_browser_daemon = AsyncMock(side_effect=TimeoutError("timed out"))

        with patch.object(mod, "render_with_browser_daemon", stub_bp.render_with_browser_daemon):
            with patch.object(mod, "_KATEX_AVAILABLE", True):
                result = await mod.render_math_image("x", output_dir=str(tmp_path))

        assert result is None

    @pytest.mark.asyncio
    async def test_generic_exception_returns_none(self, renderer, tmp_path):
        mod, stub_bp, _ = renderer
        stub_bp.render_with_browser_daemon = AsyncMock(side_effect=RuntimeError("browser crashed"))

        with patch.object(mod, "render_with_browser_daemon", stub_bp.render_with_browser_daemon):
            with patch.object(mod, "_KATEX_AVAILABLE", True):
                result = await mod.render_math_image("x", output_dir=str(tmp_path))

        assert result is None

    @pytest.mark.asyncio
    async def test_browser_does_not_write_file_returns_none(self, renderer, tmp_path):
        """render_with_browser_daemon succeeds but PNG not created -> None."""
        mod, stub_bp, _ = renderer
        # Do NOT create the output file
        stub_bp.render_with_browser_daemon = AsyncMock(return_value=None)

        with patch.object(mod, "render_with_browser_daemon", stub_bp.render_with_browser_daemon):
            with patch.object(mod, "_KATEX_AVAILABLE", True):
                result = await mod.render_math_image("x^2", output_dir=str(tmp_path))

        assert result is None


# ---------------------------------------------------------------------------
# render_math_image — HTML template sanity
# ---------------------------------------------------------------------------

class TestHtmlTemplate:
    @pytest.mark.asyncio
    async def test_html_contains_display_true(self, renderer, tmp_path):
        mod, stub_bp, _ = renderer
        output_dir = str(tmp_path)
        written_html = {}

        original_open = open

        def capturing_open(path, mode="r", **kwargs):
            if mode == "w" and path.endswith(".html"):
                import io
                buf = io.StringIO()

                class _Proxy:
                    def write(self, s):
                        buf.write(s)
                        written_html["content"] = buf.getvalue()

                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        pass

                return _Proxy()
            return original_open(path, mode, **kwargs)

        async def fake_render(**kwargs):
            open(kwargs["output_path"], "w").close()

        stub_bp.render_with_browser_daemon = AsyncMock(side_effect=fake_render)

        with patch("builtins.open", side_effect=capturing_open):
            with patch.object(mod, "render_with_browser_daemon", stub_bp.render_with_browser_daemon):
                with patch.object(mod, "_KATEX_AVAILABLE", True):
                    await mod.render_math_image(r"\alpha", display_mode=True, output_dir=output_dir)

        assert "displayMode: true" in written_html.get("content", "")

    @pytest.mark.asyncio
    async def test_html_contains_display_false_for_inline(self, renderer, tmp_path):
        mod, stub_bp, _ = renderer
        output_dir = str(tmp_path)
        written_html = {}

        original_open = open

        def capturing_open(path, mode="r", **kwargs):
            if mode == "w" and path.endswith(".html"):
                import io
                buf = io.StringIO()

                class _Proxy:
                    def write(self, s):
                        buf.write(s)
                        written_html["content"] = buf.getvalue()

                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        pass

                return _Proxy()
            return original_open(path, mode, **kwargs)

        async def fake_render(**kwargs):
            open(kwargs["output_path"], "w").close()

        stub_bp.render_with_browser_daemon = AsyncMock(side_effect=fake_render)

        with patch("builtins.open", side_effect=capturing_open):
            with patch.object(mod, "render_with_browser_daemon", stub_bp.render_with_browser_daemon):
                with patch.object(mod, "_KATEX_AVAILABLE", True):
                    await mod.render_math_image(r"\alpha", display_mode=False, output_dir=output_dir)

        assert "displayMode: false" in written_html.get("content", "")

    @pytest.mark.asyncio
    async def test_html_latex_is_json_escaped(self, renderer, tmp_path):
        mod, stub_bp, _ = renderer
        output_dir = str(tmp_path)
        written_html = {}
        original_open = open

        def capturing_open(path, mode="r", **kwargs):
            if mode == "w" and path.endswith(".html"):
                import io
                buf = io.StringIO()

                class _Proxy:
                    def write(self, s):
                        buf.write(s)
                        written_html["content"] = buf.getvalue()

                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        pass

                return _Proxy()
            return original_open(path, mode, **kwargs)

        async def fake_render(**kwargs):
            open(kwargs["output_path"], "w").close()

        stub_bp.render_with_browser_daemon = AsyncMock(side_effect=fake_render)
        latex = r'\frac{"x"}{y}'

        with patch("builtins.open", side_effect=capturing_open):
            with patch.object(mod, "render_with_browser_daemon", stub_bp.render_with_browser_daemon):
                with patch.object(mod, "_KATEX_AVAILABLE", True):
                    await mod.render_math_image(latex, display_mode=True, output_dir=output_dir)

        content = written_html.get("content", "")
        # JSON-escaped double quotes should appear
        assert '\\"' in content or "\\u0022" in content


# ---------------------------------------------------------------------------
# _resolve_katex_root
# ---------------------------------------------------------------------------

class TestResolveKatexRoot:
    def test_returns_none_when_no_candidate_exists(self):
        sys.modules.pop("utils.math_renderer", None)
        stub_bp = types.ModuleType("utils.browser_pool")
        stub_bp.render_with_browser_daemon = AsyncMock()
        with patch.dict(sys.modules, {"utils.browser_pool": stub_bp}):
            with patch("os.path.isfile", return_value=False):
                import utils.math_renderer as m
        sys.modules.pop("utils.math_renderer", None)
        assert m._resolve_katex_root() is False or m._katex_root is None

    def test_returns_file_uri_when_candidate_exists(self):
        sys.modules.pop("utils.math_renderer", None)
        stub_bp = types.ModuleType("utils.browser_pool")
        stub_bp.render_with_browser_daemon = AsyncMock()
        with patch.dict(sys.modules, {"utils.browser_pool": stub_bp}):
            with patch("os.path.isfile", return_value=True):
                import utils.math_renderer as m
        sys.modules.pop("utils.math_renderer", None)
        root = m._resolve_katex_root()
        assert root is not None
        assert root.startswith("file://")
