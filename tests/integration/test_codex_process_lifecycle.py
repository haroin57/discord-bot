"""Local-only Codex transport child ownership and bounded cleanup."""
from __future__ import annotations

import asyncio
import os
import signal
import sys

import pytest

from services.codex_app_server.client import AsyncCodexAppServerClient
from services.claude_api.subprocess_runner import get_process_group_fence


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process groups required')
@pytest.mark.parametrize('cancel_cleanup', [False, True])
async def test_codex_cleanup_reaps_owned_group_and_preserves_other_process(cancel_cleanup):
    script = '''
import json, os, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
def stop(*args):
    child.wait(timeout=2)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print(json.dumps({'method':'ready', 'params':{'child':child.pid}}), flush=True)
time.sleep(30)
'''
    client = AsyncCodexAppServerClient(codex_bin=sys.executable, global_args=['-c', script])
    other = await asyncio.create_subprocess_exec(
        sys.executable, '-c', 'import time; time.sleep(30)',
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await client.start()
        ready = await client.next_event(timeout=3)
        child_pid = ready['params']['child']
        fence = get_process_group_fence(client._proc)
        assert fence is not None and fence.pgid == client._proc.pid
        close = asyncio.create_task(client.close(timeout=0.15))
        if cancel_cleanup:
            await asyncio.sleep(0.02)
            close.cancel()
            await asyncio.sleep(0.02)
            close.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(close, 4)
        else:
            await asyncio.wait_for(close, 4)
        assert client._proc.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert other.returncode is None
        assert all(task.done() for task in (client._stdout_task, client._stderr_task, client._wait_task))
    finally:
        await client.close(timeout=0.1)
        if client._proc is not None and client._proc.returncode is None:
            os.killpg(client._proc.pid, signal.SIGKILL)
            await client._proc.wait()
        other.terminate()
        await other.wait()


async def test_per_server_actors_close_real_task_affine_stdio_sessions(monkeypatch):
    from mcp_codex_bridge import ServerActors
    from services.claude_api import mcp_client
    script = '''
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method = request['method']
    if method == 'initialize':
        result = {'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'local-test','version':'1'}}
    elif method == 'tools/list':
        result = {'tools':[{'name':'echo','inputSchema':{'type':'object','properties':{}}}]}
    else:
        time.sleep(0.03)
        result = {'content':[{'type':'text','text':'ok'}],'isError':False}
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}), flush=True)
'''
    processes = []
    original = mcp_client._spawn_one

    async def spawn(stack, name, cfg, **kwargs):
        return await original(stack, name, cfg, process_observer=processes.append)

    monkeypatch.setattr(mcp_client, '_spawn_one', spawn)
    actors = ServerActors({key: {'command':sys.executable, 'args':['-c',script], 'startup_timeout':5}
                           for key in ('one', 'two')})
    try:
        answers = await asyncio.wait_for(asyncio.gather(
            actors.submit('call', 'mcp__one__echo', {}),
            actors.submit('call', 'mcp__two__echo', {}),
        ), 10)
        assert answers == ['ok', 'ok']
        assert len(processes) == 2
    finally:
        await asyncio.wait_for(actors.close(), 5)
    assert all(process.returncode is not None for process in processes)
    for process in processes:
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
