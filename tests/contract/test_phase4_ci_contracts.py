from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


class _UniqueBaseLoader(yaml.BaseLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueBaseLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _workflow(name: str):
    return yaml.load(
        (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"),
        Loader=_UniqueBaseLoader,
    )


def _external_actions(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "uses" and isinstance(child, str) and not child.startswith("./"):
                yield child
            yield from _external_actions(child)
    elif isinstance(value, list):
        for child in value:
            yield from _external_actions(child)


def _named_step(workflow, job: str, name: str):
    return next(
        step for step in workflow["jobs"][job]["steps"] if step.get("name") == name
    )


def test_workflow_yaml_is_parseable():
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        assert (
            yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueBaseLoader)
            is not None
        )
    assert (
        yaml.load(
            (ROOT / ".github" / "actions" / "setup-tests" / "action.yml").read_text(
                encoding="utf-8"
            ),
            Loader=_UniqueBaseLoader,
        )
        is not None
    )
    for path in (ROOT / "pyproject.toml", ROOT / "tests" / "taxonomy.toml"):
        assert tomllib.loads(path.read_text(encoding="utf-8"))


def test_ci_config_parsers_reject_duplicate_keys():
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key"):
        yaml.load("jobs: {}\njobs: {}\n", Loader=_UniqueBaseLoader)
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads("[lane]\nmarker = 'unit'\nmarker = 'integration'\n")


def test_main_pull_requests_and_security_gate_are_required_without_release_gate():
    workflow = _workflow("test.yml")
    assert "main" in workflow["on"]["pull_request"]["branches"]
    assert workflow["jobs"]["security"]["uses"] == "./.github/workflows/security.yml"
    assert "release-gate" not in workflow["jobs"]
    assert "RELEASE_MANIFEST" not in (ROOT / ".github/workflows/test.yml").read_text(
        encoding="utf-8"
    )


def test_release_gate_runs_only_in_the_trusted_manual_workflow():
    workflow = _workflow("release.yml")
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "trusted-release",
        "cancel-in-progress": "true",
    }
    needs = workflow["jobs"]["release-gate"]["needs"]
    assert "security" in needs
    assert "legacy" not in needs
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["candidate_sha"]["required"] == "true"
    assert "reviewed_base_sha" in inputs
    assert "previous_release_sha" in inputs
    trust_job = workflow["jobs"]["trust-controller"]
    assert "if" not in trust_job
    assert "continue-on-error" not in trust_job
    assert "steps" in trust_job
    trust_command = trust_job["steps"][0]["run"]
    assert "^[0-9a-f]{40}$" in trust_command
    assert "CONTROLLER_REF" in trust_command
    assert "WORKFLOW_SHA" in trust_command
    assert 'test "${CONTROLLER_REF}" = "refs/heads/${DEFAULT_BRANCH}"' in (
        trust_command
    )
    assert 'test "${WORKFLOW_SHA}" = "${CONTROLLER_SHA}"' in trust_command
    for step in trust_job["steps"]:
        assert "continue-on-error" not in step
    downstream_jobs = set(workflow["jobs"]) - {"trust-controller"}
    for name in downstream_jobs:
        job = workflow["jobs"][name]
        needs = job["needs"]
        assert "trust-controller" in ({needs} if isinstance(needs, str) else set(needs))
        if name != "release-gate":
            assert "if" not in job
    command = _named_step(
        workflow, "release-gate", "Enforce trusted release prerequisites"
    )["run"]
    assert "REVIEWED_BASE_SHA" in command
    assert "PREVIOUS_RELEASE_SHA" in command
    assert "provide exactly one reviewed release base" in command
    assert "CANDIDATE_SHA}^" not in command
    assert "git rev-parse" not in command


def test_release_gate_uses_external_trust_anchors():
    workflow = _workflow("release.yml")
    gate_step = _named_step(
        workflow, "release-gate", "Enforce trusted release prerequisites"
    )
    environment = gate_step["env"]
    assert "runner.temp" in environment["RELEASE_MANIFEST"]
    assert "vars.RELEASE_MANIFEST_B64" in environment["RELEASE_MANIFEST_B64"]
    assert "vars.RELEASE_MANIFEST_SHA256" in environment["RELEASE_MANIFEST_DIGEST"]
    assert "vars.STAGING_HEALTH_ALLOWED_HOSTS" in environment["STAGING_ALLOWED_HOSTS"]
    command = gate_step["run"]
    assert gate_step["working-directory"] == "controller"
    assert '--repo "${GITHUB_WORKSPACE}/candidate"' in command
    assert "--base-sha" in command
    assert "--candidate-sha" in command
    assert "--release-manifest" in command
    assert "--release-manifest-digest" in command
    assert "--staging-allowed-host" in command
    assert "base64 --decode" in command
    assert "refactor-protected-manifest" not in command


def test_release_gate_separates_trusted_controller_from_candidate_data():
    workflow = _workflow("release.yml")
    release_job = workflow["jobs"]["release-gate"]
    controller = _named_step(workflow, "release-gate", "Check out trusted controller")
    candidate = _named_step(workflow, "release-gate", "Check out candidate as data")
    assert controller["with"] == {
        "ref": "${{ github.workflow_sha }}",
        "path": "controller",
        "persist-credentials": "false",
    }
    assert candidate["with"] == {
        "ref": "${{ inputs.candidate_sha }}",
        "path": "candidate",
        "fetch-depth": "0",
        "persist-credentials": "false",
    }
    for step in release_job["steps"]:
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] == "false"
    gate = _named_step(
        workflow, "release-gate", "Enforce trusted release prerequisites"
    )
    assert "vars.RELEASE_MANIFEST_B64" in gate["env"]["RELEASE_MANIFEST_B64"]
    assert "vars.RELEASE_MANIFEST_SHA256" in gate["env"]["RELEASE_MANIFEST_DIGEST"]
    assert "candidate/scripts" not in gate["run"]
    assert "python scripts/check_release_gate.py" in gate["run"]

    secret_anchors = ("RELEASE_MANIFEST_B64", "RELEASE_MANIFEST_SHA256")
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            text = repr(step)
            if job_name != "release-gate" or step is not gate:
                assert all(anchor not in text for anchor in secret_anchors)


def test_release_candidate_lanes_use_exact_sha_without_persisted_credentials():
    workflow = _workflow("release.yml")
    lane_jobs = {
        "collection-contract",
        "unit-fast",
        "characterization",
        "integration",
        "offline-eval",
    }
    for job_name in lane_jobs:
        assert workflow["jobs"][job_name]["needs"] == "trust-controller"
        checkout = workflow["jobs"][job_name]["steps"][0]
        assert checkout["with"] == {
            "ref": "${{ inputs.candidate_sha }}",
            "persist-credentials": "false",
        }
    security = workflow["jobs"]["security"]
    assert security["needs"] == "trust-controller"
    assert security["with"]["candidate_sha"] == "${{ inputs.candidate_sha }}"

    security_workflow = _workflow("security.yml")
    security_checkout = security_workflow["jobs"]["audit"]["steps"][0]
    assert security_checkout["with"] == {
        "ref": "${{ inputs.candidate_sha || github.sha }}",
        "persist-credentials": "false",
    }


def test_trust_controller_validates_candidate_graph_without_secrets():
    workflow = _workflow("release.yml")
    trust_job = workflow["jobs"]["trust-controller"]
    checkout = _named_step(
        workflow, "trust-controller", "Check out candidate graph for trust validation"
    )
    assert checkout["with"] == {
        "ref": "${{ inputs.candidate_sha }}",
        "path": "trust-candidate",
        "fetch-depth": "0",
        "persist-credentials": "false",
    }
    validation = _named_step(
        workflow, "trust-controller", "Validate reviewed candidate range"
    )
    assert validation["working-directory"] == "trust-candidate"
    assert "git rev-parse HEAD" in validation["run"]
    assert "git cat-file -e" in validation["run"]
    assert "git merge-base --is-ancestor" in validation["run"]
    assert "vars." not in repr(trust_job)
    assert "secrets." not in repr(trust_job)

    release_condition = workflow["jobs"]["release-gate"]["if"]
    assert "needs.trust-controller.result == 'success'" in release_condition
    assert "github.ref" not in release_condition
    assert "github.workflow_sha" not in release_condition


def test_ci_toolchain_is_separate_and_exactly_pinned():
    lock = ROOT / "requirements-ci.lock"
    lines = [
        line.strip()
        for line in lock.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lines
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s]+", line) for line in lines)
    packages = {line.split("==", 1)[0].lower().replace("-", "_") for line in lines}
    assert {"pip", "ruff", "mypy", "bandit", "pip_audit"} <= packages

    ci_sources = [
        ROOT / ".github" / "actions" / "setup-tests" / "action.yml",
        *sorted((ROOT / ".github" / "workflows").glob("*.yml")),
    ]
    ci_text = "\n".join(path.read_text(encoding="utf-8") for path in ci_sources)
    assert "requirements-dev.txt" not in ci_text
    assert "pip install --upgrade" not in ci_text
    assert not re.search(r"pip install[^\n]*(?:>=|~=|<\d)", ci_text)


def test_third_party_action_is_sha_pinned_and_permissions_are_restricted():
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for action in _external_actions(document):
            assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action), (path, action)
    composite_path = ROOT / ".github" / "actions" / "setup-tests" / "action.yml"
    composite = yaml.load(
        composite_path.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    for action in _external_actions(composite):
        assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action), action

    workflow = _workflow("lint.yml")
    assert workflow["permissions"] == {"contents": "read"}
    assert _workflow("security.yml")["permissions"] == {"contents": "read"}
    assert _workflow("notify-push.yml")["permissions"] == {}
    assert _workflow("notify-ci-failure.yml")["permissions"] == {"actions": "read"}


def test_ci_failure_notification_rechecks_latest_attempt_before_sending():
    workflow = _workflow("notify-ci-failure.yml")
    assert workflow["concurrency"] == {
        "group": "notify-ci-failure-${{ github.event.workflow_run.id }}",
        "cancel-in-progress": "true",
    }

    recheck = _named_step(
        workflow,
        "notify",
        "Recheck latest workflow attempt after grace period",
    )
    assert recheck["id"] == "latest-run"
    assert recheck["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert recheck["env"]["RUN_ID"] == "${{ github.event.workflow_run.id }}"
    assert (
        recheck["env"]["ORIGINAL_ATTEMPT"]
        == "${{ github.event.workflow_run.run_attempt }}"
    )
    command = recheck["run"]
    assert "sleep 60" in command
    assert "/actions/runs/${RUN_ID}" in command
    assert ".run_attempt" in command
    assert 'LATEST_STATUS}" == "completed"' in command
    assert 'LATEST_CONCLUSION}" == "failure"' in command
    assert "should_notify=%s" in command

    notify = _named_step(
        workflow,
        "notify",
        "Notify branch-specific Discord channels on CI failure",
    )
    assert notify["if"] == "steps.latest-run.outputs.should_notify == 'true'"


@pytest.mark.parametrize(
    ("latest_run", "expected"),
    [
        (
            {"run_attempt": 1, "status": "completed", "conclusion": "failure"},
            "true",
        ),
        (
            {"run_attempt": 2, "status": "in_progress", "conclusion": None},
            "false",
        ),
        (
            {"run_attempt": 2, "status": "completed", "conclusion": "success"},
            "false",
        ),
    ],
)
def test_ci_failure_notification_decision_uses_latest_attempt(
    tmp_path: Path,
    latest_run: dict[str, object],
    expected: str,
):
    workflow = _workflow("notify-ci-failure.yml")
    recheck = _named_step(
        workflow,
        "notify",
        "Recheck latest workflow attempt after grace period",
    )

    fixture = tmp_path / "run.json"
    fixture.write_text(json.dumps(latest_run), encoding="utf-8")
    output = tmp_path / "github-output"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "curl").write_text(
        '#!/bin/sh\ncat "${RUN_FIXTURE}"\n',
        encoding="utf-8",
    )
    (fake_bin / "sleep").chmod(0o755)
    (fake_bin / "curl").chmod(0o755)

    result = subprocess.run(
        ["bash", "-c", recheck["run"]],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "GH_TOKEN": "test-token",
            "GH_API_URL": "https://api.github.test",
            "GH_REPOSITORY": "owner/repository",
            "RUN_ID": "12345",
            "ORIGINAL_ATTEMPT": "1",
            "RUN_FIXTURE": str(fixture),
            "GITHUB_OUTPUT": str(output),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == f"should_notify={expected}\n"


def test_security_findings_are_blocking():
    text = (ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    assert "|| true" not in text
    workflow = _workflow("security.yml")
    assert "workflow_call" in workflow["on"]
