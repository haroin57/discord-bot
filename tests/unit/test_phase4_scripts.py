from __future__ import annotations

import json
import hashlib
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import tests.conftest as suite_config
from scripts import check_release_gate as release
from scripts import check_test_isolation as isolation
from scripts import run_test_lane as lanes

_CANDIDATE_SHA = "a" * 40


def _operations(tmp_path: Path, source: str) -> set[str]:
    path = tmp_path / "test_probe.py"
    path.write_text(source, encoding="utf-8")
    return {finding.operation for finding in isolation.inspect_file(path)}


@pytest.mark.parametrize(
    ("source", "operation"),
    [
        ('open("polluted", "w")\n', "call to builtins.open"),
        (
            'import os as operating\noperating.environ.update({"X": "1"})\n',
            "call to os.environ.update",
        ),
        ("from os import chdir as cd\ncd('/tmp')\n", "call to os.chdir"),
        (
            "from pathlib import Path as P\nP('polluted').write_text('x')\n",
            "call to pathlib.Path.write_text",
        ),
        (
            'from os import environ as env\nenv["X"] = "1"\n',
            "mutation of os.environ",
        ),
        (
            "import sys\nmodules = sys.modules\nmodules.__setitem__('x', object())\n",
            "call to sys.modules.__setitem__",
        ),
        (
            "import os\ndef pollute():\n    os.environ.clear()\npollute()\n",
            "call to os.environ.clear",
        ),
        (
            "import os\ndef pollute():\n    os.environ.clear()\nrun = pollute\nrun()\n",
            "call to os.environ.clear",
        ),
        (
            "import subprocess as sp\nsp.run(['true'])\n",
            "call to subprocess.run",
        ),
        (
            "from subprocess import Popen as launch\nlaunch(['true'])\n",
            "call to subprocess.Popen",
        ),
        ("import os\nos.system('true')\n", "call to os.system"),
        ("import os\nos.execvp('true', ['true'])\n", "call to os.execvp"),
        (
            "import shutil\nshutil.copy('from', 'to')\n",
            "call to shutil.copy",
        ),
        (
            "import tempfile as tf\ntf.TemporaryDirectory()\n",
            "call to tempfile.TemporaryDirectory",
        ),
        (
            "from pathlib import Path\np = Path('x')\np.open('w')\n",
            "call to pathlib.Path.open",
        ),
    ],
)
def test_isolation_checker_recurses_and_resolves_aliases(
    tmp_path: Path, source: str, operation: str
):
    assert operation in _operations(tmp_path, source)


def test_isolation_checker_ignores_uncalled_function_body(tmp_path: Path):
    operations = _operations(
        tmp_path,
        "import os\ndef scoped_fixture():\n    os.environ.update({'X': '1'})\n",
    )
    assert operations == set()


@pytest.mark.parametrize(
    "source",
    [
        """
import subprocess
class Runner:
    def start(self):
        self.launch()
    def launch(self):
        subprocess.run(["true"])
Runner().start()
""",
        """
import subprocess
class Runner:
    @classmethod
    def start(cls):
        cls.launch()
    @classmethod
    def launch(cls):
        subprocess.run(["true"])
Runner.start()
""",
        """
import subprocess
class Runner:
    def start(self):
        subprocess.run(["true"])
runner = Runner()
runner.start()
""",
        """
import subprocess
class Base:
    def __init__(self):
        subprocess.run(["true"])
class Runner(Base):
    pass
Runner()
""",
        """
import subprocess
def outer():
    def helper():
        subprocess.run(["true"])
    helper()
outer()
""",
        """
import subprocess
def first():
    second()
def second():
    first()
    subprocess.run(["true"])
first()
""",
    ],
)
def test_isolation_checker_follows_methods_inheritance_nested_helpers_and_cycles(
    tmp_path: Path,
    source: str,
):
    assert "call to subprocess.run" in _operations(tmp_path, source)


@pytest.mark.parametrize(
    "source",
    [
        """
import subprocess
class Base:
    def start(self):
        self.launch()
    def launch(self):
        pass
class Child(Base):
    def launch(self):
        subprocess.run(["true"])
Child().start()
""",
        """
import subprocess
class Root:
    def start(self):
        self.launch()
    def launch(self):
        pass
class Left(Root):
    pass
class Right(Root):
    def launch(self):
        subprocess.run(["true"])
class Child(Left, Right):
    pass
Child().start()
""",
        """
import subprocess
class Base:
    @classmethod
    def start(cls):
        cls.launch()
    @classmethod
    def launch(cls):
        pass
class Child(Base):
    @classmethod
    def launch(cls):
        subprocess.run(["true"])
Child.start()
""",
        """
import subprocess
def build():
    class Base:
        def start(self):
            self.launch()
        def launch(self):
            pass
    class Child(Base):
        def launch(self):
            subprocess.run(["true"])
    Child().start()
build()
""",
    ],
)
def test_isolation_checker_uses_effective_receiver_and_lexical_nested_bases(
    tmp_path: Path,
    source: str,
):
    assert "call to subprocess.run" in _operations(tmp_path, source)


def test_isolation_checker_conservatively_searches_overrides_for_unknown_receiver(
    tmp_path: Path,
):
    source = """
import subprocess
class Base:
    def start(self):
        self.launch()
    def launch(self):
        pass
class Child(Base):
    def launch(self):
        subprocess.run(["true"])
start = Base.start
start(object())
"""
    assert "call to subprocess.run" in _operations(tmp_path, source)


@pytest.mark.parametrize(
    "source",
    [
        """
import subprocess
class Base:
    def start(self):
        launch = self.launch
        launch()
    def launch(self):
        pass
class Child(Base):
    def launch(self):
        subprocess.run(["true"])
Child().start()
""",
        """
import subprocess
class Base:
    @classmethod
    def start(cls):
        launch = cls.launch
        launch()
    @classmethod
    def launch(cls):
        pass
class Child(Base):
    @classmethod
    def launch(cls):
        subprocess.run(["true"])
Child.start()
""",
        """
import subprocess
def build():
    class Base:
        def start(self):
            launch = self.launch
            nested_alias = launch
            nested_alias()
        def launch(self):
            pass
    class Child(Base):
        def launch(self):
            subprocess.run(["true"])
    Child().start()
build()
""",
        """
import subprocess
class Base:
    def start(self):
        launch = self.launch
        launch()
    def launch(self):
        pass
class Child(Base):
    def launch(self):
        subprocess.run(["true"])
start = Base.start
start(object())
""",
    ],
)
def test_isolation_checker_preserves_bound_method_alias_dispatch(
    tmp_path: Path,
    source: str,
):
    assert "call to subprocess.run" in _operations(tmp_path, source)


def test_isolation_checker_bound_alias_uses_only_c3_selected_override(
    tmp_path: Path,
):
    source = """
import subprocess
class Root:
    def start(self):
        launch = self.launch
        launch()
    def launch(self):
        pass
class Left(Root):
    def launch(self):
        pass
class Right(Root):
    def launch(self):
        subprocess.run(["true"])
class Child(Left, Right):
    pass
Child().start()
"""
    assert "call to subprocess.run" not in _operations(tmp_path, source)


@pytest.mark.parametrize(
    "source",
    [
        """
import subprocess
class Base:
    def start(self):
        launch = self.launch
        def invoke():
            launch()
        invoke()
    def launch(self):
        pass
class Child(Base):
    def launch(self):
        subprocess.run(["true"])
Child().start()
""",
        """
import subprocess
class Base:
    @classmethod
    def start(cls):
        launch = cls.launch
        def invoke():
            launch()
        invoke()
    @classmethod
    def launch(cls):
        pass
class Child(Base):
    @classmethod
    def launch(cls):
        subprocess.run(["true"])
Child.start()
""",
        """
import subprocess
class Base:
    def start(self):
        launch = self.launch
        alias = launch
        def invoke():
            nested_alias = alias
            nested_alias()
        invoke()
    def launch(self):
        pass
class Child(Base):
    def launch(self):
        subprocess.run(["true"])
Child().start()
""",
        """
import subprocess
class Runner:
    def start(self, flag=True):
        if flag:
            launch = self.danger
        else:
            launch = self.safe
        def invoke():
            launch()
        invoke()
    def safe(self):
        pass
    def danger(self):
        subprocess.run(["true"])
Runner().start()
""",
    ],
)
def test_isolation_checker_carries_bound_aliases_into_nested_closures(
    tmp_path: Path,
    source: str,
):
    assert "call to subprocess.run" in _operations(tmp_path, source)


@pytest.mark.parametrize(
    "branch",
    [
        """
        if flag:
            launch = self.danger
        else:
            launch = self.safe
""",
        """
        try:
            launch = self.danger
        except Exception:
            launch = self.safe
""",
        """
        try:
            probe()
        except Exception:
            launch = self.danger
        else:
            launch = self.safe
""",
        """
        try:
            launch = self.safe
        except Exception:
            launch = self.safe
        else:
            launch = self.danger
""",
        """
        try:
            launch = self.safe
        finally:
            if flag:
                launch = self.danger
""",
        """
        match value:
            case 0:
                launch = self.danger
            case _:
                launch = self.safe
""",
        """
        for item in values:
            if item:
                launch = self.danger
""",
        """
        while flag:
            launch = self.danger
            flag = False
""",
    ],
)
def test_isolation_checker_unions_reachable_branch_aliases(
    tmp_path: Path,
    branch: str,
):
    source = f"""
import subprocess
def probe():
    pass
class Runner:
    def safe(self):
        pass
    def danger(self):
        subprocess.run(["true"])
    def start(self, flag=True, value=0, values=()):
        launch = self.safe
{branch}
        launch()
Runner().start()
"""
    assert "call to subprocess.run" in _operations(tmp_path, source)


@pytest.mark.parametrize(
    "body",
    [
        """
        launch = self.danger
        def invoke(launch):
            launch()
        invoke(self.safe)
""",
        """
        launch = self.danger
        del launch
        launch = self.safe
        launch()
""",
        """
        launch = self.danger
        def launch():
            pass
        launch()
""",
        """
        launch = self.danger
        if flag:
            launch = self.safe
        else:
            launch = self.safe
        launch()
""",
        """
        launch = self.danger
        try:
            probe()
        except Exception:
            launch = self.danger
        finally:
            launch = self.safe
        launch()
""",
    ],
)
def test_isolation_checker_respects_alias_shadowing_and_reassignment(
    tmp_path: Path,
    body: str,
):
    source = f"""
import subprocess
def probe():
    pass
class Runner:
    def safe(self):
        pass
    def danger(self):
        subprocess.run(["true"])
    def start(self, flag=True):
{body}
Runner().start()
"""
    assert "call to subprocess.run" not in _operations(tmp_path, source)


@pytest.mark.parametrize(
    ("source", "operation"),
    [
        (
            """
import subprocess
def outer():
    def launch():
        subprocess.Popen(["true"])
    launch()
outer()
""",
            "call to subprocess.Popen",
        ),
        (
            """
import os
class Runner:
    def start(self):
        self.launch()
    def launch(self):
        os.system("true")
Runner().start()
""",
            "call to os.system",
        ),
    ],
)
def test_isolation_checker_detects_indirect_process_families(
    tmp_path: Path,
    source: str,
    operation: str,
):
    assert operation in _operations(tmp_path, source)


def test_isolation_checker_follows_imported_decorators_and_constructors(
    tmp_path: Path,
):
    helper = tmp_path / "helper.py"
    helper.write_text(
        """
import subprocess
import tempfile
import os

def collection_decorator(function):
    subprocess.run(["true"])
    return function

class CollectionObject:
    def __init__(self):
        tempfile.mkdtemp()

    @classmethod
    def run(cls):
        os.system("true")
""",
        encoding="utf-8",
    )
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        """
from helper import CollectionObject, collection_decorator

@collection_decorator
def test_probe():
    pass

INSTANCE = CollectionObject()
CollectionObject.run()
""",
        encoding="utf-8",
    )

    operations = {
        finding.operation for finding in isolation.inspect_files([helper, probe])
    }

    assert "call to subprocess.run" in operations
    assert "call to tempfile.mkdtemp" in operations
    assert "call to os.system" in operations


def test_isolation_checker_scans_all_python_helpers():
    scanned = isolation.test_python_files()
    assert (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "tool_loop_eval"
        / ("evalsearch_server.py")
        in scanned
    )


def test_lane_preserves_empty_collection_failure(monkeypatch):
    commands = []

    def fake_run(command):
        commands.append(command)
        return 5

    monkeypatch.setattr(lanes, "_run", fake_run)
    assert lanes.main(["legacy", "--collect-only"]) == 5
    pytest_arguments = commands[0][commands[0].index("-q") :]
    assert pytest_arguments.count("-m") == 1
    assert "legacy" in commands[0]


@pytest.mark.parametrize("argument", ["-m", "--ignore", "-k", "tests/test_bot.py"])
def test_lane_rejects_pytest_selection_overrides(argument: str):
    with pytest.raises(SystemExit):
        lanes.main(["unit-fast", argument, "anything"])


def test_lane_rejects_invalid_worker_count():
    with pytest.raises(SystemExit):
        lanes.main(["unit-fast", "--workers", "0"])


def _successful_jobs() -> str:
    return json.dumps({name: {"result": "success"} for name in release.REQUIRED_JOBS})


def test_job_results_are_required_and_complete():
    with pytest.raises(RuntimeError, match="required"):
        release.check_job_results(None)
    with pytest.raises(RuntimeError, match="missing"):
        release.check_job_results("{}")
    release.check_job_results(_successful_jobs())


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1}',
        (
            b'{"schema_version":1,"base_sha":"'
            + b"a" * 40
            + b'","candidate_sha":"'
            + b"b" * 40
            + b'","files":{"same.txt":"'
            + b"0" * 64
            + b'","same.txt":"'
            + b"1" * 64
            + b'"},"deletions":[]}'
        ),
    ],
)
def test_release_manifest_rejects_duplicate_json_keys_at_every_level(raw: bytes):
    with pytest.raises(RuntimeError, match="duplicate keys"):
        release._parse_release_manifest(raw)


def test_staging_payload_rejects_duplicate_json_keys():
    body = (
        b'{"status":"ready","status":"ok","release_sha":"'
        + _CANDIDATE_SHA.encode()
        + b'","build_sha":"'
        + _CANDIDATE_SHA.encode()
        + b'"}'
    )
    with pytest.raises(RuntimeError, match="duplicate keys"):
        release._validate_health_payload(body, _CANDIDATE_SHA)


def test_snapshot_rehearsal_inventory_uses_current_protected_schema(tmp_path: Path):
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    inventory = tmp_path / "inventory.json"

    release._write_rehearsal_inventory(inventory, state)

    payload = json.loads(inventory.read_text(encoding="utf-8"))
    assert payload == {
        "format": "discord-bot-state-inventory-v1",
        "services": [{"name": "release-gate", "path": str(state)}],
    }
    assert Path(payload["services"][0]["path"]).is_absolute()
    assert inventory.stat().st_uid == __import__("os").getuid()
    assert inventory.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("observed", ["-1", "NaN", "Infinity", "-Infinity"])
def test_rollback_rate_must_be_finite_and_non_negative(observed: str):
    with pytest.raises(RuntimeError, match="finite and non-negative"):
        release.check_rollback_threshold(observed, "1")


@pytest.mark.parametrize("threshold", ["0", "-1", "NaN", "Infinity"])
def test_rollback_threshold_must_be_finite_and_positive(threshold: str):
    with pytest.raises(RuntimeError, match="finite and greater than zero"):
        release.check_rollback_threshold("0", threshold)


class _Headers(dict):
    def get_content_type(self):
        return str(self.get("Content-Type", "")).split(";", 1)[0]


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
        url: str = "https://staging.example.test/ready",
    ) -> None:
        self.body = body
        self.status = status
        self.headers = _Headers({"Content-Type": content_type})
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def geturl(self):
        return self.url

    def read(self, limit: int):
        return self.body[:limit]


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def open(self, request, timeout: int):
        assert request.full_url == "https://staging.example.test/ready"
        assert timeout == 15
        return self.response


def test_staging_requires_allowlisted_https_and_health_schema():
    body = json.dumps(
        {
            "status": "ready",
            "release_sha": _CANDIDATE_SHA,
            "build_sha": _CANDIDATE_SHA,
        }
    ).encode()
    release.check_staging(
        "https://staging.example.test/ready",
        candidate_sha=_CANDIDATE_SHA,
        required=True,
        allowed_hosts=["staging.example.test"],
        opener=_Opener(_Response(body)),
    )


@pytest.mark.parametrize(
    "response",
    [
        _Response(b"not-json"),
        _Response(b"{}"),
        _Response(b'{"status":"unhealthy"}'),
        _Response(b"", status=204),
        _Response(b'{"status":"ok"}', content_type="text/plain"),
        _Response(b'{"status":"ok"}', url="https://redirected.example.test/ready"),
    ],
)
def test_staging_fails_closed(response: _Response):
    with pytest.raises(RuntimeError):
        release.check_staging(
            "https://staging.example.test/ready",
            candidate_sha=_CANDIDATE_SHA,
            required=True,
            allowed_hosts=["staging.example.test"],
            opener=_Opener(response),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://staging.example.test/ready",
        "https://127.0.0.1/ready",
        "https://staging.example.test@127.0.0.1/ready",
        "file:///etc/passwd",
    ],
)
def test_staging_rejects_ssrf_boundaries(url: str):
    with pytest.raises(RuntimeError):
        release.check_staging(
            url,
            candidate_sha=_CANDIDATE_SHA,
            required=True,
            allowed_hosts=["staging.example.test"],
            opener=SimpleNamespace(),
        )


@pytest.mark.parametrize("field", ["release_sha", "build_sha"])
def test_staging_rejects_release_identity_mismatch(field: str):
    payload = {
        "status": "ready",
        "release_sha": _CANDIDATE_SHA,
        "build_sha": _CANDIDATE_SHA,
    }
    payload[field] = "b" * 40
    with pytest.raises(RuntimeError, match=field):
        release.check_staging(
            "https://staging.example.test/ready",
            candidate_sha=_CANDIDATE_SHA,
            required=True,
            allowed_hosts=["staging.example.test"],
            opener=_Opener(_Response(json.dumps(payload).encode())),
        )


def test_workspace_snapshot_detects_collection_file_mutation(tmp_path: Path):
    target = tmp_path / "collection-output.txt"
    before = suite_config._workspace_snapshot(tmp_path)
    target.write_text("polluted", encoding="utf-8")
    after = suite_config._workspace_snapshot(tmp_path)

    assert before != after


def test_protected_manifest_parser_requires_entries_and_required_paths():
    with pytest.raises(RuntimeError, match="no entries"):
        release._parse_manifest(
            b"# empty\n",
            required_paths={"one.txt"},
            label="release",
        )
    with pytest.raises(RuntimeError, match="missing required paths"):
        release._parse_manifest(
            ("0" * 64 + "  one.txt\n").encode(),
            required_paths={"two.txt"},
            label="release",
        )


def test_tool_loop_protected_subset_matches_immutable_evidence(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    manifest = (root / release.PROTECTED_EVIDENCE_RELATIVE).read_bytes()

    def fake_blob(repo: Path, candidate: str, relative: str) -> bytes:
        del repo, candidate
        if relative == release.PROTECTED_EVIDENCE_RELATIVE:
            return manifest
        return (root / relative).read_bytes()

    monkeypatch.setattr(release, "_git_blob", fake_blob)
    mismatches = release.check_tool_loop_protected_evidence("candidate", repo=root)
    assert "services/session.py" not in release.TOOL_LOOP_PROTECTED_PATHS
    assert "services/session.py" in mismatches
    assert "tests/test_session.py" in mismatches
    assert release.PROTECTED_EVIDENCE_RELATIVE not in release.REQUIRED_RELEASE_PATHS
    assert hashlib.sha256(manifest).hexdigest() == release.PROTECTED_EVIDENCE_DIGEST


def test_release_main_routes_every_candidate_check_to_explicit_repo(
    tmp_path: Path,
    monkeypatch,
):
    candidate = (tmp_path / "candidate").resolve()
    candidate.mkdir()
    marker = tmp_path / "candidate-checker-ran"
    candidate_checker = candidate / "scripts" / "check_release_gate.py"
    candidate_checker.parent.mkdir()
    candidate_checker.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    observed_repos: list[Path] = []

    monkeypatch.setattr(release, "_candidate_repository", lambda path: candidate)
    monkeypatch.setattr(release, "check_job_results", lambda raw: None)
    monkeypatch.setattr(
        release,
        "check_commit_range",
        lambda base, candidate_sha, *, repo: observed_repos.append(repo),
    )
    monkeypatch.setattr(
        release,
        "check_diff",
        lambda base, candidate_sha, *, repo: observed_repos.append(repo),
    )
    monkeypatch.setattr(
        release,
        "check_conflict_markers",
        lambda candidate_sha, *, repo: observed_repos.append(repo),
    )
    monkeypatch.setattr(
        release,
        "check_clean_checkout",
        lambda *, repo: observed_repos.append(repo),
    )

    def protected(candidate_sha: str, *, repo: Path) -> list[str]:
        observed_repos.append(repo)
        return []

    monkeypatch.setattr(release, "check_tool_loop_protected_evidence", protected)
    monkeypatch.setattr(
        release,
        "check_release_manifest",
        lambda base, candidate_sha, manifest, digest, *, repo: observed_repos.append(
            repo
        ),
    )
    rehearsals = []
    monkeypatch.setattr(
        release, "rehearse_backup_restore", lambda: rehearsals.append("controller")
    )
    monkeypatch.setattr(release, "check_staging", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        release, "check_rollback_threshold", lambda observed, threshold: None
    )

    assert (
        release.main(
            [
                "--job-results",
                _successful_jobs(),
                "--repo",
                str(candidate),
                "--base-sha",
                "a" * 40,
                "--candidate-sha",
                "b" * 40,
                "--release-manifest",
                str(tmp_path / "detached.json"),
                "--release-manifest-digest",
                "0" * 64,
            ]
        )
        == 0
    )
    assert observed_repos == [candidate] * 6
    assert rehearsals == ["controller"]
    assert not marker.exists()


def test_taxonomy_is_exhaustive_and_marks_real_subprocess_tests_integration():
    taxonomy = suite_config._load_taxonomy()
    assert taxonomy["test_operational_gates.py"] == "integration"
    assert taxonomy["test_backup_state.py"] == "integration"
    assert taxonomy["test_git_hooks.py"] == "integration"
    assert taxonomy["test_runtime_bootstrap.py"] == "integration"
    root = Path(__file__).resolve().parents[2]
    assert (root / "tests/integration/test_free_code_bridge_subprocess.py").is_file()
    assert (root / "tests/integration/test_mem0_subprocess.py").is_file()
    assert "subprocess.run(" not in (root / "tests/test_free_code_bridge.py").read_text(
        encoding="utf-8"
    )
    assert "subprocess.run(" not in (root / "tests/test_mem0_integrity.py").read_text(
        encoding="utf-8"
    )


def test_application_module_cleanup_removes_real_module(monkeypatch):
    parent = ModuleType("services.phase4_parent")
    child = ModuleType("services.phase4_parent.child")
    child.__file__ = "/tmp/child.py"
    child.__spec__ = SimpleNamespace()
    parent.child = child
    monkeypatch.setitem(__import__("sys").modules, parent.__name__, parent)
    monkeypatch.setitem(__import__("sys").modules, child.__name__, child)

    suite_config._remove_module(child.__name__, child)

    assert child.__name__ not in __import__("sys").modules
    assert not hasattr(parent, "child")


def test_only_exact_pytest_current_test_transition_is_framework_owned():
    nodeid = "tests/unit/test_phase4_scripts.py::test_probe"
    assert suite_config._is_framework_environment_transition(
        "PYTEST_CURRENT_TEST",
        f"{nodeid} (setup)",
        f"{nodeid} (teardown)",
        nodeid,
    )
    assert not suite_config._is_framework_environment_transition(
        "PYTEST_CURRENT_TEST",
        f"{nodeid} (setup)",
        "application-overwrite",
        nodeid,
    )
    assert not suite_config._is_framework_environment_transition(
        "APPLICATION_STATE",
        f"{nodeid} (setup)",
        f"{nodeid} (teardown)",
        nodeid,
    )
