"""Native Python implementations of the built-in tools the bot exposes.

Tools advertised here are returned by ``builtin_tools_for_api(restricted)``
and dispatched by ``execute_builtin_tool(name, args, ctx)``. MCP tools live
in services/claude_api/mcp_client.py; tool_loop.py merges both sets and
routes calls to the right backend.

Restricted mode rejects every tool here at the policy gate
(services/claude_api/restricted.py). The implementations carry their own
defenses as a second layer: O_NOFOLLOW, output caps, and the env allowlist
apply UNCONDITIONALLY. The cwd jail, however, is tied to ToolContext.restricted
— it is enforced for restricted (non-admin) callers and intentionally dropped
for admins (restricted=False), who are trusted with full-filesystem access
(e.g. reading ~/manga). Because both the policy gate and the cwd jail key off
the same `restricted` flag, the cwd jail is NOT an independent backstop against
a mis-set flag; the always-on defenses above are.

Ports / inspiration:
- Read   → free-code/src/tools/FileReadTool/FileReadTool.tsx
- Write  → free-code/src/tools/FileWriteTool/FileWriteTool.tsx
- Edit   → free-code/src/tools/FileEditTool/FileEditTool.tsx
- Bash   → free-code/src/utils/Shell.ts:275-278 (spawn('/bin/bash', ['-c', cmd]))
- Glob   → free-code/src/tools/GlobTool/
- Grep   → free-code/src/tools/GrepTool/
"""

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


# Tell Pillow it's OK to decode a partially-written or truncated image
# file instead of raising. Without this, a single bad page (e.g. one
# truncated PNG inside a manga directory) breaks the entire `_tool_read`
# image path: Pillow.fails → we used to pass the raw bytes through →
# Anthropic API rejects with "image dimensions exceed 2000 pixels for
# many-image requests" → the agent loop retries the entire user turn,
# reading dozens of pages each retry, and never converges. Setting this
# globally is the same default the live Claude CLI ships with.
try:
    from PIL import ImageFile as _PILImageFile
    _PILImageFile.LOAD_TRUNCATED_IMAGES = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Limits / config
# ---------------------------------------------------------------------------
READ_MAX_BYTES = 1_000_000  # 1 MB
READ_MAX_LINES = 2000
WRITE_MAX_BYTES = 1_000_000

# Image read cap. Anthropic API accepts up to 5 MB per image attachment;
# we keep the same ceiling so the tool_result block stays valid downstream.
IMAGE_READ_MAX_BYTES = 5_000_000  # 5 MB
# Long-edge target after Pillow resize. Claude vision downsamples every
# input image to 1568px on the long edge anyway, so sending anything
# bigger just wastes bytes / trips the many-image 2000px ceiling. Aligning
# with that internal limit nets both quota savings and 400-error immunity.
IMAGE_TARGET_MAX_DIM = 1568
# Extensions we route to the image branch of Read. text formats fall
# through to the UTF-8 decode path so .svg / .html stay readable as code.
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_IMAGE_MEDIA_TYPES = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif":  "image/gif",
}
# Pillow save kwargs per format. PNG is lossless so just request optimize;
# JPEG/WebP take a quality setting. GIF we never re-encode (animation
# would break).
_IMAGE_SAVE_KWARGS = {
    "image/png":  {"format": "PNG",  "optimize": True},
    "image/jpeg": {"format": "JPEG", "quality": 85, "optimize": True},
    "image/webp": {"format": "WEBP", "quality": 85},
}

GLOB_MAX_RESULTS = 1000
GREP_MAX_RESULTS = 1000

# Default and MAX are separate knobs, mirroring the CLI's
# BASH_DEFAULT_TIMEOUT_MS / BASH_MAX_TIMEOUT_MS pair. The old code clamped the
# model-supplied timeout DOWN to the default (min(timeout, default)), which
# silently disabled the argument in the upward direction while the tool-loop
# status line displayed the requested value — a contract lie (audit #1).
BASH_DEFAULT_TIMEOUT_S = float(os.environ.get("CLAUDE_BASH_TIMEOUT_S", "120"))
BASH_MAX_TIMEOUT_S = float(os.environ.get("CLAUDE_BASH_MAX_TIMEOUT_S", "600"))
# Per-stream output cap. The CLI defaults to ~30KB; the old 100KB×2 was a
# token sink (audit #13). Truncation is middle-cut (head+tail preserved) so
# the FAILED/traceback tail of a test run is never hidden from the model.
BASH_OUTPUT_CAP_BYTES = int(os.environ.get("CLAUDE_BASH_OUTPUT_CAP_BYTES", "30000"))

# Whitelist of env vars that survive into Bash subprocesses. Anything else
# (ANTHROPIC_API_KEY, DISCORD_TOKEN, etc.) must not leak to spawned shells.
BASH_ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TZ", "SHELL")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------
@dataclass
class ToolContext:
    cwd: str
    restricted: bool
    channel_id: int
    # In-memory todo list for the current agent loop. Scoped to one
    # call_with_tools invocation (entry.py builds a fresh ToolContext per
    # request). TodoWrite replaces the list whole; the model reads the
    # state back via the handler's return value or by inspecting prior
    # tool_result blocks within the same loop.
    todos: list[dict] = field(default_factory=list)


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict, ToolContext], Awaitable[str]]


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------
def _resolve_under(cwd: str, path: str, *, restricted: bool = True) -> str:
    """Resolve ``path`` (relative or absolute).

    With ``restricted=True`` (default) the result must stay under ``cwd``,
    raising ``ValueError`` otherwise. With ``restricted=False`` the cwd
    jail is dropped: this is admin-only behaviour for cases like reading
    ``~/manga/*.png`` from outside the bot's repo. The realpath
    resolution + symlink-following still applies, but no containment
    check is enforced.

    O_NOFOLLOW is still applied at open() time by the callers, so a
    symlink swap at the final component still fails safely.
    """
    cwd_real = os.path.realpath(cwd)
    candidate = path if os.path.isabs(path) else os.path.join(cwd_real, path)
    resolved = os.path.realpath(candidate)
    if not restricted:
        return resolved
    # The exact cwd is allowed; subpaths must use the separator boundary so
    # /tmp/foo is not accepted as "under" /tmp/foobar.
    if resolved != cwd_real and not resolved.startswith(cwd_real + os.sep):
        raise ValueError(
            f"path escapes cwd: {path!r} → {resolved!r} (cwd={cwd_real!r})",
        )
    return resolved


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
def _read_file_sync(path: str) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if st.st_size > READ_MAX_BYTES:
            raise ValueError(
                f"file too large: {st.st_size} bytes > {READ_MAX_BYTES} cap",
            )
        with os.fdopen(fd, "rb") as fh:
            fd = -1  # ownership transferred
            data = fh.read(READ_MAX_BYTES)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    if len(lines) > READ_MAX_LINES:
        head = "".join(lines[:READ_MAX_LINES])
        return head + f"\n[truncated at {READ_MAX_LINES} lines]"
    return text


def _read_image_sync(path: str) -> list[dict]:
    """Read an image file and pack it as Anthropic tool_result image blocks.

    Returns the list shape the tool_loop forwards verbatim to the API:

        [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png",
                                         "data": "<base64>"}},
            {"type": "text", "text": "[Read image: ... 1.2 MB]"},
        ]

    Pillow resizes any image whose long edge exceeds
    ``IMAGE_TARGET_MAX_DIM`` (1568). Claude vision internally downsamples
    to that ceiling anyway, so we strip the wasted pixels at the source.
    More importantly, the Anthropic API's many-image branch rejects
    images >2000px per dimension — without this resize, the second Read
    in a session reliably 400s on manga-quality PNGs (1700x2200).
    Animated GIFs and Pillow-decode failures fall through to the raw-bytes
    path so a broken decoder can never block a working read.
    """
    import base64
    from io import BytesIO

    ext = os.path.splitext(path)[1].lower()
    media_type = _IMAGE_MEDIA_TYPES.get(ext)
    if media_type is None:
        raise ValueError(f"unsupported image extension: {ext}")

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if st.st_size > IMAGE_READ_MAX_BYTES:
            raise ValueError(
                f"image too large: {st.st_size} bytes > {IMAGE_READ_MAX_BYTES} cap "
                f"(Anthropic API rejects >5MB)",
            )
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            data = fh.read(IMAGE_READ_MAX_BYTES)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    final_bytes, final_media = _maybe_resize_image(data, media_type, path)

    b64 = base64.standard_b64encode(final_bytes).decode("ascii")
    size_kb = len(final_bytes) / 1024
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": final_media,
                "data": b64,
            },
        },
        {
            "type": "text",
            "text": f"[Read image: {os.path.basename(path)} ({size_kb:.0f} KB, {final_media})]",
        },
    ]


def _maybe_resize_image(
    data: bytes, media_type: str, path_for_log: str,
) -> tuple[bytes, str]:
    """Return ``(bytes, media_type)`` shrunk to ≤ IMAGE_TARGET_MAX_DIM
    and (when safe) re-encoded as JPEG.

    Strategy:
      - Animated GIF → pass through (re-encoding kills the animation).
      - Pillow decode failure → pass through (corrupt / exotic format).
      - Image with no alpha channel → resize *and* convert to JPEG
        (quality 85). PNG is lossless so cumulative manga Reads pile up
        to 25-30 MB / request before the model has even started
        thinking; JPEG at the same visible quality cuts each page from
        ~1 MB to ~150 KB, keeping a 20-page review under 5 MB total.
      - Image WITH alpha → keep PNG (transparency would otherwise be
        flattened onto an arbitrary background).

    Claude vision internally downsamples every input to 1568 px on the
    long edge anyway, so the JPEG re-encode at <=1568 px loses nothing
    the model would have seen.
    """
    from io import BytesIO
    try:
        from PIL import Image
    except ImportError:
        return data, media_type

    try:
        with Image.open(BytesIO(data)) as img:
            # Animated GIFs: leave alone.
            if media_type == "image/gif" and getattr(img, "is_animated", False):
                return data, media_type

            # Detect transparency. Pillow exposes it either via the mode
            # (RGBA / LA / PA) or via the info dict (P-mode PNGs that
            # carry a tRNS chunk).
            has_alpha = (
                img.mode in ("RGBA", "LA", "PA")
                or (img.mode == "P" and "transparency" in img.info)
            )

            # Pick the output format. Alpha-bearing images stay in their
            # original (lossless) format so we don't compose the alpha
            # onto a guessed background. Everything else goes JPEG.
            if has_alpha:
                out_media = media_type  # PNG / WebP
            else:
                out_media = "image/jpeg"

            w, h = img.size
            needs_resize = max(w, h) > IMAGE_TARGET_MAX_DIM
            if not needs_resize and out_media == media_type:
                return data, media_type  # nothing to do

            img_copy = img.copy()
            if needs_resize:
                img_copy.thumbnail(
                    (IMAGE_TARGET_MAX_DIM, IMAGE_TARGET_MAX_DIM),
                    Image.Resampling.LANCZOS,
                )

            # JPEG mode prep: must be RGB (or L for grayscale).
            if out_media == "image/jpeg" and img_copy.mode not in ("RGB", "L"):
                img_copy = img_copy.convert("RGB")

            save_kwargs = _IMAGE_SAVE_KWARGS.get(out_media)
            if save_kwargs is None:
                return data, media_type

            buf = BytesIO()
            img_copy.save(buf, **save_kwargs)
            resized = buf.getvalue()
            log.info(
                "Read image: %s %dx%d %s (%d B) → %dx%d %s (%d B)",
                os.path.basename(path_for_log),
                w, h, media_type, len(data),
                img_copy.size[0], img_copy.size[1], out_media, len(resized),
            )
            return resized, out_media
    except Exception as exc:
        # Pillow completely choked (rare — LOAD_TRUNCATED_IMAGES handles
        # most malformed-input cases). We can't verify the dimensions
        # without it, and silently passing the raw bytes through is what
        # caused the 2026-05-15 stuck-loop incident: a 2200px image
        # slipped past us and the API 400'd every retry. Refuse the read
        # instead — the agent loop sees a tool error and can move on.
        log.warning(
            "Read image: Pillow failed for %s (%s); cannot verify dimensions, "
            "refusing pass-through",
            path_for_log, exc,
        )
        raise ValueError(
            f"image decode failed and dimensions cannot be verified: {exc}",
        )


async def _tool_read(args: dict, ctx: ToolContext) -> "str | list[dict]":
    raw = args.get("file_path")
    if not raw:
        raise ValueError("file_path is required")
    # Admin/non-restricted callers can read outside the repo (e.g. ~/manga
    # for the kanae bot owner). Restricted callers (non-admin Discord users)
    # remain pinned under cwd.
    path = _resolve_under(ctx.cwd, raw, restricted=ctx.restricted)

    ext = os.path.splitext(path)[1].lower()
    if ext in _IMAGE_EXTENSIONS:
        return await asyncio.to_thread(_read_image_sync, path)
    return await asyncio.to_thread(_read_file_sync, path)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
def _write_file_sync(path: str, content: str) -> int:
    data = content.encode("utf-8")
    if len(data) > WRITE_MAX_BYTES:
        raise ValueError(
            f"content too large: {len(data)} bytes > {WRITE_MAX_BYTES} cap",
        )
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        raise FileNotFoundError(f"parent directory missing: {parent}")
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    return len(data)


async def _tool_write(args: dict, ctx: ToolContext) -> str:
    raw_path = args.get("file_path")
    content = args.get("content")
    if not raw_path:
        raise ValueError("file_path is required")
    if content is None:
        raise ValueError("content is required")
    path = _resolve_under(ctx.cwd, raw_path, restricted=ctx.restricted)
    n = await asyncio.to_thread(_write_file_sync, path, content)
    return f"wrote {n} bytes to {path}"


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------
async def _tool_edit(args: dict, ctx: ToolContext) -> str:
    raw_path = args.get("file_path")
    old = args.get("old_string")
    new = args.get("new_string")
    replace_all = bool(args.get("replace_all"))
    if not raw_path or old is None or new is None:
        raise ValueError("file_path, old_string, new_string are required")
    if old == new:
        raise ValueError("old_string and new_string are identical")
    path = _resolve_under(ctx.cwd, raw_path, restricted=ctx.restricted)
    content = await asyncio.to_thread(_read_file_sync, path)
    occurrences = content.count(old)
    if occurrences == 0:
        raise ValueError(f"old_string not found in {path}")
    if not replace_all and occurrences > 1:
        raise ValueError(
            f"old_string occurs {occurrences} times; pass replace_all=True or "
            "include more surrounding context to make it unique",
        )
    updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
    n = await asyncio.to_thread(_write_file_sync, path, updated)
    return f"edited {path} ({occurrences} replacement{'s' if occurrences != 1 else ''}, {n} bytes)"


# ---------------------------------------------------------------------------
# TodoWrite
# ---------------------------------------------------------------------------
_TODO_STATUSES = ("pending", "in_progress", "completed")
_TODO_MAX_COUNT = 100
_TODO_MAX_FIELD_CHARS = 500


def _validate_todo_item(item: object, idx: int) -> dict:
    if not isinstance(item, dict):
        raise ValueError(f"todos[{idx}] must be an object")
    content = item.get("content")
    status = item.get("status")
    active_form = item.get("activeForm")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"todos[{idx}].content must be a non-empty string")
    if not isinstance(active_form, str) or not active_form.strip():
        raise ValueError(f"todos[{idx}].activeForm must be a non-empty string")
    if status not in _TODO_STATUSES:
        raise ValueError(
            f"todos[{idx}].status must be one of {_TODO_STATUSES}, got {status!r}"
        )
    if len(content) > _TODO_MAX_FIELD_CHARS or len(active_form) > _TODO_MAX_FIELD_CHARS:
        raise ValueError(
            f"todos[{idx}] content/activeForm exceeds {_TODO_MAX_FIELD_CHARS} chars"
        )
    return {"content": content, "status": status, "activeForm": active_form}


async def _tool_todo_write(args: dict, ctx: ToolContext) -> str:
    todos = args.get("todos")
    if not isinstance(todos, list):
        raise ValueError("todos must be an array")
    if len(todos) > _TODO_MAX_COUNT:
        raise ValueError(f"too many todos: {len(todos)} > {_TODO_MAX_COUNT}")
    validated = [_validate_todo_item(item, i) for i, item in enumerate(todos)]
    in_progress = sum(1 for t in validated if t["status"] == "in_progress")
    if in_progress > 1:
        raise ValueError(
            f"only one todo may be in_progress at a time, got {in_progress}"
        )
    ctx.todos = validated
    counts = {s: sum(1 for t in validated if t["status"] == s) for s in _TODO_STATUSES}
    if not validated:
        return "TodoWrite: cleared (0 todos)"
    lines = [
        f"TodoWrite: {len(validated)} todos "
        f"({counts['in_progress']} in_progress, "
        f"{counts['pending']} pending, "
        f"{counts['completed']} completed)"
    ]
    for i, t in enumerate(validated, 1):
        marker = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}[t["status"]]
        lines.append(f"  {i}. {marker} {t['content']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bash
# ---------------------------------------------------------------------------
def _bash_env() -> dict:
    return {k: v for k, v in os.environ.items() if k in BASH_ENV_ALLOWLIST}


class _StreamCapture:
    """Bounded head+tail capture of a subprocess stream.

    Keeps the first ``cap`` bytes and a rolling buffer of the last ``cap``
    bytes, so memory stays bounded for arbitrarily chatty commands while the
    middle-cut rendering below can still show both the start of the output
    and the tail (where test FAILED lines / tracebacks live).
    """

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if len(self.head) < self.cap:
            take = self.cap - len(self.head)
            self.head.extend(chunk[:take])
            chunk = chunk[take:]
        if chunk:
            self.tail.extend(chunk)
            if len(self.tail) > self.cap:
                del self.tail[:len(self.tail) - self.cap]

    def render(self) -> str:
        """Decoded output, middle-cut when over budget (audit #13)."""
        if self.total <= self.cap:
            return self.head.decode("utf-8", errors="replace")
        keep = self.cap // 2
        head = self.head[:keep].decode("utf-8", errors="replace")
        tail = self.tail[-keep:].decode("utf-8", errors="replace")
        dropped = self.total - 2 * keep
        return f"{head}\n... [{dropped} bytes truncated] ...\n{tail}"


async def _drain_stream(stream, capture: _StreamCapture) -> None:
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        capture.feed(chunk)


async def _tool_bash(args: dict, ctx: ToolContext) -> str:
    cmd = args.get("command")
    if not cmd or not isinstance(cmd, str):
        raise ValueError("command is required (string)")
    # The model-supplied timeout is honored up to BASH_MAX_TIMEOUT_S (CLI
    # parity: default and max are independent). Values are SECONDS.
    timeout_s = float(args.get("timeout", BASH_DEFAULT_TIMEOUT_S))
    timeout_s = max(1.0, min(timeout_s, BASH_MAX_TIMEOUT_S))

    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "-c", cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=ctx.cwd,
        env=_bash_env(),
        start_new_session=True,  # isolate process group for clean termination
    )
    # Incremental drain instead of communicate(): on timeout the buffered
    # partial output is returned to the model (audit #10) instead of being
    # discarded with a bare TimeoutError — `npm test` / `pip install` runs
    # that time out keep their diagnostics.
    out_cap = _StreamCapture(BASH_OUTPUT_CAP_BYTES)
    err_cap = _StreamCapture(BASH_OUTPUT_CAP_BYTES)
    drain = asyncio.gather(
        _drain_stream(proc.stdout, out_cap),
        _drain_stream(proc.stderr, err_cap),
    )
    timed_out = False
    try:
        await asyncio.wait_for(drain, timeout=timeout_s)
        await proc.wait()
    except asyncio.TimeoutError:
        timed_out = True
        drain.cancel()
        try:
            await drain
        except (asyncio.CancelledError, Exception):
            pass
        # Kill the whole process group
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (OSError, ProcessLookupError):
            pass

    out = out_cap.render()
    err = err_cap.render()
    pieces = []
    if out:
        pieces.append(out)
    if err:
        pieces.append(f"--- stderr ---\n{err}")
    if timed_out:
        pieces.append(
            f"--- command timed out after {timeout_s:.0f}s; "
            "partial output above ---"
        )
    else:
        pieces.append(f"--- exit code: {proc.returncode} ---")
    return "\n".join(pieces)


# ---------------------------------------------------------------------------
# Glob
# ---------------------------------------------------------------------------
def _glob_sync(cwd: str, pattern: str, restricted: bool) -> list[str]:
    """Run a glob against ``cwd`` (relative pattern) or anywhere on disk
    (absolute pattern, admin only).

    Pathlib's ``Path.rglob`` rejects absolute patterns outright with
    ``Non-relative patterns are unsupported``. For admin callers we fall
    through to stdlib ``glob.glob(..., recursive=True)`` which handles
    both shapes — the same relaxation `_resolve_under` gives Read.
    """
    if os.path.isabs(pattern):
        if restricted:
            raise ValueError(
                f"absolute pattern not allowed in restricted mode: {pattern!r}",
            )
        import glob as _glob  # local: stdlib name shadows our module-level Glob
        matches = _glob.glob(pattern, recursive=True)
        return matches[:GLOB_MAX_RESULTS]

    base = Path(cwd)
    results: list[str] = []
    for p in base.rglob(pattern):
        results.append(str(p))
        if len(results) >= GLOB_MAX_RESULTS:
            break
    return results


async def _tool_glob(args: dict, ctx: ToolContext) -> str:
    pattern = args.get("pattern")
    if not pattern:
        raise ValueError("pattern is required")
    results = await asyncio.to_thread(
        _glob_sync, ctx.cwd, pattern, ctx.restricted,
    )
    if not results:
        return f"(no matches for {pattern!r})"
    return "\n".join(results)


# ---------------------------------------------------------------------------
# Grep
# ---------------------------------------------------------------------------
async def _grep_via_rg(cwd: str, pattern: str, glob: str | None) -> str:
    cmd = ["rg", "-n", "--no-heading", "--color=never", pattern]
    if glob:
        cmd.extend(["-g", glob])
    cmd.append(".")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    out = (stdout or b"").decode("utf-8", errors="replace")
    lines = out.splitlines()
    if len(lines) > GREP_MAX_RESULTS:
        lines = lines[:GREP_MAX_RESULTS] + [f"[truncated at {GREP_MAX_RESULTS} matches]"]
    return "\n".join(lines) if lines else "(no matches)"


def _grep_python(cwd: str, pattern: str, glob: str | None) -> str:
    rx = re.compile(pattern)
    results: list[str] = []
    base = Path(cwd)
    iterator = base.rglob(glob) if glob else base.rglob("*")
    for p in iterator:
        if not p.is_file():
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    if rx.search(line):
                        results.append(f"{p}:{lineno}:{line.rstrip()}")
                        if len(results) >= GREP_MAX_RESULTS:
                            return "\n".join(results) + f"\n[truncated at {GREP_MAX_RESULTS}]"
        except OSError:
            continue
    return "\n".join(results) if results else "(no matches)"


async def _tool_grep(args: dict, ctx: ToolContext) -> str:
    pattern = args.get("pattern")
    if not pattern:
        raise ValueError("pattern is required")
    glob = args.get("glob")
    if shutil.which("rg"):
        return await _grep_via_rg(ctx.cwd, pattern, glob)
    return await asyncio.to_thread(_grep_python, ctx.cwd, pattern, glob)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
BUILTIN_TOOLS: dict[str, ToolDef] = {
    "Read": ToolDef(
        name="Read",
        description=(
            "**Tool name is exactly `Read`** (PascalCase, single word — "
            "NOT `read_file`, NOT `read`, NOT `file_read`). "
            "Read a file from disk. UTF-8 text files (.py, .md, .txt, .json, ...) "
            "return their contents as text. Image files (.png, .jpg, .jpeg, .webp, "
            ".gif) are returned as a vision block — you SEE the image and can "
            "describe it directly. Use this when the user asks you to look at "
            "screenshots, scans, or any image already on disk. "
            "Image cap: 5 MB / call; text cap: 1 MB / 2000 lines."
        ),
        input_schema={
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        },
        handler=_tool_read,
    ),
    "Write": ToolDef(
        name="Write",
        description=(
            "**Tool name is exactly `Write`** (PascalCase, single word — "
            "NOT `write_file`, NOT `write`). "
            "Write content to a file under the working directory (0600)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        },
        handler=_tool_write,
    ),
    "Edit": ToolDef(
        name="Edit",
        description=(
            "**Tool name is exactly `Edit`** (PascalCase, single word — "
            "NOT `edit_file`, NOT `edit`). "
            "Replace a substring inside an existing file."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
        handler=_tool_edit,
    ),
    "Bash": ToolDef(
        name="Bash",
        description=(
            "**Tool name is exactly `Bash`** (PascalCase, single word — "
            "NOT `bash`, NOT `shell`). "
            "Run a command via /bin/bash -c with a per-call timeout."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {
                    "type": "number",
                    "description": (
                        f"Optional timeout in SECONDS (default "
                        f"{BASH_DEFAULT_TIMEOUT_S:.0f}, max "
                        f"{BASH_MAX_TIMEOUT_S:.0f}). Values above the max "
                        "are clamped."
                    ),
                },
            },
            "required": ["command"],
        },
        handler=_tool_bash,
    ),
    "Glob": ToolDef(
        name="Glob",
        description=(
            "**Tool name is exactly `Glob`** (PascalCase, single word — "
            "NOT `glob`, NOT `find_files`). "
            "Find files matching a glob pattern. "
            "Relative patterns search under the working directory; "
            "absolute patterns (admin only) search the named path. "
            "Use `**/*.png` etc. for recursive matches."
        ),
        input_schema={
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
        handler=_tool_glob,
    ),
    "Grep": ToolDef(
        name="Grep",
        description=(
            "**Tool name is exactly `Grep`** (PascalCase, single word — "
            "NOT `grep`, NOT `search`). "
            "Search for a regex inside files under the working directory."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "glob": {"type": "string"},
            },
            "required": ["pattern"],
        },
        handler=_tool_grep,
    ),
    "TodoWrite": ToolDef(
        name="TodoWrite",
        description=(
            "**Tool name is exactly `TodoWrite`** (PascalCase, single word — "
            "NOT `todo_write`, NOT `TaskCreate`). "
            "Manage a structured task list for the current agent loop. "
            "Each call REPLACES the entire list (not append). Use proactively "
            "for multi-step work (3+ distinct steps), explicit user todo "
            "requests, or to capture follow-ups discovered mid-task. "
            "Each item: {content (imperative), status (pending|in_progress|"
            "completed), activeForm (present continuous)}. Only ONE item may "
            "be in_progress at a time. Mark complete immediately on finish."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": list(_TODO_STATUSES),
                            },
                            "activeForm": {"type": "string"},
                        },
                        "required": ["content", "status", "activeForm"],
                    },
                },
            },
            "required": ["todos"],
        },
        handler=_tool_todo_write,
    ),
}


# LLM mis-naming compensator. The advertised schema only lists the
# canonical PascalCase names ("Read", "Write", ...), but Sonnet
# occasionally calls them with the snake_case shapes it knows from
# OpenAI function-calling priors ("read_file", "write_file", ...).
# Without this map, those calls hit `KeyError: unknown builtin tool` and
# the model wastes a turn rediscovering the right name. We resolve them
# silently and log so we can spot patterns.
BUILTIN_TOOL_ALIASES = {
    "read":       "Read",
    "read_file":  "Read",
    "file_read":  "Read",
    "write":      "Write",
    "write_file": "Write",
    "file_write": "Write",
    "edit":       "Edit",
    "edit_file":  "Edit",
    "file_edit":  "Edit",
    "bash":       "Bash",
    "shell":      "Bash",
    "glob":       "Glob",
    "grep":       "Grep",
    "todo_write": "TodoWrite",
    "todowrite":  "TodoWrite",
    "TaskCreate": "TodoWrite",
    "task_create": "TodoWrite",
    "TodoList":   "TodoWrite",
}


def builtin_tools_for_api(restricted: bool) -> list[dict]:
    """Return the tools-API schema list for the requested permission level.

    Restricted mode currently advertises NO built-in tools (the policy
    gate would deny them anyway; advertising them just wastes context).
    Aliases (BUILTIN_TOOL_ALIASES) are deliberately NOT exposed here so
    the model only sees one canonical name per tool.
    """
    if restricted:
        return []
    return [
        {
            "name": td.name,
            "description": td.description,
            "input_schema": td.input_schema,
        }
        for td in BUILTIN_TOOLS.values()
    ]


async def execute_builtin_tool(name: str, args: dict, ctx: ToolContext) -> str:
    td = BUILTIN_TOOLS.get(name)
    if td is None:
        canonical = BUILTIN_TOOL_ALIASES.get(name)
        if canonical is not None:
            log.info("tool alias resolved: %s → %s", name, canonical)
            td = BUILTIN_TOOLS.get(canonical)
    if td is None:
        raise KeyError(f"unknown builtin tool: {name}")
    return await td.handler(args, ctx)
