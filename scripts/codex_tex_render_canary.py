#!/usr/bin/env python3
"""Exercise Codex-style TeX delimiters through the production PNG renderer."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLE = r"""
厳密には、元の行列 \(A\) の第 \(j\) 行による展開ではありません。

\[
B=
\begin{pmatrix}
 & & \\
a_{i1}&\cdots&a_{in} \quad \leftarrow j\text{行目}\\
 & &
\end{pmatrix}
\]

したがって、\[C_{ij}=|B|=0\] です。
"""
REGRESSION_FIXTURE = ROOT / "tests" / "fixtures" / "tex_inline_vertical_case.md"


def _png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError(f"renderer did not create a PNG: {path.name}")
    return struct.unpack(">II", header[16:24])


async def _run(*, keep_artifacts: bool) -> dict[str, Any]:
    from utils.browser_pool import shutdown_browser_daemon
    from utils.markdown_parser import MathSegment, parse_segments
    from utils.math_presentation import PresentationBlockKind, plan_segments
    from utils.math_renderer import render_math_image

    output_dir = Path(tempfile.mkdtemp(prefix="discord-codex-tex-"))
    sample_segments = parse_segments(SAMPLE)
    segments = [
        segment
        for segment in sample_segments
        if isinstance(segment, MathSegment)
    ]
    if len(segments) != 4:
        raise RuntimeError(f"expected 4 TeX segments, got {len(segments)}")
    matrix = next(
        (segment for segment in segments if r"\begin{pmatrix}" in segment.latex),
        None,
    )
    if matrix is None or not matrix.display_mode:
        raise RuntimeError("display matrix segment was not detected")

    sample_plan = plan_segments(sample_segments)
    sample_images = [
        block
        for block in sample_plan
        if block.kind is PresentationBlockKind.MATH_IMAGE
    ]
    if len(sample_images) != 1 or sample_images[0].source is not matrix:
        raise RuntimeError(
            f"expected only the matrix image, got {len(sample_images)} images"
        )

    regression_text = REGRESSION_FIXTURE.read_text(encoding="utf-8")
    regression_segments = parse_segments(regression_text)
    regression_plan = plan_segments(regression_segments)
    regression_images = sum(
        block.kind is PresentationBlockKind.MATH_IMAGE
        for block in regression_plan
    )
    if regression_images != 1:
        raise RuntimeError(
            "vertical-math regression fixture must produce exactly one image; "
            f"got {regression_images}"
        )

    try:
        rendered = await render_math_image(
            matrix.latex,
            display_mode=matrix.display_mode,
            output_dir=str(output_dir),
        )
        if not rendered:
            raise RuntimeError("math renderer returned no image")
        path = Path(rendered)
        width, height = _png_dimensions(path)
        return {
            "ok": width > 0 and height > 0,
            "detected_math_segments": len(segments),
            "detected_display_segments": sum(
                segment.display_mode for segment in segments
            ),
            "adaptive_sample_images": len(sample_images),
            "regression_math_segments": sum(
                isinstance(segment, MathSegment)
                for segment in regression_segments
            ),
            "regression_output_blocks": len(regression_plan),
            "regression_math_images": regression_images,
            "matrix_detected": True,
            "png_created": True,
            "png_width": width,
            "png_height": height,
            "artifact_dir": str(output_dir) if keep_artifacts else None,
        }
    finally:
        await shutdown_browser_daemon()
        if not keep_artifacts:
            shutil.rmtree(output_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-artifacts", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(_run(keep_artifacts=args.keep_artifacts))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
