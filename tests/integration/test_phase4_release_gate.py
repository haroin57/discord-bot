from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import check_release_gate as release


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "phase4@example.invalid")
    _git(repo, "config", "user.name", "phase4")
    return repo


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def test_candidate_diff_and_all_text_conflict_markers_are_checked(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "base.txt").write_text("clean\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "workflow.yml").write_text(
        "<<<<<<< candidate\nvalue: one\n=======\nvalue: two\n>>>>>>> base\n",
        encoding="utf-8",
    )
    (repo / "bad.txt").write_text("trailing whitespace \n", encoding="utf-8")
    candidate = _commit(repo, "candidate")

    release.check_commit_range(base, candidate, repo=repo)
    with pytest.raises(subprocess.CalledProcessError):
        release.check_diff(base, candidate, repo=repo)
    with pytest.raises(RuntimeError, match="conflict markers"):
        release.check_conflict_markers(candidate, repo=repo)


def test_dirty_candidate_checkout_is_rejected(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _commit(repo, "clean")
    release.check_clean_checkout(repo=repo)

    (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="dirty deploy rejected"):
        release.check_clean_checkout(repo=repo)


def test_candidate_repository_must_be_an_explicit_separate_git_root(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "tracked.txt").write_text("candidate\n", encoding="utf-8")
    _commit(repo, "candidate")

    assert release._candidate_repository(repo.resolve()) == repo.resolve()
    with pytest.raises(RuntimeError, match="absolute"):
        release._candidate_repository(Path("repo"))
    with pytest.raises(RuntimeError, match="separate from controller"):
        release._candidate_repository(release.CONTROLLER_ROOT)
    nested = repo / "nested"
    nested.mkdir()
    with pytest.raises(RuntimeError, match="checkout root"):
        release._candidate_repository(nested)


def test_release_manifest_is_externally_pinned_and_checks_required_candidate_paths(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    (repo / "unchanged.txt").write_text("base\n", encoding="utf-8")
    (repo / "deleted.txt").write_text("delete me\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "deleted.txt").unlink()
    files = {}
    for relative in sorted(release.REQUIRED_RELEASE_PATHS):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"release:{relative}\n".encode()
        path.write_bytes(content)
        files[relative] = hashlib.sha256(content).hexdigest()
    changed = repo / "changed.txt"
    changed.write_text("candidate\n", encoding="utf-8")
    files["changed.txt"] = hashlib.sha256(changed.read_bytes()).hexdigest()
    candidate = _commit(repo, "release candidate")
    manifest = tmp_path / "detached-release-manifest.json"
    payload = {
        "schema_version": 1,
        "base_sha": base,
        "candidate_sha": candidate,
        "files": files,
        "deletions": ["deleted.txt"],
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()

    release.check_release_manifest(base, candidate, str(manifest), digest, repo=repo)
    with pytest.raises(RuntimeError, match="external digest pin"):
        release.check_release_manifest(
            base,
            candidate,
            str(manifest),
            "0" * 64,
            repo=repo,
        )

    payload["candidate_sha"] = base
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    stale_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="candidate SHA is stale"):
        release.check_release_manifest(
            base,
            candidate,
            str(manifest),
            stale_digest,
            repo=repo,
        )


def test_release_manifest_requires_changed_hashes_and_exact_deletions(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "deleted.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "deleted.txt").unlink()
    files = {}
    for relative in sorted(release.REQUIRED_RELEASE_PATHS):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    (repo / "unlisted.txt").write_text("changed", encoding="utf-8")
    candidate = _commit(repo, "candidate")
    manifest = tmp_path / "release.json"

    def write_manifest(*, deletions):
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "base_sha": base,
                    "candidate_sha": candidate,
                    "files": files,
                    "deletions": deletions,
                }
            ),
            encoding="utf-8",
        )
        return hashlib.sha256(manifest.read_bytes()).hexdigest()

    with pytest.raises(RuntimeError, match="omits changed candidate paths"):
        release.check_release_manifest(
            base,
            candidate,
            str(manifest),
            write_manifest(deletions=["deleted.txt"]),
            repo=repo,
        )

    files["unlisted.txt"] = hashlib.sha256(
        (repo / "unlisted.txt").read_bytes()
    ).hexdigest()
    with pytest.raises(RuntimeError, match="deletion records"):
        release.check_release_manifest(
            base,
            candidate,
            str(manifest),
            write_manifest(deletions=[]),
            repo=repo,
        )


def test_release_manifest_covers_the_full_reviewed_multi_commit_diff(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repo, "reviewed base")
    intermediate_path = repo / "intermediate.txt"
    intermediate_path.write_text("must remain covered\n", encoding="utf-8")
    _commit(repo, "intermediate candidate commit")

    files = {}
    for relative in sorted(release.REQUIRED_RELEASE_PATHS):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    candidate = _commit(repo, "final candidate commit")
    manifest = tmp_path / "release.json"

    def write_manifest() -> str:
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "base_sha": base,
                    "candidate_sha": candidate,
                    "files": files,
                    "deletions": [],
                }
            ),
            encoding="utf-8",
        )
        return hashlib.sha256(manifest.read_bytes()).hexdigest()

    with pytest.raises(RuntimeError, match="intermediate.txt"):
        release.check_release_manifest(
            base,
            candidate,
            str(manifest),
            write_manifest(),
            repo=repo,
        )

    files["intermediate.txt"] = hashlib.sha256(
        intermediate_path.read_bytes()
    ).hexdigest()
    release.check_release_manifest(
        base,
        candidate,
        str(manifest),
        write_manifest(),
        repo=repo,
    )


def test_protected_evidence_cannot_be_substituted_for_release_manifest():
    with pytest.raises(RuntimeError, match="protected evidence"):
        release._validate_manifest_path(release.PROTECTED_EVIDENCE_RELATIVE)


def test_real_snapshot_and_restore_api_rehearsal():
    release.rehearse_backup_restore()
