"""Tests for the local KaTeX/Puppeteer math renderer."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import sys

import pytest


_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc````"
    b"\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB\x60\x82"
)


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
    """math_renderer with the local browser daemon mocked to write a PNG."""
    from unittest.mock import AsyncMock, patch

    saved = sys.modules.get("utils.math_renderer")
    sys.modules.pop("utils.math_renderer", None)
    import utils.math_renderer as mod

    async def _write_png(**kwargs):
        with open(kwargs["output_path"], "wb") as file:
            file.write(_PNG)

    render_mock = AsyncMock(side_effect=_write_png)
    with (
        patch.object(mod, "render_with_browser_daemon", render_mock),
        patch.object(mod, "_KATEX_AVAILABLE", True),
        patch.object(mod, "KATEX_CDN", "file:///local/katex"),
    ):
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

    def test_script_end_tag_cannot_escape_inline_script(self):
        import json

        mod = _import_renderer()
        latex = "</script><script>globalThis.compromised = true</script>"
        escaped = mod._escape_for_json(latex)
        html = mod.HTML_TEMPLATE.format(
            katex_cdn="file:///katex",
            css="",
            latex_json=escaped,
            display_mode="true",
        )

        assert "<" not in escaped
        assert "\\u003c/script>\\u003cscript>" in escaped
        assert json.loads(escaped) == latex
        assert html.count("<script>") == 1
        assert latex not in html

    def test_javascript_line_separators_use_unicode_escapes(self):
        import json

        latex = "left\u2028middle\u2029right"
        escaped = self._get()(latex)

        assert "\u2028" not in escaped
        assert "\u2029" not in escaped
        assert "\\u2028" in escaped
        assert "\\u2029" in escaped
        assert json.loads(escaped) == latex


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
        _TEMPLATE_VERSION = "4"
        cache_key = f"v{_TEMPLATE_VERSION}:{latex}:{'display' if display else 'inline'}"
        expected_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
        expected_path = os.path.join(output_dir, f"math_{expected_hash}.png")

        result = await mod.render_math_image(
            latex, display_mode=display, output_dir=output_dir
        )

        assert result == expected_path
        kwargs = render_mock.call_args.kwargs
        assert kwargs["selector"] == ".math-container"
        assert kwargs["viewport"]["deviceScaleFactor"] == 2
        assert kwargs["html_path"].endswith(".html")

    @pytest.mark.asyncio
    async def test_display_and_inline_produce_different_hashes(self, renderer):
        mod, _, tmp_path = renderer
        output_dir = str(tmp_path)
        latex = "x^2"

        path_display = await mod.render_math_image(
            latex, display_mode=True, output_dir=output_dir
        )
        path_inline = await mod.render_math_image(
            latex, display_mode=False, output_dir=output_dir
        )

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
        _TEMPLATE_VERSION = "4"
        cache_key = f"v{_TEMPLATE_VERSION}:{latex}:display"
        h = hashlib.md5(cache_key.encode()).hexdigest()[:12]
        cached_path = os.path.join(output_dir, f"math_{h}.png")
        with open(cached_path, "wb") as file:
            file.write(_PNG)

        result = await mod.render_math_image(
            latex, display_mode=True, output_dir=output_dir
        )

        assert result == cached_path
        render_mock.assert_not_called()  # render client not invoked

    @pytest.mark.asyncio
    async def test_partial_png_is_not_reused(self, renderer):
        mod, render_mock, tmp_path = renderer
        latex = "partial"
        cache_key = f"v4:{latex}:display"
        h = hashlib.md5(cache_key.encode()).hexdigest()[:12]
        cached_path = os.path.join(str(tmp_path), f"math_{h}.png")
        with open(cached_path, "wb") as file:
            file.write(b"\x89PNG\r\n\x1a\npartial")

        result = await mod.render_math_image(latex, output_dir=str(tmp_path))

        assert result == cached_path
        render_mock.assert_awaited_once()
        assert open(cached_path, "rb").read() == _PNG


# ---------------------------------------------------------------------------
# render_math_image — KaTeX unavailable
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
# render_math_image — error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_render_failure_returns_none(self, renderer):
        mod, render_mock, tmp_path = renderer
        render_mock.side_effect = RuntimeError("browser failed")

        result = await mod.render_math_image("x", output_dir=str(tmp_path))
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_output_returns_none(self, renderer):
        mod, render_mock, tmp_path = renderer
        render_mock.side_effect = None
        render_mock.return_value = None

        result = await mod.render_math_image("x^2", output_dir=str(tmp_path))
        assert result is None

    @pytest.mark.asyncio
    async def test_cancel_removes_partial_png(self, renderer):
        mod, render_mock, tmp_path = renderer

        async def partial_then_cancel(**kwargs):
            with open(kwargs["output_path"], "wb") as file:
                file.write(b"\x89PNG\r\n\x1a\npartial")
            raise asyncio.CancelledError

        render_mock.side_effect = partial_then_cancel
        with pytest.raises(asyncio.CancelledError):
            await mod.render_math_image("cancel", output_dir=str(tmp_path))

        assert list(tmp_path.glob("*.part")) == []
        assert list(tmp_path.glob("*.png")) == []


# ---------------------------------------------------------------------------
# render_math_image — HTML template sanity
# ---------------------------------------------------------------------------


class TestHtmlTemplate:
    @pytest.mark.asyncio
    async def test_html_contains_display_true(self, renderer):
        mod, render_mock, tmp_path = renderer
        await mod.render_math_image(
            r"\alpha", display_mode=True, output_dir=str(tmp_path)
        )
        html = open(render_mock.call_args.kwargs["html_path"]).read()
        assert "displayMode: true" in html

    @pytest.mark.asyncio
    async def test_html_contains_display_false_for_inline(self, renderer):
        mod, render_mock, tmp_path = renderer
        await mod.render_math_image(
            r"\alpha", display_mode=False, output_dir=str(tmp_path)
        )
        html = open(render_mock.call_args.kwargs["html_path"]).read()
        assert "displayMode: false" in html

    @pytest.mark.asyncio
    async def test_html_latex_is_json_escaped(self, renderer):
        mod, render_mock, tmp_path = renderer
        await mod.render_math_image(
            r'\frac{"x"}{y}', display_mode=True, output_dir=str(tmp_path)
        )
        html = open(render_mock.call_args.kwargs["html_path"]).read()
        assert '\\"' in html or "\\u0022" in html

    @pytest.mark.asyncio
    async def test_html_references_local_katex(self, renderer):
        mod, render_mock, tmp_path = renderer
        await mod.render_math_image(
            r"\alpha", display_mode=True, output_dir=str(tmp_path)
        )
        html = open(render_mock.call_args.kwargs["html_path"]).read()
        assert mod.KATEX_CDN in html
        assert "katex.min.css" in html
        assert "file://" in html


# ---------------------------------------------------------------------------
# KaTeX availability tracks local assets
# ---------------------------------------------------------------------------


class TestKatexAvailability:
    def test_resolver_finds_first_local_install(self):
        from unittest.mock import patch

        mod = _import_renderer()
        target = mod._KATEX_CANDIDATES[1]
        with patch.object(
            mod.os.path, "isfile", side_effect=lambda p: p == f"{target}/katex.min.js"
        ):
            assert mod._resolve_katex_root() == f"file://{target}"

    def test_resolver_returns_none_without_local_install(self):
        from unittest.mock import patch

        mod = _import_renderer()
        with patch.object(mod.os.path, "isfile", return_value=False):
            assert mod._resolve_katex_root() is None
