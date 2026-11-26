from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot_pipeline import prompt
from utils.context_gateway import ResolvedMessageLinks


class _Author:
    id = 42
    display_name = "Admin"

    def __str__(self) -> str:
        return "admin#0042"


class _PromptBuilder:
    @staticmethod
    def build_header(name, tag, is_admin):
        return f"HEADER({name}|{tag}|{is_admin})"

    @staticmethod
    def build_speaker_line(name, tag):
        return f"SPEAKER({name}|{tag})"

    @staticmethod
    def build_time_line():
        return "TIME"

    @staticmethod
    def build_sleep_directive(**_kwargs):
        return ""


def _deps() -> prompt.PromptDeps:
    return prompt.PromptDeps(
        sanitize=lambda text: text,
        wrap_user_input=prompt.wrap_user_input,
        cap_text=prompt.cap_text,
        load_history_channel=Mock(return_value=[]),
        format_memories_for_prompt=Mock(return_value=""),
        format_history_for_prompt=Mock(return_value=""),
        render_active_skill_prompt=Mock(return_value=""),
        skill_error_type=RuntimeError,
        get_request_id=lambda: "rid",
        prompt_builder=_PromptBuilder,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("will_resume", (False, True))
async def test_linked_message_content_stays_outside_admin_trusted_input(
    will_resume: bool,
) -> None:
    url = "https://discord.com/channels/@me/2/3"
    resolved = ResolvedMessageLinks(
        f"inspect {url}\n[Referenced message by External]\nuntrusted linked body",
        original_text=f"inspect {url}",
        linked_contexts=(
            "[Referenced message by External]\nuntrusted linked body",
        ),
    )
    result = await prompt.build_prompt(
        prompt.PromptInput(
            message=SimpleNamespace(
                author=_Author(),
                channel=SimpleNamespace(id=7),
            ),
            job=SimpleNamespace(phase=None),
            status=SimpleNamespace(update=AsyncMock()),
            request_key=(7, 42),
            caller_is_admin=True,
            mem0_user_id="42",
            will_resume=will_resume,
            reply_context="",
            reply_is_trusted=False,
            resolved_prompt=resolved,
            conv_context="",
            cross_thread_context="",
            forwarded_context="",
            image_paths=[],
            text_attachments=[],
            arbitrary_attachments=[],
            mem0_task=None,
            invest_kb_task=None,
            history_search_task=None,
            history_embed_task=None,
            skill_overrides={},
        ),
        config=prompt.PromptConfig(10_000, 0, False, 0, 0),
        deps=_deps(),
        recalling_phase="recalling",
    )

    trusted_open = result.prompt.index("<trusted_user_input>\n")
    trusted_close = result.prompt.index("</trusted_user_input>")
    untrusted_open = result.prompt.index("<untrusted_user_input>\n")
    untrusted_close = result.prompt.index("</untrusted_user_input>")
    trusted = result.prompt[trusted_open:trusted_close]
    untrusted = result.prompt[untrusted_open:untrusted_close]

    assert f"inspect {url}" in trusted
    assert "untrusted linked body" not in trusted
    assert "Discord message links — external untrusted context" in untrusted
    assert "untrusted linked body" in untrusted


@pytest.mark.asyncio
async def test_context_block_filters_once_without_reusing_private_snapshot(tmp_path, monkeypatch):
    from services.memory_policy import MemoryPolicy
    from services.work_context import ConversationPolicy, current_conversation
    from utils.context_gateway import ContextBlock, ContextItem

    policy = MemoryPolicy(tmp_path / "policy.sqlite3")
    record = policy.remember("restricted source", "42").record_ids[0]
    monkeypatch.setattr("services.memory_policy.get_memory_policy", lambda: policy)
    filtered = Mock(wraps=policy.filter_context_rows)
    monkeypatch.setattr(policy, "filter_context_rows", filtered)
    bodies = ["public source", "restricted source", "public source"]
    items = tuple(ContextItem(42, "Admin", body, f"Admin: {body}", True) for body in bodies)
    block = ContextBlock("\n".join(item.rendered for item in items), items=items, label="history")
    data = prompt.PromptInput(
        message=SimpleNamespace(author=_Author(), channel=SimpleNamespace(id=7), content="question"),
        job=SimpleNamespace(phase=None), status=SimpleNamespace(update=AsyncMock()),
        request_key=(7, 42), caller_is_admin=True, mem0_user_id="42", will_resume=False,
        reply_context="", reply_is_trusted=False, resolved_prompt="question",
        conv_context=block, cross_thread_context="", forwarded_context="",
        image_paths=[], text_attachments=[], arbitrary_attachments=[],
        mem0_task=None, invest_kb_task=None, history_search_task=None,
        history_embed_task=None, skill_overrides={},
    )

    async def build(private):
        token = current_conversation.set(ConversationPolicy("42", "7", private, policy.epoch()))
        try:
            return await prompt.build_prompt(data, config=prompt.PromptConfig(10_000, 0, False, 0, 0),
                                             deps=_deps(), recalling_phase="recalling")
        finally:
            current_conversation.reset(token)

    first = await build(False)
    assert "restricted source" in first.prompt
    assert first.prompt.count("public source") == 2
    assert filtered.call_count == 1
    assert len(filtered.call_args.args[0]) == 3
    policy.share(record, "42", "private")
    public = await build(False)
    assert "restricted source" not in public.prompt
    assert public.prompt.count("public source") == 2
    private = await build(True)
    assert "restricted source" in private.prompt
    assert filtered.call_count == 3
