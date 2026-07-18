"""Deep Research の収集根拠と本文 citation の支持関係。"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .types import ResearchState, SourceRef

_CITATION_RE = re.compile(r"\[(S\d+)\]")
_NUMBER_RE = re.compile(
    r"(?<![\w.])(?P<number>[+\-−]?\d+(?:,\d{3})*(?:\.\d+)?)"
    r"(?:(?P<symbol>\s*[%％])|"
    r"(?P<compact>\s*(?:bps|bp|pp)\b)|"
    r"(?P<word>\s+(?:percentage\s+points?|basis\s+points?|bps|"
    r"percent|per\s+cent|パーセント)\b))?",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z][a-z0-9_-]{2,}")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]*", re.IGNORECASE)
_CJK_RE = re.compile(r"[一-龯ぁ-んァ-ヶー]")
_CITATION_GROUP_RE = re.compile(r"(?:\[S\d+\]\s*)+")
_ENGLISH_CAUSAL_RE = re.compile(
    r"\b(?:causes?|caused|causing|leads?\s+to|led\s+to|results?\s+in|"
    r"resulted\s+in|drives?|drove|triggers?|triggered)\b",
    re.IGNORECASE,
)
_ENGLISH_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|none|neither|without|cannot|"
    r"(?:ai|ca|wo|sha|was|were|has|had|could|should|would|must|might|need|"
    r"did|does|do|is|are|have)n['’]t)\b",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_RE = re.compile(
    r"(?<=[.!?])\s+|[;,；]+|"
    r"\s+(?:and|but|while|whereas|however|before|after|then|"
    r"when|because|although|as)\s+",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?;；])\s*|(?<=\.)\s+")
_LOCAL_CLAIM_PREFIX_RE = re.compile(
    r"^[\s,，;；:：.。!?！？-]*(?:(?:and|but|while|whereas|however|"
    r"when|because|although|as)\b[\s,，;；:：-]*)?",
    re.IGNORECASE,
)
_TREND_PATTERNS = {
    "increase": re.compile(
        r"\b(?:increase[ds]?|increasing|growth|grew|rise[sn]?|rose|rising|gain(?:ed|s)?|"
        r"higher|climb(?:ed|s)?|improv(?:e[ds]?|ing))\b",
        re.IGNORECASE,
    ),
    "decrease": re.compile(
        r"\b(?:decrease[ds]?|decreasing|decline[ds]?|declining|fall(?:en|ing|s)?|fell|"
        r"loss(?:es)?|lower|drop(?:ped|s)?|shrink(?:ing|s)?|worsen(?:ed|s|ing)?)\b",
        re.IGNORECASE,
    ),
}
_CJK_TRENDS = {
    "increase": ("上昇", "増加", "拡大", "成長", "改善", "増え", "伸び"),
    "decrease": ("減少", "低下", "下落", "縮小", "悪化", "減り", "落ち込"),
}
_WORD_CANONICAL = {
    "acquisition": "merger",
    "acquisitions": "merger",
    "approved": "approve",
    "approves": "approve",
    "approving": "approve",
    "approval": "approve",
    "boards": "board",
    "director": "board",
    "directors": "board",
    "defeated": "defeat",
    "defeating": "defeat",
    "defeats": "defeat",
}
_RELATION_WORDS = {
    "acquire",
    "approve",
    "beat",
    "buy",
    "cause",
    "decrease",
    "defeat",
    "drive",
    "exceed",
    "increase",
    "lead",
    "oppose",
    "reject",
    "sell",
    "support",
    "surpass",
    "trigger",
}
_RELATION_CANONICAL = {
    "acquired": "acquire",
    "acquires": "acquire",
    "beat": "defeat",
    "beaten": "defeat",
    "beats": "defeat",
    "bought": "buy",
    "caused": "cause",
    "causes": "cause",
    "decreased": "decrease",
    "decreases": "decrease",
    "drove": "drive",
    "driven": "drive",
    "exceeded": "exceed",
    "exceeds": "exceed",
    "increased": "increase",
    "increases": "increase",
    "led": "lead",
    "leads": "lead",
    "opposed": "oppose",
    "opposes": "oppose",
    "rejected": "reject",
    "rejects": "reject",
    "sold": "sell",
    "supported": "support",
    "supports": "support",
    "surpassed": "surpass",
    "surpasses": "surpass",
    "triggered": "trigger",
    "triggers": "trigger",
}
_ENTITY_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "been",
    "being",
    "had",
    "has",
    "have",
    "her",
    "his",
    "is",
    "its",
    "our",
    "that",
    "the",
    "their",
    "this",
    "was",
    "were",
}
_OBJECT_BOUNDARIES = {
    "after",
    "at",
    "before",
    "because",
    "by",
    "during",
    "for",
    "from",
    "in",
    "on",
    "when",
    "while",
    "with",
}
_STOP_WORDS = {
    "and",
    "are",
    "for",
    "from",
    "has",
    "have",
    "into",
    "that",
    "the",
    "this",
    "was",
    "were",
    "with",
    "について",
    "による",
    "として",
    "ため",
    "こと",
    "もの",
}


@dataclass(frozen=True)
class CitationSupport:
    sid: str
    claim: str
    supported: bool
    reason: str


@dataclass(frozen=True)
class _RelationFrame:
    subject: str
    relation: str
    object: str
    asserted: bool = True


class EvidenceRegistry:
    """``ResearchState`` を保存先にする run-scoped evidence registry。"""

    def __init__(self, state: ResearchState) -> None:
        self.state = state

    def register(
        self,
        *,
        url: str,
        title: str = "",
        source_type: str = "web",
        snippet: str = "",
    ) -> SourceRef | None:
        ref = self.state.register_source(
            url=url,
            title=title,
            source_type=source_type,
            snippet=snippet,
        )
        if ref is not None:
            evidence = " ".join(
                part.strip() for part in (title, snippet) if part and part.strip()
            )
            if evidence and evidence not in ref.evidence:
                ref.evidence.append(evidence[:2000])
        return ref

    def verify(self, report: str) -> list[CitationSupport]:
        by_sid = {ref.sid: ref for ref in self.state.sources.values()}
        checks: list[CitationSupport] = []
        for unit in _claim_units(report):
            claim = _strip_claim(_CITATION_RE.sub("", unit))
            for sid in dict.fromkeys(_CITATION_RE.findall(unit)):
                ref = by_sid.get(sid)
                if ref is None:
                    continue
                supported, reason = _supports(claim, ref)
                checks.append(CitationSupport(sid, claim, supported, reason))
        return checks


def _claim_units(report: str) -> list[str]:
    """Bind each citation group to the immediately preceding local claim."""
    units: list[str] = []
    previous_claim = ""
    for line in (report or "").splitlines():
        floor = 0
        for citations in _CITATION_GROUP_RE.finditer(line):
            fragment = line[floor : citations.start()]
            local_claim = _local_claim(fragment) or previous_claim
            if local_claim:
                units.append(f"{local_claim} {citations.group().strip()}")
                previous_claim = local_claim
            floor = citations.end()
        trailing_claim = _local_claim(line[floor:])
        if trailing_claim:
            previous_claim = trailing_claim
    return units


def _local_claim(fragment: str) -> str:
    cleaned = _CITATION_RE.sub("", fragment or "")
    candidates = [
        _strip_claim(_LOCAL_CLAIM_PREFIX_RE.sub("", part))
        for part in _SENTENCE_SPLIT_RE.split(cleaned)
    ]
    return next((candidate for candidate in reversed(candidates) if candidate), "")


def _strip_claim(value: str) -> str:
    return (value or "").strip(" \t-–—:：,、.;；。!！?？")


def _number_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for match in _NUMBER_RE.finditer((value or "").replace("−", "-")):
        number = match.group("number")
        normalized = number.replace(",", "")
        if normalized.startswith("+"):
            normalized = normalized[1:]
        unit = (
            (
                match.group("symbol")
                or match.group("compact")
                or match.group("word")
                or ""
            )
            .strip()
            .lower()
        )
        if unit in {"%", "％", "percent", "per cent", "パーセント"}:
            normalized += "%"
        elif unit == "pp" or unit.startswith("percentage point"):
            normalized += "pp"
        elif unit in {"bp", "bps"} or unit.startswith("basis point"):
            normalized += "bp"
        tokens.add(normalized)
    return tokens


def _trend_directions(value: str) -> set[str]:
    directions = {
        direction
        for direction, pattern in _TREND_PATTERNS.items()
        if pattern.search(value or "")
    }
    for direction, terms in _CJK_TRENDS.items():
        if any(term in (value or "") for term in terms):
            directions.add(direction)
    return directions


def _has_negation(value: str) -> bool:
    text = value or ""
    return bool(_ENGLISH_NEGATION_RE.search(text)) or any(
        term in text
        for term in (
            "ではない",
            "ではなかった",
            "しない",
            "しなかった",
            "ない",
            "なかった",
            "ず",
            "ぬ",
        )
    )


def _relevant_evidence_polarities(claim: str, evidence: str) -> set[bool]:
    """Return polarities of independently scoped evidence matching a claim."""
    polarities: set[bool] = set()
    for quoted_part in re.split(r'["“”]', evidence or ""):
        for clause in _CLAUSE_SPLIT_RE.split(quoted_part):
            if clause.strip() and _substantial_overlap(claim, clause):
                polarities.add(_has_negation(clause))
    return polarities


def _causal_pair(value: str) -> tuple[str, str] | None:
    text = _CITATION_RE.sub("", value or "")
    match = _ENGLISH_CAUSAL_RE.search(text)
    if match is not None:
        left, right = text[: match.start()].strip(), text[match.end() :].strip()
        return (left, right) if left and right else None
    for marker in ("によって", "により", "が原因で", "のために", "のため"):
        if marker in text:
            left, right = text.split(marker, 1)
            left, right = left.strip(), right.strip()
            return (left, right) if left and right else None
    return None


def _side_matches(left: str, right: str) -> bool:
    left_norm = _normalized(left)
    right_norm = _normalized(right)
    if (
        len(left_norm) >= 3
        and len(right_norm) >= 3
        and (left_norm in right_norm or right_norm in left_norm)
    ):
        return True
    left_words = set(_semantic_words(left))
    right_words = set(_semantic_words(right))
    if left_words and right_words:
        overlap = left_words & right_words
        if len(overlap) / min(len(left_words), len(right_words)) >= 0.6:
            return True
    left_bigrams = _cjk_bigrams(left)
    right_bigrams = _cjk_bigrams(right)
    return bool(left_bigrams and right_bigrams) and (
        len(left_bigrams & right_bigrams) / min(len(left_bigrams), len(right_bigrams))
        >= 0.6
    )


def _substantial_overlap(claim: str, evidence: str) -> bool:
    claim_words = {word for word in _semantic_words(claim) if word not in _STOP_WORDS}
    evidence_words = set(_semantic_words(evidence))
    overlap = claim_words & evidence_words
    if claim_words and (len(overlap) >= 3 or len(overlap) / len(claim_words) >= 0.6):
        return True
    claim_bigrams = _cjk_bigrams(claim)
    evidence_bigrams = _cjk_bigrams(evidence)
    cjk_overlap = claim_bigrams & evidence_bigrams
    return (
        bool(claim_bigrams)
        and len(cjk_overlap) >= 3
        and (len(cjk_overlap) / len(claim_bigrams) >= 0.35)
    )


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-z一-龯ぁ-んァ-ヶー]+", "", (value or "").lower())


def _normalize_approval_phrases(value: str) -> str:
    """限定的な nominalization を単純な能動態へ戻す。"""

    text = value or ""
    give = r"(?:gave|give|gives|given|grant|grants|granted)"
    text = re.sub(
        rf"\b{give}\s+(?:(?:their|its|the)\s+)?approval\s+(?:to|for)\s+",
        "approved ",
        text,
        flags=re.IGNORECASE,
    )

    def replace_object_approval(match: re.Match) -> str:
        return f"approved {match.group('object')}"

    return re.sub(
        rf"\b{give}\s+(?P<object>.+?)\s+"
        r"(?:(?:their|its|the)\s+)?approval\b",
        replace_object_approval,
        text,
        flags=re.IGNORECASE,
    )


def _canonical_word(word: str) -> str:
    lowered = (word or "").lower()
    return _WORD_CANONICAL.get(lowered, lowered)


def _semantic_words(value: str) -> list[str]:
    return [
        _canonical_word(word)
        for word in _TOKEN_RE.findall(_normalize_approval_phrases(value))
    ]


def _relation_lemma(word: str) -> str | None:
    lowered = (word or "").lower()
    canonical = _canonical_word(lowered)
    if canonical in _RELATION_WORDS:
        return canonical
    mapped = _RELATION_CANONICAL.get(lowered)
    if mapped is not None:
        return mapped
    return None


def _entity_text(words: list[str]) -> str:
    return " ".join(
        _canonical_word(word)
        for word in words
        if word.lower() not in _ENTITY_STOP_WORDS
        and word.lower() not in _ATTITUDE_ENTITY_WORDS
    )


_ATTITUDE_NOUNS = {
    "allegation",
    "allegations",
    "assertion",
    "assertions",
    "claim",
    "claims",
    "report",
    "reports",
    "rumor",
    "rumors",
    "statement",
    "statements",
    "suggestion",
    "suggestions",
}
_SPEECH_ATTITUDES = {
    "allege",
    "alleged",
    "alleges",
    "assert",
    "asserted",
    "asserts",
    "claim",
    "claimed",
    "claims",
    "report",
    "reported",
    "reports",
    "say",
    "said",
    "says",
    "state",
    "stated",
    "states",
    "suggest",
    "suggested",
    "suggests",
}
_REJECTED_ATTITUDES = {
    "contested",
    "contradicted",
    "debunked",
    "denied",
    "denies",
    "dismissed",
    "disputed",
    "false",
    "incorrect",
    "refuted",
    "retracted",
    "reject",
    "rejected",
    "rejects",
    "unfounded",
    "unsubstantiated",
    "untrue",
    "withdrawn",
}
_HEDGE_ATTITUDES = {
    "alleged",
    "allegedly",
    "apparent",
    "apparently",
    "appeared",
    "appears",
    "believed",
    "expected",
    "likely",
    "ostensibly",
    "perhaps",
    "possible",
    "possibly",
    "probable",
    "predicted",
    "purportedly",
    "reportedly",
    "seem",
    "seemed",
    "seems",
    "suspected",
    "unlikely",
    "unconfirmed",
    "unproven",
    "unverified",
}
_MODAL_ATTITUDES = {
    "able",
    "can",
    "could",
    "may",
    "might",
    "must",
    "need",
    "needed",
    "needs",
    "ought",
    "shall",
    "should",
    "will",
    "would",
}
_CONDITIONAL_ATTITUDES = {
    "assuming",
    "condition",
    "conditions",
    "conditional",
    "conditionally",
    "contingent",
    "depending",
    "if",
    "provided",
    "providing",
    "suppose",
    "supposing",
    "unless",
    "whether",
}
_ASSERTED_ATTITUDES = {
    "confirm",
    "confirmed",
    "confirms",
    "established",
    "found",
    "prove",
    "proved",
    "proven",
    "verified",
    "verifies",
}
_ATTITUDE_ENTITY_WORDS = (
    _ATTITUDE_NOUNS
    | _SPEECH_ATTITUDES
    | _REJECTED_ATTITUDES
    | _HEDGE_ATTITUDES
    | _MODAL_ATTITUDES
    | _CONDITIONAL_ATTITUDES
    | _ASSERTED_ATTITUDES
    | {"he", "she", "they"}
)


def _relation_is_asserted(words: list[str], relation_index: int) -> bool:
    before = words[:relation_index]
    wrapper = before
    if "that" in before:
        wrapper = before[: len(before) - 1 - before[::-1].index("that")]
    has_attitude = bool(
        set(wrapper)
        & (
            _ATTITUDE_NOUNS
            | _SPEECH_ATTITUDES
            | _REJECTED_ATTITUDES
            | _HEDGE_ATTITUDES
            | _MODAL_ATTITUDES
            | _CONDITIONAL_ATTITUDES
            | _ASSERTED_ATTITUDES
        )
    )
    if not has_attitude:
        return True
    all_words = set(words)
    if all_words & (
        _REJECTED_ATTITUDES
        | _HEDGE_ATTITUDES
        | _MODAL_ATTITUDES
        | _CONDITIONAL_ATTITUDES
    ):
        return False
    if all_words & _ASSERTED_ATTITUDES and _has_negation(" ".join(words)):
        return False
    if all_words & _ASSERTED_ATTITUDES:
        return True
    # An unqualified allegation/report does not assert its embedded relation.
    return False


def _is_attitude_scope(value: str) -> bool:
    words = [word.lower() for word in _TOKEN_RE.findall(value)]
    word_set = set(words)
    markers = (
        _ATTITUDE_NOUNS
        | _SPEECH_ATTITUDES
        | _REJECTED_ATTITUDES
        | _ASSERTED_ATTITUDES
        | _HEDGE_ATTITUDES
        | _MODAL_ATTITUDES
        | _CONDITIONAL_ATTITUDES
    )
    if not word_set & markers:
        return False
    substantive_relations = {
        relation
        for word in words
        if (relation := _relation_lemma(word)) is not None and relation != "reject"
    }
    return not substantive_relations


def _scope_is_unasserted(value: str) -> bool:
    words = {word.lower() for word in _TOKEN_RE.findall(value)}
    if words & (_HEDGE_ATTITUDES | _MODAL_ATTITUDES | _CONDITIONAL_ATTITUDES):
        return True
    if words & _ASSERTED_ATTITUDES and _has_negation(value):
        return True
    if words & _REJECTED_ATTITUDES and words & (
        _ATTITUDE_NOUNS | _SPEECH_ATTITUDES | _REJECTED_ATTITUDES
    ):
        return True
    if words & _SPEECH_ATTITUDES and not words & _ASSERTED_ATTITUDES:
        return True
    return False


def _relation_frames_from_clause(
    value: str,
    inherited_subject: str,
) -> tuple[list[_RelationFrame], str]:
    words = _TOKEN_RE.findall(value)
    lowered = [word.lower() for word in words]
    relation_indexes = [
        index for index, word in enumerate(lowered) if _relation_lemma(word) is not None
    ]
    frames: list[_RelationFrame] = []
    current_subject = inherited_subject
    for relation_offset, index in enumerate(relation_indexes):
        relation = _relation_lemma(lowered[index])
        assert relation is not None
        previous_index = (
            relation_indexes[relation_offset - 1] if relation_offset else -1
        )
        next_index = (
            relation_indexes[relation_offset + 1]
            if relation_offset + 1 < len(relation_indexes)
            else len(lowered)
        )
        prefix = (
            lowered[:index]
            if relation_offset == 0
            else lowered[previous_index + 1 : index]
        )
        prefix = [word for word in prefix if word not in {"and", "or", "then"}]
        tail = lowered[index + 1 : next_index]
        tail = [word for word in tail if word not in {"and", "or", "then"}]
        if not _entity_text(tail) and relation_offset + 1 < len(relation_indexes):
            tail = lowered[relation_indexes[-1] + 1 :]
        passive_auxiliaries = {"is", "was", "were", "been", "being"}
        passive_scope_start = (
            len(prefix) - prefix[::-1].index("that") if "that" in prefix else 0
        )
        auxiliary_indexes = [
            position
            for position, token in enumerate(prefix)
            if position >= passive_scope_start and token in passive_auxiliaries
        ]
        passive = bool(auxiliary_indexes)
        if passive:
            auxiliary_index = auxiliary_indexes[-1]
            patient = _entity_text(prefix[:auxiliary_index]) or current_subject
            if not patient:
                frames.append(
                    _RelationFrame(
                        "",
                        relation,
                        "",
                        _relation_is_asserted(lowered, index),
                    )
                )
                continue
            if "by" in tail:
                by_index = tail.index("by")
                agent_words = tail[by_index + 1 :]
                agent = _entity_text(
                    agent_words[
                        : next(
                            (
                                i
                                for i, token in enumerate(agent_words)
                                if token in _OBJECT_BOUNDARIES
                            ),
                            len(agent_words),
                        )
                    ]
                )
                if agent and patient:
                    frames.append(
                        _RelationFrame(
                            agent,
                            relation,
                            patient,
                            _relation_is_asserted(lowered, index),
                        )
                    )
                    current_subject = agent
                    continue
            # Passive evidence without an explicit agent cannot establish the
            # subject required by an active directional claim.
            frames.append(
                _RelationFrame(
                    "",
                    relation,
                    patient,
                    _relation_is_asserted(lowered, index),
                )
            )
            continue

        subject_prefix = prefix
        outer_subject = ""
        if "that" in prefix:
            that_index = len(prefix) - 1 - prefix[::-1].index("that")
            outer_subject = _entity_text(prefix[:that_index])
            subject_prefix = prefix[that_index + 1 :]
        subject = _entity_text(subject_prefix) or outer_subject or current_subject
        if not subject:
            continue
        current_subject = subject
        asserted = _relation_is_asserted(lowered, index)
        if not asserted:
            for tail_index, token in enumerate(tail):
                if (
                    token in {"is", "was", "were"}
                    and set(tail[tail_index + 1 :]) & _REJECTED_ATTITUDES
                ):
                    tail = tail[:tail_index]
                    break
        boundary = next(
            (i for i, token in enumerate(tail) if token in _OBJECT_BOUNDARIES),
            len(tail),
        )
        object_text = _entity_text(tail[:boundary])
        if object_text:
            frames.append(
                _RelationFrame(
                    subject,
                    relation,
                    object_text,
                    asserted,
                )
            )
    return frames, current_subject


def _coordinated_relation_frames(value: str) -> list[_RelationFrame]:
    """Extract relations sharing a subject and object (``A beat and passed B``)."""
    words = [word.lower() for word in _TOKEN_RE.findall(value)]
    frames: list[_RelationFrame] = []
    for index in range(len(words) - 3):
        first_relation = _relation_lemma(words[index])
        if first_relation is None or words[index + 1] not in {"and", "or"}:
            continue
        second_relation = _relation_lemma(words[index + 2])
        if second_relation is None:
            continue
        subject = _entity_text(words[:index])
        tail = words[index + 3 :]
        boundary = next(
            (i for i, token in enumerate(tail) if token in _OBJECT_BOUNDARIES),
            len(tail),
        )
        object_text = _entity_text(tail[:boundary])
        if not subject or not object_text:
            continue
        for relation, relation_index in (
            (first_relation, index),
            (second_relation, index + 2),
        ):
            frames.append(
                _RelationFrame(
                    subject,
                    relation,
                    object_text,
                    _relation_is_asserted(words, relation_index),
                )
            )
    return frames


def _relation_frames(value: str) -> list[_RelationFrame]:
    """Extract every relation frame across sentences and explicit clauses."""
    normalized = _normalize_approval_phrases(value)
    frames = _coordinated_relation_frames(normalized)
    if _scope_is_unasserted(normalized):
        frames = [replace(frame, asserted=False) for frame in frames]
    inherited_subject = ""
    clauses = [
        clause for clause in _CLAUSE_SPLIT_RE.split(normalized) if clause.strip()
    ]
    for index, clause in enumerate(clauses):
        if not clause.strip():
            continue
        clause_frames, inherited_subject = _relation_frames_from_clause(
            clause,
            inherited_subject,
        )
        context = [clause]
        if index > 0 and _is_attitude_scope(clauses[index - 1]):
            context.insert(0, clauses[index - 1])
        if index + 1 < len(clauses) and _is_attitude_scope(clauses[index + 1]):
            context.append(clauses[index + 1])
        if _scope_is_unasserted(" ".join(context)):
            clause_frames = [replace(frame, asserted=False) for frame in clause_frames]
        frames.extend(clause_frames)
    return list(dict.fromkeys(frames))


def _relation_alignment(
    claim: str,
    evidence: str,
) -> tuple[bool, str] | None:
    claim_frames = _relation_frames(claim)
    evidence_frames = _relation_frames(evidence)
    if not claim_frames or not evidence_frames:
        return None

    for claim_frame in claim_frames:
        forward = any(
            claim_frame.relation == evidence_frame.relation
            and evidence_frame.asserted
            and _side_matches(claim_frame.subject, evidence_frame.subject)
            and _side_matches(claim_frame.object, evidence_frame.object)
            for evidence_frame in evidence_frames
        )
        reverse = any(
            claim_frame.relation == evidence_frame.relation
            and evidence_frame.asserted
            and _side_matches(claim_frame.subject, evidence_frame.object)
            and _side_matches(claim_frame.object, evidence_frame.subject)
            for evidence_frame in evidence_frames
        )
        if forward and reverse:
            return False, "relation-direction-ambiguous"

    unmatched: list[_RelationFrame] = []
    for claim_frame in claim_frames:
        if any(
            claim_frame.relation == evidence_frame.relation
            and evidence_frame.asserted
            and _side_matches(claim_frame.subject, evidence_frame.subject)
            and _side_matches(claim_frame.object, evidence_frame.object)
            for evidence_frame in evidence_frames
        ):
            continue
        unmatched.append(claim_frame)
    if not unmatched:
        return True, "relation-match"

    for claim_frame in unmatched:
        if any(
            claim_frame.relation == evidence_frame.relation
            and not evidence_frame.subject
            and _side_matches(claim_frame.object, evidence_frame.object)
            for evidence_frame in evidence_frames
        ):
            return False, "relation-agent-missing"
        if any(
            claim_frame.relation == evidence_frame.relation
            and not evidence_frame.asserted
            and _side_matches(claim_frame.subject, evidence_frame.subject)
            and _side_matches(claim_frame.object, evidence_frame.object)
            for evidence_frame in evidence_frames
        ):
            return False, "relation-attitude-unsupported"
        for evidence_frame in evidence_frames:
            if not evidence_frame.asserted:
                continue
            same_relation = claim_frame.relation == evidence_frame.relation
            same_object = _side_matches(claim_frame.object, evidence_frame.object)
            if same_relation and not evidence_frame.subject and same_object:
                return False, "relation-agent-missing"
            reverse_subject = _side_matches(
                claim_frame.subject,
                evidence_frame.object,
            )
            reverse_object = _side_matches(
                claim_frame.object,
                evidence_frame.subject,
            )
            same_subject = _side_matches(
                claim_frame.subject,
                evidence_frame.subject,
            )
            if same_relation and reverse_subject and reverse_object:
                return False, "relation-direction-mismatch"
            if same_subject and same_object:
                return False, "relation-mismatch"
    if any(
        claim_frame.relation == evidence_frame.relation
        for claim_frame in unmatched
        for evidence_frame in evidence_frames
    ):
        return False, "relation-arguments-mismatch"
    return False, "relation-mismatch"


def _cjk_bigrams(value: str) -> set[str]:
    chars = "".join(_CJK_RE.findall(value))
    return {chars[i : i + 2] for i in range(max(0, len(chars) - 1))}


def _supports(claim: str, ref: SourceRef) -> tuple[bool, str]:
    title_evidence = (
        ref.title if not ref.title.lower().startswith(("http://", "https://")) else ""
    )
    evidence_parts = [title_evidence, ref.snippet, *ref.evidence]
    evidence = " ".join(
        dict.fromkeys(p.strip() for p in evidence_parts if p and p.strip())
    )
    claim_norm = _normalized(claim)
    evidence_norm = _normalized(evidence)
    if len(claim_norm) < 3:
        return False, "claim-too-short"
    if len(evidence_norm) < 8:
        return False, "evidence-unavailable"

    claim_cause = _causal_pair(claim)
    evidence_cause = _causal_pair(evidence)
    if (
        claim_cause
        and evidence_cause
        and (
            _side_matches(claim_cause[0], evidence_cause[1])
            and _side_matches(claim_cause[1], evidence_cause[0])
            and not (
                _side_matches(claim_cause[0], evidence_cause[0])
                and _side_matches(claim_cause[1], evidence_cause[1])
            )
        )
    ):
        return False, "causal-direction-mismatch"

    claim_trends = _trend_directions(claim)
    evidence_trends = _trend_directions(evidence)
    if claim_trends and evidence_trends and claim_trends.isdisjoint(evidence_trends):
        return False, "direction-mismatch"

    if len(_relevant_evidence_polarities(claim, evidence)) > 1:
        return False, "polarity-ambiguous"

    relation_alignment = _relation_alignment(claim, evidence)
    if relation_alignment == (False, "relation-attitude-unsupported"):
        return relation_alignment

    if _has_negation(claim) != _has_negation(evidence) and _substantial_overlap(
        claim, evidence
    ):
        return False, "polarity-mismatch"

    claim_numbers = _number_tokens(claim)
    evidence_numbers = _number_tokens(evidence)
    if claim_numbers and not claim_numbers.issubset(evidence_numbers):
        return False, "numeric-mismatch"

    if relation_alignment is not None:
        return relation_alignment

    if len(claim_norm) >= 10 and claim_norm in evidence_norm:
        return True, "exact-claim"

    if _substantial_overlap(claim, evidence):
        claim_words = {
            word for word in _semantic_words(claim) if word not in _STOP_WORDS
        }
        evidence_words = set(_semantic_words(evidence))
        if claim_words & evidence_words:
            return True, "lexical-overlap"
        if _cjk_bigrams(claim) & _cjk_bigrams(evidence):
            return True, "cjk-overlap"

    return False, "insufficient-overlap"


def verify_citation_support(report: str, state: ResearchState) -> list[CitationSupport]:
    return EvidenceRegistry(state).verify(report)
