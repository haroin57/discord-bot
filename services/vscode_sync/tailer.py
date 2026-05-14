"""JSONL file tailer for ~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


@dataclass
class TailedLine:
    path: Path
    raw: dict


class JSONLTailer:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._offsets: dict[Path, int] = {}
        self._inodes: dict[Path, int] = {}

    def prime(self) -> None:
        for path in self._scan_files():
            try:
                st = path.stat()
            except OSError:
                continue
            self._offsets[path] = st.st_size
            self._inodes[path] = st.st_ino

    def _scan_files(self) -> Iterable[Path]:
        if not self.root.exists():
            return []
        try:
            return list(self.root.glob("*/*.jsonl"))
        except OSError as e:
            log.warning("vscode_sync: glob failed on %s: %s", self.root, e)
            return []

    def poll(self) -> list[TailedLine]:
        out: list[TailedLine] = []
        for path in self._scan_files():
            try:
                st = path.stat()
            except OSError:
                continue

            inode = st.st_ino
            size = st.st_size
            prev_inode = self._inodes.get(path)
            offset = self._offsets.get(path, 0)

            if prev_inode is not None and prev_inode != inode:
                offset = 0
            if size < offset:
                offset = 0

            if size == offset:
                self._inodes[path] = inode
                continue

            try:
                with open(path, "rb") as f:
                    f.seek(offset)
                    chunk = f.read(size - offset)
            except OSError as e:
                log.warning("vscode_sync: read failed on %s: %s", path, e)
                continue

            try:
                text = chunk.decode("utf-8")
                consumed = len(chunk)
            except UnicodeDecodeError as e:
                text = chunk[: e.start].decode("utf-8", errors="replace")
                consumed = e.start

            last_nl = text.rfind("\n")
            if last_nl < 0:
                self._inodes[path] = inode
                continue
            complete = text[: last_nl + 1]
            new_offset = offset + len(complete.encode("utf-8"))
            if new_offset > offset + consumed:
                new_offset = offset + consumed
            self._offsets[path] = new_offset
            self._inodes[path] = inode

            for line in complete.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except (json.JSONDecodeError, ValueError) as e:
                    log.warning(
                        "vscode_sync: bad JSON in %s: %s (line=%r)",
                        path.name, e, line[:200],
                    )
                    continue
                out.append(TailedLine(path=path, raw=raw))
        return out

    def reset_offset(self, path: Path) -> None:
        self._offsets.pop(path, None)
        self._inodes.pop(path, None)
