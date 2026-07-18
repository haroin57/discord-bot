"""Attachment artifacts with immutable source and trust provenance."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AttachmentProvenance:
    """Identity and trust assigned at the Discord message boundary."""

    sender_id: int | None
    origin: str
    trusted: bool


@dataclass(frozen=True)
class ImageArtifact:
    path: str
    provenance: AttachmentProvenance


@dataclass(frozen=True)
class TextArtifact:
    filename: str
    content: str
    provenance: AttachmentProvenance


@dataclass(frozen=True)
class BinaryArtifact:
    metadata: dict
    provenance: AttachmentProvenance


@dataclass
class AttachmentArtifacts:
    """Downloaded attachment values without discarding their source trust."""

    images: list[ImageArtifact] = field(default_factory=list)
    texts: list[TextArtifact] = field(default_factory=list)
    binaries: list[BinaryArtifact] = field(default_factory=list)
