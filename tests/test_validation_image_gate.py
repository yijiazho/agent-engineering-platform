from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from deploy.validation import verify


ROOT = Path(__file__).parents[1]


def test_gate_contract_matches_run_validation_resource_lock_and_fixture() -> None:
    contract = verify.load_contract(ROOT)

    assert contract.image.endswith(
        "@sha256:6e0214265e1c8bbdc0553413801dded85ede2c1c2e90be413d0c02fae17fbf5a"
    )
    assert contract.platform == "linux/amd64"
    assert contract.network == "none"
    assert contract.container_path == contract.workdir == "/workspace"
    assert contract.read_only is False
    assert contract.cpu_limit == 2
    assert contract.memory_bytes == 1073741824
    assert contract.timeout_ms == 600000
    assert contract.commands == (
        (
            "python",
            "/workspace/deploy/validation/offline_bootstrap.py",
            "--workspace",
            "/workspace",
        ),
        ("python", "-m", "pytest", "/workspace/tests"),
    )


def test_gate_docker_create_matches_production_isolation_contract(tmp_path: Path) -> None:
    contract = verify.load_contract(ROOT)

    argv = verify.docker_create_argv(contract, "candidate", tmp_path, "gate")

    assert argv == [
        "docker",
        "create",
        "--name",
        "gate",
        "--network",
        "none",
        "--cpus",
        "2",
        "--memory",
        "1073741824",
        "--mount",
        f"type=bind,src={tmp_path.resolve()},dst=/workspace",
        "candidate",
        "sleep",
        "infinity",
    ]


@pytest.mark.parametrize(
    ("relative", "mutate"),
    [
        (
            ".ai/tasks/run-validation.yaml",
            lambda value: value["spec"]["validation"]["commands"][1].update(
                argv=["python", "-m", "pytest", "tests"]
            ),
        ),
        (
            "deploy/validation/image.lock.json",
            lambda value: value["gate"].update(network="bridge"),
        ),
        (
            "deploy/validation/image.lock.json",
            lambda value: value["gate"].update(workdir="/source"),
        ),
        (
            "deploy/validation/image.lock.json",
            lambda value: value["gate"].update(
                image="ghcr.io/yijiazho/validation@sha256:" + "a" * 64
            ),
        ),
        (
            "deploy/validation/image.lock.json",
            lambda value: value["gate"].update(
                verifiedImage="ghcr.io/yijiazho/validation@sha256:" + "b" * 64
            ),
        ),
        (
            "fixtures/self-hosting/expected-bundle.json",
            lambda value: value["validation"]["workspaceMount"].update(
                readOnly=True
            ),
        ),
        (
            "fixtures/self-hosting/expected-bundle.json",
            lambda value: value["validation"]["resources"].update(cpuLimit=4),
        ),
    ],
)
def test_gate_rejects_resource_lock_or_fixture_drift(
    tmp_path: Path, relative: str, mutate
) -> None:
    for path in (
        ".ai/tasks/run-validation.yaml",
        "deploy/validation/image.lock.json",
        "fixtures/self-hosting/expected-bundle.json",
    ):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, target)
    target = tmp_path / relative
    value = json.loads(target.read_text(encoding="utf-8"))
    mutate(value)
    target.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(
        verify.ValidationGateError,
        match="drifted|disable networking|not selected",
    ):
        verify.load_contract(tmp_path)


class FakeRunner(verify.CommandRunner):
    def __init__(self, image_id: str) -> None:
        self.image_id = image_id
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, **kwargs):
        arguments = tuple(argv)
        self.calls.append(arguments)
        if arguments[:4] == ("docker", "image", "inspect", "--format"):
            return subprocess.CompletedProcess(argv, 0, stdout=self.image_id + "\n", stderr="")
        if arguments[:2] == ("docker", "run"):
            executable = arguments[-2]
            output = "Python 3.12.13\n" if executable == "python" else "git version 2.47.3\n"
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")


def test_published_gate_rejects_a_digest_with_a_different_image_identity() -> None:
    contract = verify.load_contract(ROOT)
    runner = FakeRunner("sha256:" + "f" * 64)

    with pytest.raises(verify.ValidationGateError, match="does not resolve"):
        verify.verify_published(contract, runner)


def test_published_gate_reruns_credential_free_probes_by_digest() -> None:
    contract = verify.load_contract(ROOT)
    runner = FakeRunner(contract.verified_image_id)

    assert verify.verify_published(contract, runner) == contract.verified_image_id
    probe_calls = [call for call in runner.calls if call[:2] == ("docker", "run")]
    assert len(probe_calls) == 2
    assert all("--network" in call and "none" in call for call in probe_calls)
    assert all(contract.image in call for call in probe_calls)


def test_ci_uses_the_checked_in_gate_for_all_validation_inputs() -> None:
    workflow = (ROOT / ".github/workflows/validation-image.yml").read_text(
        encoding="utf-8"
    )

    assert "python3 deploy/validation/verify.py verify" in workflow
    for path in (
        "deploy/validation/**",
        ".ai/tasks/run-validation.yaml",
        "fixtures/**",
        "requirements-dev.lock",
        "tests/**",
    ):
        assert path in workflow
