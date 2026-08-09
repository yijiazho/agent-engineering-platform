from __future__ import annotations

import json
import hashlib
import hmac
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.request import Request, urlopen

import pytest

from aep.local_service import (
    LocalMvpComposition,
    LocalServiceConfig,
    LocalServiceConfigurationError,
    LocalServiceRuntime,
    MVP_SERVICE_PORTS,
)
from aep.resource_loader import ResourceLoader, format_ref
from aep.github_webhook import WEBHOOK_PATH


REPOSITORY_ROOT = Path(__file__).parents[1]


def test_smoke_starts_all_services_and_resolves_basic_resources(tmp_path: Path) -> None:
    configs = [service_config(name, tmp_path, port=0) for name in MVP_SERVICE_PORTS]

    with LocalMvpComposition(configs) as composition:
        assert set(composition.addresses) == set(MVP_SERVICE_PORTS)
        for name, address in composition.addresses.items():
            with urlopen(f"{address}/healthz", timeout=2) as response:
                health = json.load(response)
            assert health == {
                "environment": "local-test",
                "persistence": "local-directory",
                "repository": "github:yijiazho/agent-engineering-platform",
                "service": name,
                "status": "ready",
                "workspace": "agent-engineering-platform:1.0.0",
            }

        resource_address = composition.addresses["resource-controller"]
        with urlopen(f"{resource_address}/v1/resources", timeout=2) as response:
            resolved = json.load(response)

    loaded = ResourceLoader(REPOSITORY_ROOT).load()
    assert resolved == {
        "workspace": "Workspace/agent-engineering-platform:1.0.0",
        "resources": [format_ref(resource.ref) for resource in loaded.resources],
    }
    for service_name in MVP_SERVICE_PORTS:
        marker = json.loads((tmp_path / service_name / "ready.json").read_text())
        assert marker["service"] == service_name


def test_configuration_requires_every_explicit_boundary() -> None:
    with pytest.raises(LocalServiceConfigurationError, match="webhook secret"):
        LocalServiceConfig.from_env(
            {
                "AEP_SERVICE_NAME": "event-controller",
                "AEP_SERVICE_PORT": "8081",
                "AEP_REPOSITORY_ROOT": str(REPOSITORY_ROOT),
                "AEP_RESOURCE_SCHEMA_ROOT": str(
                    REPOSITORY_ROOT / "schemas/resources/v1"
                ),
                "AEP_REPOSITORY_PROVIDER": "github",
                "AEP_REPOSITORY_OWNER": "yijiazho",
                "AEP_REPOSITORY_NAME": "agent-engineering-platform",
                "AEP_WORKSPACE_NAME": "agent-engineering-platform",
                "AEP_WORKSPACE_VERSION": "1.0.0",
                "AEP_EXECUTION_ENVIRONMENT": "local-test",
            }
        )


def test_event_controller_loads_webhook_secret_from_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "github-webhook-secret"
    secret_file.write_bytes(b"file-backed-secret\n")
    values = {
        "AEP_SERVICE_NAME": "event-controller",
        "AEP_SERVICE_PORT": "8081",
        "AEP_REPOSITORY_ROOT": str(REPOSITORY_ROOT),
        "AEP_RESOURCE_SCHEMA_ROOT": str(REPOSITORY_ROOT / "schemas/resources/v1"),
        "AEP_REPOSITORY_PROVIDER": "github",
        "AEP_REPOSITORY_OWNER": "yijiazho",
        "AEP_REPOSITORY_NAME": "agent-engineering-platform",
        "AEP_WORKSPACE_NAME": "agent-engineering-platform",
        "AEP_WORKSPACE_VERSION": "1.0.0",
        "AEP_EXECUTION_ENVIRONMENT": "local-test",
        "AEP_STATE_ROOT": str(tmp_path / "state"),
        "AEP_GITHUB_WEBHOOK_SECRET_FILE": str(secret_file),
    }

    config = LocalServiceConfig.from_env(values)

    assert config.github_webhook_secret == b"file-backed-secret"


def test_event_controller_requires_secret_and_accepts_signed_http_delivery(
    tmp_path: Path,
) -> None:
    config = service_config("event-controller", tmp_path, port=0)
    payload = json.loads(
        (REPOSITORY_ROOT / "fixtures/github/issue-created.json").read_text(encoding="utf-8")
    )
    payload["repository"]["full_name"] = "yijiazho/agent-engineering-platform"
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    signature = "sha256=" + hmac.new(
        config.github_webhook_secret or b"", body, hashlib.sha256
    ).hexdigest()

    with LocalMvpComposition(
        [
            config
            if name == "event-controller"
            else service_config(name, tmp_path, port=0)
            for name in MVP_SERVICE_PORTS
        ]
    ) as composition:
        request = Request(
            composition.addresses["event-controller"] + WEBHOOK_PATH,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": signature,
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "local-delivery-038",
            },
        )
        with urlopen(request, timeout=2) as response:
            result = json.load(response)

    assert response.status == 202
    assert result["status"] == "accepted"


def test_event_controller_restart_replays_from_shared_durable_state(
    tmp_path: Path,
) -> None:
    config = service_config("event-controller", tmp_path, port=0)
    payload = json.loads(
        (REPOSITORY_ROOT / "fixtures/github/issue-created.json").read_text(
            encoding="utf-8"
        )
    )
    payload["repository"]["full_name"] = "yijiazho/agent-engineering-platform"
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    headers = {
        "X-Hub-Signature-256": "sha256="
        + hmac.new(
            config.github_webhook_secret or b"", body, hashlib.sha256
        ).hexdigest(),
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": "restart-delivery-038",
    }

    first_runtime = LocalServiceRuntime.initialize(config)
    first = first_runtime.github_webhook_ingress
    assert first is not None
    accepted = first.handle(headers=headers, raw_body=body)

    restarted_runtime = LocalServiceRuntime.initialize(config)
    restarted = restarted_runtime.github_webhook_ingress
    assert restarted is not None
    replay = restarted.handle(headers=headers, raw_body=body)

    assert accepted.status_code == 202
    assert replay.status_code == 200
    assert replay.body["eventId"] == accepted.body["eventId"]


def test_startup_rejects_repository_or_workspace_drift(tmp_path: Path) -> None:
    config = service_config("resource-controller", tmp_path, port=0)
    mismatched = LocalServiceConfig(
        **{**config.__dict__, "repository_name": "another-repository"}
    )

    with pytest.raises(LocalServiceConfigurationError, match="does not match"):
        LocalServiceRuntime.initialize(mismatched)


def test_explicit_schema_path_supports_deployment_layout(tmp_path: Path) -> None:
    repository_mount = tmp_path / "workspace"
    schema_mount = tmp_path / "opt/aep/schemas/resources/v1"
    state_mount = tmp_path / "var/lib/aep"
    site_packages = tmp_path / "site-packages"
    process_root = tmp_path / "runtime"
    shutil.copytree(REPOSITORY_ROOT / ".ai", repository_mount / ".ai")
    shutil.copytree(REPOSITORY_ROOT / "schemas/resources/v1", schema_mount)
    shutil.copytree(REPOSITORY_ROOT / "src/aep", site_packages / "aep")
    process_root.mkdir()
    environment = {
        **os.environ,
        "PYTHONPATH": str(site_packages),
        "AEP_SERVICE_NAME": "resource-controller",
        "AEP_SERVICE_PORT": "0",
        "AEP_REPOSITORY_ROOT": str(repository_mount),
        "AEP_RESOURCE_SCHEMA_ROOT": str(schema_mount),
        "AEP_REPOSITORY_PROVIDER": "github",
        "AEP_REPOSITORY_OWNER": "yijiazho",
        "AEP_REPOSITORY_NAME": "agent-engineering-platform",
        "AEP_WORKSPACE_NAME": "agent-engineering-platform",
        "AEP_WORKSPACE_VERSION": "1.0.0",
        "AEP_EXECUTION_ENVIRONMENT": "container-test",
        "AEP_STATE_ROOT": str(state_mount),
    }
    script = """
import json
from pathlib import Path
import aep
from aep.local_service import LocalServiceConfig, LocalServiceRuntime

config = LocalServiceConfig.from_env()
runtime = LocalServiceRuntime.initialize(config)
print(json.dumps({
    "packagePath": str(Path(aep.__file__).resolve()),
    "workspace": runtime.resolved_configuration()["workspace"],
}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=process_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert Path(result["packagePath"]).is_relative_to(site_packages)
    assert result["workspace"] == "Workspace/agent-engineering-platform:1.0.0"
    assert (state_mount / "resource-controller/ready.json").is_file()


def test_compose_declares_all_services_health_checks_ports_and_persistence() -> None:
    compose = (REPOSITORY_ROOT / "deploy/local/compose.yaml").read_text(encoding="utf-8")
    for service_name, port in MVP_SERVICE_PORTS.items():
        assert f"  {service_name}:" in compose
        assert f'AEP_SERVICE_NAME: "{service_name}"' in compose
        assert f'AEP_SERVICE_PORT: "{port}"' in compose
        assert f'"{port}:{port}"' in compose
    assert compose.count("\n    healthcheck:") == len(MVP_SERVICE_PORTS)
    assert compose.count("<<: *aep-common") == len(MVP_SERVICE_PORTS)
    assert compose.count("aep-state:/var/lib/aep") == 1
    assert "../../:/workspace:ro" in compose
    assert "AEP_RESOURCE_SCHEMA_ROOT: /opt/aep/schemas/resources/v1" in compose
    assert (
        "AEP_GITHUB_WEBHOOK_SECRET_FILE: /run/secrets/github_webhook_secret" in compose
    )
    assert "github_webhook_secret:" in compose


def service_config(name: str, state_root: Path, *, port: int) -> LocalServiceConfig:
    return LocalServiceConfig(
        service_name=name,
        port=port,
        repository_root=REPOSITORY_ROOT,
        resource_schema_root=REPOSITORY_ROOT / "schemas/resources/v1",
        repository_provider="github",
        repository_owner="yijiazho",
        repository_name="agent-engineering-platform",
        workspace_name="agent-engineering-platform",
        workspace_version="1.0.0",
        execution_environment="local-test",
        state_root=state_root,
        github_webhook_secret=(
            b"local-test-webhook-secret" if name == "event-controller" else None
        ),
    )
