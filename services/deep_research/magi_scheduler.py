"""MAGI 3レンズの並列スケジューラー。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

import discord

from .types import LensResult


@dataclass(frozen=True)
class MagiScheduler:
    max_parallel: int
    lens_order: tuple[str, ...]
    lens_spec: dict
    lens_emoji: dict[str, str]
    view_factory: Callable
    run_lens: Callable[..., Awaitable[LensResult]]
    run_integrator: Callable[..., Awaitable[str]]
    safe_edit: Callable[..., Awaitable[None]]

    async def run(self, ctx, *, refine, approve) -> str | None:
        await refine(ctx)
        await approve(ctx)
        await self.safe_edit(
            ctx.anchor,
            "🔬 MAGI 推論中…（科学 / 直観 / 懐疑 が中心的問いに独立回答）",
        )
        views = {}
        for key in self.lens_order:
            label = self.lens_spec[key][0]
            try:
                message = await ctx.thread.send(f"⏳ {label} — 待機中")
            except discord.HTTPException:
                message = None
            views[key] = self.view_factory(label, message, emoji=self.lens_emoji[key])

        semaphore = asyncio.Semaphore(self.max_parallel)

        async def one(key: str) -> LensResult:
            result = await self.run_lens(ctx, key, views[key], semaphore)
            await views[key].finish(result.ok, result.stance)
            return result

        raw = await asyncio.gather(
            *(one(key) for key in self.lens_order),
            return_exceptions=True,
        )
        results: list[LensResult] = []
        for key, result in zip(self.lens_order, raw):
            if isinstance(result, LensResult):
                results.append(result)
            else:
                results.append(
                    LensResult(
                        lens_key=key,
                        lens_label=self.lens_spec[key][0],
                        stance="",
                        body="",
                        ok=False,
                        error=str(result),
                    )
                )
        ctx.state.lens_outputs = results
        if not any(result.ok and result.body.strip() for result in results):
            return None
        await self.safe_edit(
            ctx.anchor, "🔬 🧩 3視点を統合しています…（合意と相違を抽出）"
        )
        return await self.run_integrator(ctx)
