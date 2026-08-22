"""Build and verify the hermetic validation image from the Resource contract."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from time import monotonic
from typing import Any, Mapping, Sequence
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
SOURCE_LABEL = "https://github.com/yijiazho/agent-engineering-platform"


class ValidationGateError(RuntimeError):
    """The image or verification inputs do not satisfy the locked contract."""


@dataclass(frozen=True)
class ValidationContract:
    image: str
    platform: str
    network: str
    container_path: str
    read_only: bool
    workdir: str
    cpu_limit: int | float
    memory_bytes: int
    timeout_ms: int
    required_executables: tuple[tuple[tuple[str, ...], str], ...]
    commands: tuple[tuple[str, ...], ...]
    dirty_path: str
    dirty_append: str
    verified_image_id: str


class CommandRunner:
    """Small subprocess boundary used by the release gate and its tests."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        print("+ " + subprocess.list2cmdline(list(argv)), flush=True)
        return subprocess.run(
            list(argv),
            cwd=cwd,
            check=True,
            text=True,
            timeout=timeout,
            capture_output=capture,
        )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationGateError(f"cannot read verification input {path}") from error
    if not isinstance(value, dict):
        raise ValidationGateError(f"verification input {path} must be an object")
    return value


def load_contract(root: Path = ROOT) -> ValidationContract:
    task = _read_json(root / ".ai/tasks/run-validation.yaml")
    lock = _read_json(root / "deploy/validation/image.lock.json")
    expected = _read_json(root / "fixtures/self-hosting/expected-bundle.json")
    validation = task.get("spec", {}).get("validation")
    fixture = expected.get("validation")
    gate = lock.get("gate")
    if not all(isinstance(value, Mapping) for value in (validation, fixture, gate)):
        raise ValidationGateError("Task, lock, and fixture must declare validation gate objects")
    assert isinstance(validation, Mapping)
    assert isinstance(fixture, Mapping)
    assert isinstance(gate, Mapping)

    comparisons = {
        "image": validation.get("image"),
        "requiredExecutables": validation.get("requiredExecutables"),
        "commands": [item.get("argv") for item in validation.get("commands", [])],
        "workspaceMount": validation.get("workspaceMount"),
        "resources": validation.get("resources"),
        "timeoutMs": validation.get("timeoutMs"),
    }
    for name, resource_value in comparisons.items():
        if gate.get(name) != resource_value or fixture.get(name) != resource_value:
            raise ValidationGateError(
                f"validation {name} drifted between Task, lock, and deterministic fixture"
            )
    image = str(validation.get("image", ""))
    if lock.get("image") != image or not IMAGE_PATTERN.fullmatch(image):
        raise ValidationGateError("validation image must be one matching immutable digest")
    if gate.get("verifiedImage") != image:
        raise ValidationGateError("lock image was not selected by the verification gate")
    verified_image_id = str(gate.get("verifiedImageId", ""))
    if not DIGEST_PATTERN.fullmatch(verified_image_id):
        raise ValidationGateError("lock requires a verified source image configuration digest")
    if gate.get("network") != "none" or gate.get("workdir") != "/workspace":
        raise ValidationGateError("validation gate must disable networking and use /workspace")
    if gate.get("platform") != "linux/amd64":
        raise ValidationGateError("validation gate must build the production Linux platform")
    fixture_gate = fixture.get("gate")
    if not isinstance(fixture_gate, Mapping):
        raise ValidationGateError("deterministic fixture must record validation gate identity")
    for name in (
        "platform", "network", "workdir", "image", "verifiedImage",
        "verifiedImageId", "dirtyChange",
    ):
        if fixture_gate.get(name) != gate.get(name):
            raise ValidationGateError(
                f"validation gate {name} drifted between lock and deterministic fixture"
            )
    dirty = gate.get("dirtyChange")
    if not isinstance(dirty, Mapping) or not str(dirty.get("path", "")).startswith("docs/"):
        raise ValidationGateError("validation gate requires one bounded documentation change")

    mount = validation["workspaceMount"]
    resources = validation["resources"]
    return ValidationContract(
        image=image,
        platform=str(gate["platform"]),
        network=str(gate["network"]),
        container_path=str(mount["containerPath"]),
        read_only=bool(mount["readOnly"]),
        workdir=str(gate["workdir"]),
        cpu_limit=resources["cpuLimit"],
        memory_bytes=int(resources["memoryBytes"]),
        timeout_ms=int(validation["timeoutMs"]),
        required_executables=tuple(
            (tuple(item["argv"]), str(item["versionPattern"]))
            for item in validation["requiredExecutables"]
        ),
        commands=tuple(tuple(item["argv"]) for item in validation["commands"]),
        dirty_path=str(dirty["path"]),
        dirty_append=str(dirty["append"]),
        verified_image_id=verified_image_id,
    )


def docker_create_argv(
    contract: ValidationContract, image: str, workspace: Path, name: str
) -> list[str]:
    mount = f"type=bind,src={workspace.resolve()},dst={contract.container_path}"
    if contract.read_only:
        mount += ",readonly"
    return [
        "docker", "create", "--name", name,
        "--network", contract.network,
        "--cpus", str(contract.cpu_limit),
        "--memory", str(contract.memory_bytes),
        "--mount", mount,
        image, "sleep", "infinity",
    ]


def _image_id(image: str, runner: CommandRunner) -> str:
    result = runner.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image], capture=True
    )
    image_id = result.stdout.strip()
    if not DIGEST_PATTERN.fullmatch(image_id):
        raise ValidationGateError(f"Docker returned an invalid image ID for {image}")
    return image_id


def _probe_image(image: str, contract: ValidationContract, runner: CommandRunner) -> None:
    for argv, pattern in contract.required_executables:
        result = runner.run(
            ["docker", "run", "--rm", "--platform", contract.platform,
             "--network", contract.network, image, *argv],
            capture=True,
            timeout=contract.timeout_ms / 1000,
        )
        output = (result.stdout + result.stderr).strip()
        print(output, flush=True)
        if re.search(pattern, output) is None:
            raise ValidationGateError(
                f"image readiness output for {argv[0]} does not match {pattern}"
            )


def _verify_workspace(
    image: str,
    workspace: Path,
    label: str,
    contract: ValidationContract,
    runner: CommandRunner,
) -> None:
    name = f"aep-validation-gate-{label}-{uuid4().hex}"
    deadline = monotonic() + contract.timeout_ms / 1000

    def remaining() -> float:
        value = deadline - monotonic()
        if value <= 0:
            raise subprocess.TimeoutExpired("validation gate", contract.timeout_ms / 1000)
        return value

    runner.run(
        docker_create_argv(contract, image, workspace, name), timeout=remaining()
    )
    try:
        runner.run(["docker", "start", name], timeout=remaining())
        for argv, pattern in contract.required_executables:
            result = runner.run(
                ["docker", "exec", name, *argv], capture=True, timeout=remaining()
            )
            output = (result.stdout + result.stderr).strip()
            print(output, flush=True)
            if re.search(pattern, output) is None:
                raise ValidationGateError(
                    f"{label} readiness output for {argv[0]} does not match {pattern}"
                )
        for argv in contract.commands:
            runner.run(
                ["docker", "exec", "--workdir", contract.workdir, name, *argv],
                timeout=remaining(),
            )
    finally:
        runner.run(["docker", "rm", "-f", name], timeout=30)


def _tracked_snapshot(root: Path, destination: Path, runner: CommandRunner) -> None:
    files = runner.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture=True,
    ).stdout.split("\0")
    for relative in files:
        if not relative:
            continue
        source = root / relative
        if not source.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    runner.run(["git", "init", "-b", "validation-gate"], cwd=destination)
    runner.run(["git", "config", "user.name", "AEP Validation Gate"], cwd=destination)
    runner.run(["git", "config", "user.email", "validation@example.invalid"], cwd=destination)
    runner.run(["git", "add", "."], cwd=destination)
    runner.run(["git", "commit", "-m", "Validation gate snapshot"], cwd=destination)


def _prepare_workspaces(
    root: Path,
    temporary_root: Path,
    contract: ValidationContract,
    runner: CommandRunner,
    *,
    include_working_tree: bool,
) -> tuple[Path, Path]:
    clean = temporary_root / "clean"
    if include_working_tree:
        clean.mkdir()
        _tracked_snapshot(root, clean, runner)
    else:
        status = runner.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            capture=True,
        ).stdout
        if status:
            raise ValidationGateError(
                "release verification requires a clean checkout; "
                "use --include-working-tree only for development"
            )
        runner.run(["git", "clone", "--no-hardlinks", str(root), str(clean)])
        runner.run(["git", "checkout", "--detach", "HEAD"], cwd=clean)
    if runner.run(["git", "status", "--porcelain=v1"], cwd=clean, capture=True).stdout:
        raise ValidationGateError("clean validation workspace is dirty")

    dirty = temporary_root / "dirty"
    runner.run(["git", "clone", "--no-hardlinks", str(clean), str(dirty)])
    changed = dirty / contract.dirty_path
    with changed.open("a", encoding="utf-8", newline="") as stream:
        stream.write(contract.dirty_append)
    status = runner.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=dirty,
        capture=True,
    ).stdout.strip()
    if status not in {f"M {contract.dirty_path}", f" M {contract.dirty_path}"}:
        raise ValidationGateError("dirty gate workspace must contain only the allowed change")
    if root.resolve() in {clean.resolve(), dirty.resolve()}:
        raise ValidationGateError("Resource checkout cannot be a candidate workspace")
    return clean, dirty


def _build_source(tag: str, contract: ValidationContract, runner: CommandRunner) -> str:
    runner.run(
        [
            "docker", "build", "--pull=false", "--platform", contract.platform,
            "--file", str(ROOT / "deploy/validation/Dockerfile"),
            "--label", f"org.opencontainers.image.source={SOURCE_LABEL}",
            "--tag", tag, str(ROOT),
        ],
        timeout=max(1200, contract.timeout_ms / 1000),
    )
    return _image_id(tag, runner)


def verify_source(
    contract: ValidationContract,
    runner: CommandRunner,
    *,
    tag: str,
    include_working_tree: bool,
    require_locked_identity: bool,
) -> str:
    image_id = _build_source(tag, contract, runner)
    if require_locked_identity and image_id != contract.verified_image_id:
        raise ValidationGateError(
            "reviewed Dockerfile image differs from the image identity recorded in the lock"
        )
    _probe_image(tag, contract, runner)
    with tempfile.TemporaryDirectory(prefix="aep-validation-gate-") as temporary:
        clean, dirty = _prepare_workspaces(
            ROOT,
            Path(temporary),
            contract,
            runner,
            include_working_tree=include_working_tree,
        )
        _verify_workspace(tag, clean, "clean", contract, runner)
        _verify_workspace(tag, dirty, "dirty", contract, runner)
    return image_id


def verify_published(contract: ValidationContract, runner: CommandRunner) -> str:
    runner.run(["docker", "manifest", "inspect", contract.image], capture=True)
    runner.run(["docker", "pull", "--platform", contract.platform, contract.image])
    image_id = _image_id(contract.image, runner)
    if image_id != contract.verified_image_id:
        raise ValidationGateError(
            "published digest does not resolve to the source image that passed the gate"
        )
    _probe_image(contract.image, contract, runner)
    return image_id


def promote(
    contract: ValidationContract,
    runner: CommandRunner,
    *,
    target: str,
    include_working_tree: bool,
) -> dict[str, str]:
    if "@sha256:" in target or ":" not in target.rsplit("/", 1)[-1]:
        raise ValidationGateError("promotion target must be an explicit registry tag")
    candidate = f"aep-validation-promotion:{uuid4().hex}"
    image_id = verify_source(
        contract,
        runner,
        tag=candidate,
        include_working_tree=include_working_tree,
        require_locked_identity=False,
    )
    runner.run(["docker", "tag", candidate, target])
    pushed = runner.run(["docker", "push", target], capture=True)
    matches = DIGEST_PATTERN.findall(pushed.stdout + pushed.stderr)
    if not matches:
        raise ValidationGateError("registry push did not report a manifest digest")
    published = f"{target.rsplit(':', 1)[0]}@{matches[-1]}"
    runner.run(["docker", "pull", "--platform", contract.platform, published])
    if _image_id(published, runner) != image_id:
        raise ValidationGateError("registry digest differs from the image that passed the gate")
    published_contract = ValidationContract(
        **{**contract.__dict__, "image": published, "verified_image_id": image_id}
    )
    _probe_image(published, published_contract, runner)
    return {"image": published, "verifiedImageId": image_id}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("candidate", "verify", "published", "promote"))
    parser.add_argument("--tag", default="aep-validation-gate:local")
    parser.add_argument("--target")
    parser.add_argument("--include-working-tree", action="store_true")
    arguments = parser.parse_args(argv)
    contract = load_contract()
    runner = CommandRunner()
    try:
        if arguments.mode == "candidate":
            result = {
                "verifiedImageId": verify_source(
                    contract,
                    runner,
                    tag=arguments.tag,
                    include_working_tree=arguments.include_working_tree,
                    require_locked_identity=False,
                )
            }
        elif arguments.mode == "verify":
            source_id = verify_source(
                contract,
                runner,
                tag=arguments.tag,
                include_working_tree=arguments.include_working_tree,
                require_locked_identity=True,
            )
            published_id = verify_published(contract, runner)
            result = {"sourceImageId": source_id, "publishedImageId": published_id}
        elif arguments.mode == "published":
            result = {"publishedImageId": verify_published(contract, runner)}
        else:
            if not arguments.target:
                raise ValidationGateError("promotion requires --target")
            result = promote(
                contract,
                runner,
                target=arguments.target,
                include_working_tree=arguments.include_working_tree,
            )
        print(json.dumps({"status": "PASS", **result}, sort_keys=True))
        return 0
    except (ValidationGateError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"validation gate failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
