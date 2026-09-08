"""Provider-native planning, evidence, independent review, and revision."""
from __future__ import annotations
import asyncio
from dataclasses import replace
import logging
import os
from pathlib import Path
import stat

from services.codex_app_server import auxiliary
from services.codex_app_server.session import _invoke

log = logging.getLogger(__name__)


def _read_evidence(path: Path) -> str:
    # Bound the read itself, not merely the returned string. Reject symlinks
    # and nonregular files so a forged ledger path cannot read arbitrary data
    # or block the helper on a FIFO/device.
    if path.resolve(strict=True) != path:
        raise ValueError('Evidence symlinks are not allowed')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Evidence must be a regular file')
        with os.fdopen(fd, 'r', encoding='utf-8', errors='replace') as stream:
            fd = -1
            return stream.read(200000)
    finally:
        if fd >= 0:
            os.close(fd)


async def run(*, prompt, user_request: str, model: str, channel_id: int, execute,
              route_judgment: bool, evidence, status_callback=None,
              scratch_stream_callback=None, scratch_finalize_callback=None,
              verify_stream_callback=None, verify_finalize_callback=None):
    from config import (DELIB_PLANNER_ENABLED, DELIB_VERIFIER_ENABLED, DELIB_VERIFY_MAX_REVISIONS,
                        MAGI_LITE_ENABLED, DELIB_VERIFIER_MAX_TURNS)
    from services.claude_api.planner import generate_plan, render_plan_block
    from services.claude_api.verifier import _VERIFIER_SYSTEM, _parse_verdict, build_revise_prompt, build_caveat

    def prepend(text, original):
        return text + '\n\n' + original if isinstance(original, str) else [{'type':'text','text':text}, *original]

    if DELIB_PLANNER_ENABLED:
        await _invoke(status_callback, '計画を作成しています。')
        plan = await generate_plan(user_request=user_request, model=model, channel_id=channel_id)
        if plan is not None and not plan.trivial:
            block = render_plan_block(plan, scratchpad_available=True)
            prompt = prepend(block, prompt)
            await _invoke(scratch_stream_callback, '計画', block, 'write')
            await _invoke(scratch_finalize_callback)
    result = await execute(prompt)
    if MAGI_LITE_ENABLED and route_judgment:
        from services.claude_api.magi_lite import run_lenses, build_lens_revise_prompt
        await _invoke(status_callback, '独立した3視点で検討しています。')
        magi = await run_lenses(question=user_request, evidence_digest=evidence.digest(), channel_id=channel_id, model=model)
        if magi is not None and not magi.degraded:
            try:
                result = await execute(build_lens_revise_prompt(magi))
            except Exception:
                log.warning('Codex lens revision failed; retaining draft', exc_info=True)
    if not DELIB_VERIFIER_ENABLED:
        return result

    async def read_evidence(name, args):
        if name != 'Read':
            raise ValueError('Unsupported verifier tool')
        path = Path(os.path.abspath(str(args.get('file_path', ''))))
        allowed = {Path(os.path.abspath(record.raw_path)) for record in evidence.records if record.raw_path}
        if path not in allowed:
            raise ValueError('Verifier may only read this request evidence files')
        return await asyncio.to_thread(_read_evidence, path)

    for revision in range(DELIB_VERIFY_MAX_REVISIONS + 1):
        await _invoke(status_callback, '回答を独立したコンテキストで検証しています。')
        try:
            verified = await auxiliary.run_task(
                prompt=f'# Request\n{user_request[:6000]}\n\n# Draft\n{result.final_text[:24000]}\n\n# Evidence\n{evidence.digest(max_chars=18000)}',
                instructions=_VERIFIER_SYSTEM,
                tools=[{'name':'Read','description':'Read an exact raw file from the current request evidence ledger.',
                        'input_schema':{'type':'object','properties':{'file_path':{'type':'string'}},'required':['file_path']}}],
                execute_tool=read_evidence, effort='high', timeout_s=180,
                max_tool_calls=DELIB_VERIFIER_MAX_TURNS,
                thinking_stream_callback=verify_stream_callback,
                thinking_finalize_callback=verify_finalize_callback,
            )
            verdict = _parse_verdict(verified.final_text)
        except Exception:
            log.warning('Codex verifier unavailable; retaining draft without claiming verification', exc_info=True)
            return replace(result, final_text=result.final_text + '\n\n-# 自動検証を完了できていません。')
        if verdict.degraded:
            return replace(result, final_text=result.final_text + '\n\n-# 自動検証の結果を確認できていません。')
        if verdict.ok:
            return result
        if revision >= DELIB_VERIFY_MAX_REVISIONS:
            return replace(result, final_text=result.final_text + '\n\n' + build_caveat(verdict))
        try:
            result = await execute(build_revise_prompt(verdict))
        except Exception:
            log.warning('Codex revision failed; retaining reviewed draft with caveat', exc_info=True)
            return replace(result, final_text=result.final_text + '\n\n' + build_caveat(verdict))
    return result


async def review_chat(*, result, user_request, channel_id, evidence, tier,
                      incremental_text_callback=None):
    """Match the existing long, tool-backed chat response quality gate."""
    from config import DELIB_JUDGE_LITE_ENABLED
    from services.claude_api.verifier import should_run_judge, judge_lite, build_caveat
    if not DELIB_JUDGE_LITE_ENABLED or not should_run_judge(
        result.final_text, evidence, tier=tier, restricted=False,
    ):
        return result
    verdict = await judge_lite(user_request=user_request, draft=result.final_text,
                               evidence_digest=evidence.digest(), channel_id=channel_id)
    if not verdict.ok and verdict.issues:
        caveat = build_caveat(verdict)
        await _invoke(incremental_text_callback, caveat)
        return replace(result, final_text=result.final_text + '\n\n' + caveat)
    return result
