"""Provider workflow that owns cross-provider session coherence."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from services.providers.contracts import ProviderName, ProviderRequest, ProviderResult
from services.providers.execution import ProviderDispatcher

log = logging.getLogger(__name__)
_continuity_cleanup_tasks: set[asyncio.Task[None]] = set()


@dataclass(frozen=True, slots=True)
class ProviderWorkflow:
    dispatcher: ProviderDispatcher

    async def run(
        self,
        request: ProviderRequest,
        provider: ProviderName,
    ) -> ProviderResult:
        await _ensure_session_registry_ready()
        await _prepare_provider_continuity(request, provider)
        return await self._run_with_retirement(request, provider)

    async def _run_with_retirement(
        self,
        request: ProviderRequest,
        provider: ProviderName,
    ) -> ProviderResult:
        """Dispatch a provider and durably retire its dormant peer."""

        try:
            result = await self.dispatcher.dispatch(request, provider)
        except BaseException as dispatch_error:
            await _finish_provider_retirement(
                request,
                provider,
                dispatch_error=dispatch_error,
            )
            # Cleanup succeeded. A bare raise preserves the original exception
            # object and dispatch traceback. Cancellation is re-raised by the
            # helper so the first CancelledError also retains priority.
            raise

        await _finish_provider_retirement(request, provider)
        return result


class ProviderContinuityError(RuntimeError):
    """A stale provider thread could not be retired safely."""


async def _finish_provider_retirement(
    request: ProviderRequest,
    provider: ProviderName,
    *,
    dispatch_error: BaseException | None = None,
) -> None:
    """Complete post-dispatch cleanup despite repeated cancellation.

    Cleanup failure takes precedence and fails closed. Otherwise the caller
    re-raises its original dispatch exception, or this helper re-raises the first
    cancellation that initiated/interrupted cleanup.
    """

    cleanup_task = asyncio.create_task(
        _retire_dormant_provider(request, provider),
        name=(
            f"provider-continuity-cleanup:{provider}:"
            f"{request.channel_id}:{request.user_id}"
        ),
    )
    _continuity_cleanup_tasks.add(cleanup_task)
    cleanup_task.add_done_callback(_continuity_cleanup_done)
    cancellations: list[asyncio.CancelledError] = []
    if isinstance(dispatch_error, asyncio.CancelledError):
        cancellations.append(dispatch_error)

    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as cancel_error:
            if cleanup_task.cancelled():
                break
            cancellations.append(cancel_error)
        except BaseException:
            if cleanup_task.done():
                break
            raise

    try:
        cleanup_task.result()
    except ProviderContinuityError as cleanup_error:
        if dispatch_error is not None:
            if cleanup_error.__cause__ is not None:
                cleanup_error.add_note(
                    f"continuity cleanup failure: {cleanup_error.__cause__!r}"
                )
            raise cleanup_error from dispatch_error
        raise
    except BaseException as cleanup_error:
        continuity_error = ProviderContinuityError(
            f"{provider.title()} provider continuity cleanup did not complete"
        )
        raise continuity_error from (dispatch_error or cleanup_error)

    if cancellations:
        raise cancellations[0]


def _continuity_cleanup_done(task: asyncio.Task[None]) -> None:
    """Release the strong reference and always retrieve terminal failures."""

    _continuity_cleanup_tasks.discard(task)
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


async def _ensure_session_registry_ready() -> None:
    from services.session import ensure_session_registry_loaded

    try:
        await ensure_session_registry_loaded()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ProviderContinuityError(
            "session registry is unavailable for provider dispatch"
        ) from exc


async def _prepare_provider_continuity(
    request: ProviderRequest,
    provider: ProviderName,
) -> None:
    from services.session import (
        is_provider_continuity_invalid,
        validate_provider_continuity,
    )

    if not is_provider_continuity_invalid(
        provider,
        request.channel_id,
        request.user_id,
    ):
        return
    if provider == "claude":
        # Invalidation atomically removed the registry-owned Claude session.
        validate_provider_continuity(provider, request.channel_id, request.user_id)
        return

    from services.codex_app_server.manager import clear_codex_thread

    try:
        await clear_codex_thread(request.channel_id, request.user_id)
    except Exception as exc:
        raise ProviderContinuityError(
            "stale Codex provider continuity could not be retired"
        ) from exc
    validate_provider_continuity(provider, request.channel_id, request.user_id)


async def _retire_dormant_provider(
    request: ProviderRequest,
    provider: ProviderName,
) -> None:
    from services.session import (
        invalidate_provider_continuity,
        validate_provider_continuity,
    )

    dormant: ProviderName = "claude" if provider == "codex" else "codex"
    invalidate_provider_continuity(dormant, request.channel_id, request.user_id)
    try:
        if provider == "codex":
            try:
                from services.claude_api.session_store import _clear_session_time

                _clear_session_time(request.channel_id, request.user_id)
            except ImportError:
                pass
        else:
            from services.codex_app_server.manager import clear_codex_thread

            await clear_codex_thread(request.channel_id, request.user_id)
            validate_provider_continuity(
                dormant,
                request.channel_id,
                request.user_id,
            )
    except Exception as exc:
        log.warning(
            "Dormant provider session cleanup failed for channel=%s: %s",
            request.channel_id,
            exc,
        )

    # The provider response must not escape until the continuity transition is
    # durable. This applies even when dormant cleanup succeeds: a process crash
    # after returning the response must not resurrect the previous provider.
    from services.session import flush_session_registry

    try:
        await flush_session_registry()
    except Exception as flush_exc:
        raise ProviderContinuityError(
            "failed provider cleanup marker could not be persisted"
        ) from flush_exc
