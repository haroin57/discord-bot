"""Request-scoped successful validation reuse, without changing fallback policy."""
import asyncio
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from PIL import Image
import pytest

from utils import attachment_downloader as downloader


class Response:
    status = 200
    def __init__(self, body):
        self.body = body
        self.content = self
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def iter_chunked(self, size):
        yield self.body


class Session:
    def __init__(self, body):
        self.body = body
        self.gets = 0
        self.bytes = 0
    def get(self, url, *, allow_redirects):
        assert allow_redirects is False
        self.gets += 1
        self.bytes += len(self.body)
        return Response(self.body)


def png():
    stream = BytesIO()
    Image.new('RGB', (2, 2), 'blue').save(stream, format='PNG')
    return stream.getvalue()


def attachment(mime='image/png', filename='fixture.png'):
    return NS(id=123, url='https://cdn.discordapp.com/fixture', content_type=mime,
              filename=filename, size=0)


async def dedicated(session, item, tmp_path, *, max_bytes=10000):
    common = dict(max_bytes=max_bytes, semaphore=asyncio.Semaphore(1), host_check=lambda _: True)
    if item.content_type.startswith('image/'):
        return await downloader.download_image(session, item, attachment_dir=str(tmp_path), **common)
    return await downloader.download_text(session, item, **common)


async def arbitrary(session, item, tmp_path):
    return await downloader.download_arbitrary(
        session, item, attachment_dir=str(tmp_path), max_bytes=10000,
        denied_extensions=frozenset(), semaphore=asyncio.Semaphore(1),
        host_check=lambda _: True, sanitize_basename=lambda name: name,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['image', 'text'])
async def test_success_reuses_one_get_and_one_decode(tmp_path, monkeypatch, kind):
    body = png() if kind == 'image' else '検証済みテキスト'.encode()
    item = attachment() if kind == 'image' else attachment('text/plain', 'fixture.txt')
    name = '_fully_decoded_image' if kind == 'image' else '_decode_text'
    original = getattr(downloader, name)
    calls = []
    def decode(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(downloader, name, decode)
    observations = []
    for scoped in (False, True):
        calls.clear()
        session = Session(body)
        with downloader.attachment_reuse_scope() if scoped else nullcontext():
            assert await dedicated(session, item, tmp_path)
            assert await arbitrary(session, item, tmp_path) is None
        observations.append((session.gets, len(calls), session.bytes))
    assert observations == [(2, 2, len(body) * 2), (1, 1, len(body))]
    assert downloader._validated_attachments.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['image', 'text'])
async def test_orchestrator_children_share_only_the_current_receipt_set(tmp_path, kind):
    session = Session(png() if kind == 'image' else b'valid text')
    item = attachment() if kind == 'image' else attachment('text/plain', 'fixture.txt')
    async def specialized(http, att):
        return await dedicated(http, att, tmp_path)
    async def binary(http, att):
        return await arbitrary(http, att, tmp_path)
    with downloader.attachment_reuse_scope():
        if kind == 'image':
            result = await downloader.download_images(
                session, [item], image_content_types={'image/png'}, max_attachments=3,
                download_one=specialized,
            )
        else:
            result = await downloader.download_texts(
                session, [item], text_content_types={'text/plain'}, max_attachments=3,
                download_one=specialized,
            )
        assert result
        assert await downloader.download_arbitraries(
            session, [item], caller_is_admin=True, image_content_types={'image/png'},
            text_content_types={'text/plain'}, max_attachments=3, download_one=binary,
        ) == []
    assert session.gets == 1


@pytest.mark.asyncio
async def test_injected_downloader_result_is_not_treated_as_validation(tmp_path):
    session = Session(b'invalid image retained by binary fallback')
    item = attachment()
    with downloader.attachment_reuse_scope():
        assert await downloader.download_images(
            session, [item], image_content_types={'image/png'}, max_attachments=3,
            download_one=AsyncMock(return_value='/injected/image.png'),
        ) == ['/injected/image.png']
        fallback = await arbitrary(session, item, tmp_path)
    assert session.gets == 1
    assert fallback is not None


@pytest.mark.asyncio
async def test_reuse_does_not_expand_arbitrary_candidate_limit(tmp_path):
    session = Session(png())
    item, other = attachment(), attachment('application/octet-stream', 'other.bin')
    async def binary(http, att):
        return await arbitrary(http, att, tmp_path)
    with downloader.attachment_reuse_scope():
        assert await dedicated(session, item, tmp_path)
        assert await downloader.download_arbitraries(
            session, [item, other], caller_is_admin=True, image_content_types={'image/png'},
            text_content_types={'text/plain'}, max_attachments=1, download_one=binary,
        ) == []
    assert session.gets == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mime,filename,body', [
    ('image/jpeg', 'mislabeled.jpg', png()),
    ('image/png', 'broken.png', b'\x89PNG\r\n\x1a\ncorrupt'),
    ('text/plain', 'invalid.txt', b'\xff\x00\x01binary'),
])
async def test_failed_dedicated_validation_preserves_binary_retry(tmp_path, mime, filename, body):
    session = Session(body)
    item = attachment(mime, filename)
    with downloader.attachment_reuse_scope():
        assert await dedicated(session, item, tmp_path) is None
        fallback = await arbitrary(session, item, tmp_path)
    assert session.gets == 2
    assert fallback is not None
    assert Path(fallback['path']).read_bytes() == body


@pytest.mark.asyncio
@pytest.mark.parametrize('body,binary_expected', [(b'x' * 40, False), (b'\x00' * 40, True)])
async def test_oversize_dedicated_failure_still_runs_arbitrary_policy(tmp_path, body, binary_expected):
    session = Session(body)
    item = attachment('text/plain', 'large.txt')
    with downloader.attachment_reuse_scope():
        assert await dedicated(session, item, tmp_path, max_bytes=10) is None
        fallback = await arbitrary(session, item, tmp_path)
    assert session.gets == 2
    assert (fallback is not None) is binary_expected


@pytest.mark.asyncio
@pytest.mark.parametrize('field,new_value', [
    ('id', 124), ('url', 'https://cdn.discordapp.com/changed'),
    ('content_type', 'image/jpeg'), ('filename', 'renamed.png'), ('size', 1),
])
async def test_changed_attachment_identity_never_reuses_receipt(tmp_path, field, new_value):
    session = Session(png())
    item = attachment()
    with downloader.attachment_reuse_scope():
        assert await dedicated(session, item, tmp_path)
        setattr(item, field, new_value)
        await arbitrary(session, item, tmp_path)
    assert session.gets == 2


@pytest.mark.asyncio
async def test_new_group_and_new_object_do_not_reuse_previous_success(tmp_path):
    session = Session(png())
    item = attachment()
    with downloader.attachment_reuse_scope():
        assert await dedicated(session, item, tmp_path)
        with downloader.attachment_reuse_scope():
            await arbitrary(session, item, tmp_path)
        assert await arbitrary(session, item, tmp_path) is None
        await arbitrary(session, NS(**vars(item)), tmp_path)
    assert session.gets == 3
    with downloader.attachment_reuse_scope():
        await arbitrary(session, item, tmp_path)
    assert session.gets == 4


@pytest.mark.asyncio
async def test_concurrent_request_scopes_do_not_share_validation(tmp_path):
    item = attachment()
    session = Session(png())
    recorded = asyncio.Event()
    async def first():
        with downloader.attachment_reuse_scope():
            assert await dedicated(session, item, tmp_path)
            recorded.set()
            await asyncio.sleep(.005)
            assert await arbitrary(session, item, tmp_path) is None
    async def second():
        await recorded.wait()
        with downloader.attachment_reuse_scope():
            await arbitrary(session, item, tmp_path)
    await asyncio.gather(first(), second())
    assert session.gets == 2


@pytest.mark.asyncio
async def test_nonadmin_gate_is_unchanged(tmp_path):
    callback = AsyncMock()
    with downloader.attachment_reuse_scope():
        result = await downloader.download_arbitraries(
            Session(b'binary'), [attachment()], caller_is_admin=False,
            image_content_types={'image/png'}, text_content_types={'text/plain'},
            max_attachments=3, download_one=callback,
        )
    assert result == []
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_scope_does_not_leave_receipts(tmp_path):
    session = Session(png())
    item = attachment()
    with pytest.raises(asyncio.CancelledError):
        with downloader.attachment_reuse_scope():
            assert await dedicated(session, item, tmp_path)
            raise asyncio.CancelledError
    assert downloader._validated_attachments.get() is None
    await arbitrary(session, item, tmp_path)
    assert session.gets == 2


@pytest.mark.asyncio
async def test_context_seams_keep_provenance_scopes_separate():
    from bot_pipeline import context
    from bot_pipeline.admission import AttachmentLimits
    scopes, permissions = [], []
    item = attachment()
    async def images(http, attachments):
        scopes.append(downloader._validated_attachments.get())
        return []
    async def binaries(http, attachments, *, caller_is_admin):
        permissions.append(caller_is_admin)
        assert downloader._validated_attachments.get() is scopes[-1]
        return []
    async def value(result):
        return result
    owner = NS(reply=value(('', [item], False)), links=value('question'), thread=value(''),
               cross_thread=value(''), mem0=None, invest_kb=None, history_search=None,
               history_embed=None, will_resume=False, close=AsyncMock())
    deps = context.ContextDeps(
        launch_context_tasks=lambda **kwargs: owner, sanitize=lambda x:x, cap_text=lambda x,n:x[:n],
        get_session=Mock(), is_session_fresh=Mock(), fetch_thread_context=AsyncMock(),
        fetch_channel_context=AsyncMock(), resolve_message_links=AsyncMock(),
        resolve_channel_mention_contexts=AsyncMock(), search_memories_smart=AsyncMock(),
        should_use_memory_llm_mode=Mock(), get_http_session=AsyncMock(return_value=object()),
        download_attachments=images, attachment_metadata_by_path=Mock(return_value={}),
        archive_images=Mock(return_value=[]), download_text_attachments=AsyncMock(return_value=[]),
        download_arbitrary_attachments=binaries,
    )
    config = context.ContextConfig(set(), 1000, 5, False, 3, 1, False, 1, False, 1,
                                   AttachmentLimits({'image/png'}, {'text/plain'}, 10, 10, 10))
    result = await context.prepare_context(
        bot=NS(), message=NS(attachments=[item], author=NS(id=1), channel=NS(id=2), id=3),
        status=NS(update=AsyncMock()), prompt='question', raw_user_text='question', caller_is_admin=True,
        mem0_user_id='1', will_resume_override=False, image_paths=[], config=config, deps=deps,
    )
    assert len(scopes) == 2 and all(scope is not None for scope in scopes)
    assert scopes[0] is not scopes[1]
    assert permissions == [True, False]
    assert downloader._validated_attachments.get() is None
    await result.close()


@pytest.mark.asyncio
async def test_reaction_task_installs_scope_without_changing_downloader_seams(monkeypatch):
    from services import claude_tag
    scopes = []
    async def images(*args):
        scopes.append(downloader._validated_attachments.get())
        return []
    async def binaries(*args, **kwargs):
        assert downloader._validated_attachments.get() is scopes[0]
        assert kwargs == {'caller_is_admin': False}
        return []
    monkeypatch.setattr(claude_tag, 'download_attachments', images)
    monkeypatch.setattr(claude_tag, 'download_text_attachments', AsyncMock(return_value=[]))
    monkeypatch.setattr(claude_tag, 'download_arbitrary_attachments', binaries)
    monkeypatch.setattr(claude_tag, 'fetch_channel_context', AsyncMock(return_value=''))
    monkeypatch.setattr(claude_tag, 'ADMIN_USER_IDS', set())
    message = NS(author=NS(id=10, display_name='fixture'), content='task', attachments=[],
                 channel=NS(id=2), id=3, jump_url='')
    await claude_tag._build_task_prompt(message, NS(user_id=20, member=NS(display_name='reactor')),
                                       NS(get_http_session=AsyncMock(return_value=object())))
    assert scopes[0] is not None
    assert downloader._validated_attachments.get() is None
