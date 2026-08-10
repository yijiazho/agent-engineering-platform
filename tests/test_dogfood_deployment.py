from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from aep.dogfood_deployment import (
    DogfoodDeploymentConfigurationError,
    emergency_publication_guard,
    verify_dogfood_environment,
)
from aep.local_service import LocalServiceConfig, LocalServiceRuntime


ROOT = Path(__file__).parents[1]


def test_dogfood_environment_binds_pinned_release_resources_and_providers(
    tmp_path: Path,
) -> None:
    environment = dogfood_environment(tmp_path)

    result = verify_dogfood_environment(environment)

    assert result == {
        "emergencyDisabled": False,
        "imageDigest": "sha256:" + "a" * 64,
        "modelProvider": "openai",
        "repository": "github:yijiazho/agent-engineering-platform",
        "resourceRevision": git_head(),
        "status": "READY",
        "workspace": "agent-engineering-platform:1.0.0",
    }


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("AEP_IMAGE_DIGEST", "latest", "sha256 digest"),
        ("AEP_RESOURCE_REVISION", "main", "Git commit"),
        ("AEP_REPOSITORY_NAME", "another-repository", "must be yijiazho"),
        ("AEP_EXECUTION_ENVIRONMENT", "production", "must be dogfood"),
    ],
)
def test_dogfood_environment_rejects_floating_or_drifted_identity(
    tmp_path: Path, name: str, value: str, message: str
) -> None:
    environment = dogfood_environment(tmp_path)
    environment[name] = value

    with pytest.raises(DogfoodDeploymentConfigurationError, match=message):
        verify_dogfood_environment(environment)


def test_dogfood_environment_requires_only_service_scoped_secrets(
    tmp_path: Path,
) -> None:
    environment = dogfood_environment(tmp_path, service="resource-controller")
    for name in (
        "AEP_GITHUB_WEBHOOK_SECRET_FILE",
        "AEP_GITHUB_APP_PRIVATE_KEY_FILE",
        "AEP_OPENAI_API_KEY_FILE",
    ):
        environment.pop(name)

    assert verify_dogfood_environment(environment)["status"] == "READY"


def test_emergency_disable_is_dynamic_and_rejects_new_deliveries(tmp_path: Path) -> None:
    environment = dogfood_environment(tmp_path)
    runtime = LocalServiceRuntime.initialize(LocalServiceConfig.from_env(environment))
    marker = Path(environment["AEP_EMERGENCY_DISABLE_FILE"])
    marker.parent.mkdir(parents=True)
    marker.write_text("operator disabled publication\n", encoding="utf-8")

    assert runtime.health()["status"] == "disabled"
    assert verify_dogfood_environment(environment)["status"] == "DISABLED"
    assert emergency_publication_guard(environment) is False


def test_self_hosting_compose_is_pinned_read_only_durable_and_least_privilege() -> None:
    compose = (ROOT / "deploy/self-hosting/compose.yaml").read_text(encoding="utf-8")

    assert "@sha256:${AEP_IMAGE_DIGEST" in compose
    assert "AEP_RESOURCE_REVISION: ${AEP_RESOURCE_REVISION" in compose
    assert ":/srv/aep/resources:ro" in compose
    assert ":/var/lib/aep" in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "AEP_EMERGENCY_DISABLE_FILE: /var/lib/aep/control/EMERGENCY_DISABLE" in compose
    assert compose.count("/var/run/docker.sock") == 2
    assert compose.count("github_webhook_secret]") == 1
    assert compose.count("openai_api_key]") == 2
    assert compose.count("github_app_private_key") >= 3
    for service in (
        "event-controller",
        "resource-controller",
        "workflow-runtime",
        "agent-resolver",
        "context-builder",
        "tool-runtime",
        "evaluation-engine",
    ):
        assert f"  {service}:" in compose


def dogfood_environment(tmp_path: Path, *, service: str = "event-controller") -> dict[str, str]:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    secrets = {
        "AEP_GITHUB_WEBHOOK_SECRET_FILE": secret_root / "webhook",
        "AEP_GITHUB_APP_PRIVATE_KEY_FILE": secret_root / "github.pem",
        "AEP_OPENAI_API_KEY_FILE": secret_root / "openai",
    }
    for path in secrets.values():
        path.write_text("test-secret", encoding="utf-8")
    return {
        "AEP_SERVICE_NAME": service,
        "AEP_SERVICE_PORT": "0",
        "AEP_REPOSITORY_ROOT": str(ROOT),
        "AEP_RESOURCE_SCHEMA_ROOT": str(ROOT / "schemas/resources/v1"),
        "AEP_REPOSITORY_PROVIDER": "github",
        "AEP_REPOSITORY_OWNER": "yijiazho",
        "AEP_REPOSITORY_NAME": "agent-engineering-platform",
        "AEP_REPOSITORY_DEFAULT_BRANCH": "main",
        "AEP_WORKSPACE_NAME": "agent-engineering-platform",
        "AEP_WORKSPACE_VERSION": "1.0.0",
        "AEP_EXECUTION_ENVIRONMENT": "dogfood",
        "AEP_STATE_ROOT": str(tmp_path / "state"),
        "AEP_RESOURCE_REVISION": git_head(),
        "AEP_IMAGE_DIGEST": "a" * 64,
        "AEP_EMERGENCY_DISABLE_FILE": str(
            tmp_path / "state/control/EMERGENCY_DISABLE"
        ),
        "AEP_OPENAI_API_URL": "https://api.openai.com/v1",
        **{name: str(path) for name, path in secrets.items()},
    }


def git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
