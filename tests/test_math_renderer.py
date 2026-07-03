"""Tests for utils/math_renderer.py (Cloudflare Browser Rendering backend).

No browser is launched: the Browser Rendering client (render_screenshot) is
mocked to return fake PNG bytes, and KaTeX availability is driven by
_KATEX_AVAILABLE (True when the CF client is configured).
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sys

import pytest


# ---------------------------------------------------------------------------
# Helper to (re)import a fresh math_renderer module
# ---------------------------------------------------------------------------

def _import_renderer():
    sys.modules.pop("utils.math_renderer", None)
    mod = importlib.import_module("utils.math_renderer")
    sys.modules.pop("utils.math_renderer", None)
    return mod


@pytest.fixture()
def renderer(tmp_path):
    """math_renderer with the CF client mocked to succeed (returns PNG bytes).

    The fixture yields (module, render_mock, tmp_path). render_mock is an async
    mock installed as ``mod.render_screenshot``; by default it returns b"PNG".
    _KATEX_AVAILABLE is forced True regardless of environment config.
    """
    from unittest.mock import AsyncMock, patch

    saved = sys.modules.get("utils.math_renderer")
    sys.modules.pop("utils.math_renderer", None)
    import utils.math_renderer as mod

    render_mock = AsyncMock(return_value=b"\x89PNG\r\n")
    with patch.object(mod, "render_screenshot", render_mock), \
         patch.object(mod, "_KATEX_AVAILABLE", True):
        yield mod, render_mock, tmp_path

    sys.modules.pop("utils.math_renderer", None)
    if saved is not None:
        sys.modules["utils.math_renderer"] = saved


# ---------------------------------------------------------------------------
# _estimate_complexity
# ---------------------------------------------------------------------------

class TestEstimateComplexity:
    def _get(self):
        return _import_renderer()._estimate_complexity

    def test_simple_short_expression(self):
        assert self._get()("x + 1") == "simple"

    def test_medium_single_frac(self):
        assert self._get()(r"\frac{a}{b}") == "medium"

    def test_complex_multiple_structural_cmds(self):
        assert self._get()(r"\frac{1}{n} \sum_{i=0}^{n} x_i") == "complex"

    def test_medium_many_subscripts(self):
        # 3 subscripts, short otherwise -> medium
        assert self._get()("a_1 b_2 c_3") == "medium"

    def test_complex_long_with_subscripts(self):
        expr = "a_1 + b_2 + c_3 + d_4 + e_5 + f_6 + g_7 + h_8 + i_9"
        assert self._get()(expr) in ("medium", "complex")


# ---------------------------------------------------------------------------
# _get_scale_params
# ---------------------------------------------------------------------------

class TestGetScaleParams:
    def _get(self):
        return _import_renderer()._get_scale_params

    def test_display_mode_simple(self):
        p = self._get()("x", display_mode=True)
        assert p["font_size"] == 10
        assert p["screenshot_pad"] == 1

    def test_inline_mode_simple(self):
        p = self._get()("x", display_mode=False)
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
        return _import_renderer()._get_math_css

    def test_display_mode_contains_line_height_15(self):
        assert "1.5" in self._get()(display_mode=True)

    def test_inline_mode_contains_line_height_14(self):
        assert "1.4" in self._get()(display_mode=False)

    def test_css_contains_dark_background(self):
        assert "#2b2d31" in self._get()(display_mode=True)

    def test_katex_theme_appended(self):
        assert "katex" in self._get()(display_mode=True).lower()


# ---------------------------------------------------------------------------
# _escape_for_json
# ---------------------------------------------------------------------------

class TestEscapeForJson:
    def _get(self):
        return _import_renderer()._escape_for_json

    def test_plain_string_wrapped_in_quotes(self):
        assert self._get()("hello") == '"hello"'

    def test_backslash_escaped(self):
        result = self._get()(r"\frac{a}{b}")
        assert "\\\\" in result  # double-escaped backslash in JSON

    def test_double_quote_escaped(self):
        assert '\\"' in self._get()('say "hi"')

    def test_newline_escaped(self):
        assert "\\n" in self._get()("line1\nline2")


# ---------------------------------------------------------------------------
# render_math_image — hash naming
# ---------------------------------------------------------------------------

class TestHashNaming:
    @pytest.mark.asyncio
    async def test_output_path_uses_md5_of_cache_key(self, renderer):
        mod, render_mock, tmp_path = renderer
        output_dir = str(tmp_path)

        latex = r"\alpha + \beta"
        display = True
        _TEMPLATE_VERSION = "3"
        cache_key = f"v{_TEMPLATE_VERSION}:{latex}:{'display' if display else 'inline'}"
        expected_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
        expected_path = os.path.join(output_dir, f"math_{expected_hash}.png")

        result = await mod.render_math_image(latex, display_mode=display, output_dir=output_dir)

        assert result == expected_path
        # The remote browser is told to clip to the math element, HiDPI, and
        # wait for the CDN KaTeX bundle.
        _, kwargs = render_mock.call_args
        assert kwargs["selector"] == ".math-container"
        assert kwargs["device_scale_factor"] == 2
        assert kwargs["wait_for_network_idle"] is True

    @pytest.mark.asyncio
    async def test_display_and_inline_produce_different_hashes(self, renderer):
        mod, _, tmp_path = renderer
        output_dir = str(tmp_path)
        latex = "x^2"

        path_display = await mod.render_math_image(latex, display_mode=True, output_dir=output_dir)
        path_inline = await mod.render_math_image(latex, display_mode=False, output_dir=output_dir)

        assert path_display != path_inline


# ---------------------------------------------------------------------------
# render_math_image — cache hit
# ---------------------------------------------------------------------------

class TestCacheHit:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_render(self, renderer):
        mod, render_mock, tmp_path = renderer
        output_dir = str(tmp_path)
        latex = "x^2"
        _TEMPLATE_VERSION = "3"
        cache_key = f"v{_TEMPLATE_VERSION}:{latex}:display"
        h = hashlib.md5(cache_key.encode()).hexdigest()[:12]
        cached_path = os.path.join(output_dir, f"math_{h}.png")
        open(cached_path, "w").close()  # pre-create cache file

        result = await mod.render_math_image(latex, display_mode=True, output_dir=output_dir)

        assert result == cached_path
        render_mock.assert_not_called()  # render client not invoked


# ---------------------------------------------------------------------------
# render_math_image — KaTeX / Browser Rendering unavailable
# ---------------------------------------------------------------------------

class TestKatexUnavailable:
    @pytest.mark.asyncio
    async def test_returns_none_when_unavailable(self, renderer):
        from unittest.mock import patch
        mod, _, tmp_path = renderer
        with patch.object(mod, "_KATEX_AVAILABLE", False):
            result = await mod.render_math_image(r"\alpha", output_dir=str(tmp_path))
        assert result is None


# ---------------------------------------------------------------------------
# render_math_image — error handling (client returns None → None result)
# ---------------------------------------------------------------------------

class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_render_failure_returns_none(self, renderer):
        mod, render_mock, tmp_path = renderer
        render_mock.return_value = None  # client failed (timeout/5xx/etc.)

        result = await mod.render_math_image("x", output_dir=str(tmp_path))
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_bytes_returns_none(self, renderer):
        mod, render_mock, tmp_path = renderer
        render_mock.return_value = b""  # no image content

        result = await mod.render_math_image("x^2", output_dir=str(tmp_path))
        assert result is None


# ---------------------------------------------------------------------------
# render_math_image — HTML template sanity (HTML passed to render_screenshot)
# ---------------------------------------------------------------------------

class TestHtmlTemplate:
    @pytest.mark.asyncio
    async def test_html_contains_display_true(self, renderer):
        mod, render_mock, tmp_path = renderer
        await mod.render_math_image(r"\alpha", display_mode=True, output_dir=str(tmp_path))
        html = render_mock.call_args.args[0]
        assert "displayMode: true" in html

    @pytest.mark.asyncio
    async def test_html_contains_display_false_for_inline(self, renderer):
        mod, render_mock, tmp_path = renderer
        await mod.render_math_image(r"\alpha", display_mode=False, output_dir=str(tmp_path))
        html = render_mock.call_args.args[0]
        assert "displayMode: false" in html

    @pytest.mark.asyncio
    async def test_html_latex_is_json_escaped(self, renderer):
        mod, render_mock, tmp_path = renderer
        await mod.render_math_image(r'\frac{"x"}{y}', display_mode=True, output_dir=str(tmp_path))
        html = render_mock.call_args.args[0]
        assert '\\"' in html or "\\u0022" in html

    @pytest.mark.asyncio
    async def test_html_references_katex_cdn(self, renderer):
        mod, render_mock, tmp_path = renderer
        await mod.render_math_image(r"\alpha", display_mode=True, output_dir=str(tmp_path))
        html = render_mock.call_args.args[0]
        # KaTeX is loaded from the pinned CDN base (not a local file:// path).
        assert mod.KATEX_CDN in html
        assert "katex.min.css" in html
        assert "file://" not in html


# ---------------------------------------------------------------------------
# KaTeX availability tracks Browser Rendering config
# ---------------------------------------------------------------------------

class TestKatexAvailability:
    def test_available_mirrors_is_configured(self):
        from unittest.mock import patch
        # Configured → available
        with patch("utils.cf_browser.is_configured", return_value=True):
            mod = _import_renderer()
            assert mod._KATEX_AVAILABLE is True
        # Not configured → unavailable
        with patch("utils.cf_browser.is_configured", return_value=False):
            mod = _import_renderer()
            assert mod._KATEX_AVAILABLE is False
