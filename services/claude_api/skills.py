"""Skill 呼び出し — free-code (Claude Code CLI) SkillTool の Python 移植。

~/.claude/skills/<name>/SKILL.md を読み込み、
- 一覧（``- name: description``）を Skill ツールの description に埋め込んで広告する
- 呼び出し時に SKILL.md 本文を ``Base directory for this skill: ...`` 付きで返す

本家との対応:
- 返却フォーマット: tools/SkillTool/SkillTool.ts:1076 /
  skills/loadSkillsDir.ts:346 の
  ``Base directory for this skill: ${baseDir}\\n\\n${markdownContent}`` を踏襲
- 一覧整形: tools/SkillTool/prompt.ts の formatCommandsWithinBudget を簡約移植
  （MAX_LISTING_DESC_CHARS=250 / DEFAULT_CHAR_BUDGET=8000 / MIN_DESC_LENGTH=20
  は本家と同値。bundled 優遇パーティションは bundled skills 未移植のため省略）
- $ARGUMENTS 置換: utils/argumentSubstitution の主要ケース
  （placeholder があれば置換、無ければ末尾に ``ARGUMENTS: <args>`` を追記）
- 未移植: plugin / bundled / managed / MCP skills、frontmatter の
  allowed-tools / model / effort / hooks（読み飛ばす）

本家との構造差分（意図的）:
- 本家は一覧を system-reminder で会話に注入するが、bot にその機構は無いため
  ツール description に載せる。skills の追加・変更は bot 再起動で反映
  （本文は呼び出しごとに mtime チェック付きで再読込するので常に最新）。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

SKILLS_DIR = os.path.expanduser("~/.claude/skills")

# prompt.ts と同値の定数
MAX_LISTING_DESC_CHARS = 250
DEFAULT_CHAR_BUDGET = 8_000
MIN_DESC_LENGTH = 20

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


@dataclass
class Skill:
    name: str
    description: str
    base_dir: str
    body: str


# mtime シグネチャで invalidate される読み込みキャッシュ
_cache: dict = {"sig": None, "skills": {}}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    try:
        import yaml  # bot venv に既存 (mem0 等の依存)

        loaded = yaml.safe_load(m.group(1)) or {}
        if isinstance(loaded, dict):
            meta = {str(k): v for k, v in loaded.items()}
    except Exception:  # noqa: BLE001 — YAML が壊れていても素朴パースで続行
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"')
    return meta, text[m.end():]


def load_skills(force: bool = False) -> dict[str, Skill]:
    """skills dir を走査して {name: Skill} を返す（mtime キャッシュ付き）。"""
    entries: list[tuple[str, str, float]] = []
    try:
        names = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return {}
    for d in names:
        p = os.path.join(SKILLS_DIR, d, "SKILL.md")
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        entries.append((d, p, mt))
    sig = tuple((d, mt) for d, _, mt in entries)
    if not force and _cache["sig"] == sig:
        return _cache["skills"]
    skills: dict[str, Skill] = {}
    for d, p, _ in entries:
        try:
            text = open(p, encoding="utf-8").read()
        except OSError as exc:
            log.warning("skill %s unreadable: %s", d, exc)
            continue
        meta, body = _parse_frontmatter(text)
        name = str(meta.get("name") or d).strip()
        desc = str(meta.get("description") or "").strip()
        if not desc:
            # 本家 extractDescriptionFromMarkdown 相当: 最初の非見出し行
            desc = next(
                (ln.strip() for ln in body.splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")),
                "",
            )
        skills[name] = Skill(
            name=name, description=desc,
            base_dir=os.path.dirname(p), body=body,
        )
    _cache["sig"] = sig
    _cache["skills"] = skills
    return skills


def _cap(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def format_listing() -> str:
    """一覧を ``- name: description`` 形式で（formatCommandsWithinBudget 簡約版）。"""
    skills = list(load_skills().values())
    if not skills:
        return "(no skills installed)"
    entries = [f"- {s.name}: {_cap(s.description, MAX_LISTING_DESC_CHARS)}"
               for s in skills]
    total = sum(len(e) for e in entries) + len(entries) - 1
    if total <= DEFAULT_CHAR_BUDGET:
        return "\n".join(entries)
    n = len(skills)
    name_overhead = sum(len(s.name) + 4 for s in skills) + n - 1
    max_desc = (DEFAULT_CHAR_BUDGET - name_overhead) // n
    if max_desc < MIN_DESC_LENGTH:
        return "\n".join(f"- {s.name}" for s in skills)
    return "\n".join(f"- {s.name}: {_cap(s.description, max_desc)}"
                     for s in skills)


def build_invocation(skill_name: str, args: str = "") -> str:
    """SKILL.md 本文を本家フォーマットで返す（$ARGUMENTS 置換込み）。"""
    skills = load_skills()
    s = skills.get(skill_name)
    if s is None:
        # 大文字小文字ゆらぎの救済（本家の alias 解決に相当する最小限）
        lowered = {k.lower(): v for k, v in skills.items()}
        s = lowered.get(skill_name.lower())
    if s is None:
        avail = ", ".join(sorted(skills)) or "(none)"
        raise ValueError(f"unknown skill: {skill_name!r}. Available: {avail}")
    body = s.body
    if "$ARGUMENTS" in body:
        body = body.replace("$ARGUMENTS", args or "")
    elif args:
        body = f"{body.rstrip()}\n\nARGUMENTS: {args}"
    return f"Base directory for this skill: {s.base_dir}\n\n{body}"
