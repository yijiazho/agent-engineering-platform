"""Fail-closed configuration checks for the repository-bound dogfood deployment."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import re
from typing import Any

from aep.github_app_provider import github_app_provider_from_environment
from aep.local_service import (
    LocalServiceConfig,
    LocalServiceRuntime,
    main as local_main,
)
from aep.openai_model_provider import (
    openai_model_adapter_from_environment,
    verify_openai_model_provider_environment,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_BOUND_REPOSITORY = "yijiazho/agent-engineering-platform"


class DogfoodDeploymentConfigurationError(ValueError):
    """Raised before service startup when a dogfood invariant is not met."""


def emergency_publication_guard(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return false whenever the operator's durable disable marker exists."""

    values = os.environ if environ is None else environ
    configured = values.get("AEP_EMERGENCY_DISABLE_FILE", "").strip()
    return bool(configured) and not Path(configured).exists()


def verify_dogfood_environment(
    environ: Mapping[str, str] | None = None,
    *,
    require_secrets: bool = True,
) -> dict[str, Any]:
    """Validate immutable identity, storage, provider, and disable boundaries."""

    values = os.environ if environ is None else environ

    def required(name: str) -> str:
        value = values.get(name, "").strip()
        if not value:
            raise DogfoodDeploymentConfigurationError(f"{name} must be configured")
        return value

    image_digest = required("AEP_IMAGE_DIGEST")
    resource_revision = required("AEP_RESOURCE_REVISION")
    if not _SHA256.fullmatch(image_digest):
        raise DogfoodDeploymentConfigurationError(
            "AEP_IMAGE_DIGEST must be a lowercase sha256 digest without a prefix"
        )
    if not _COMMIT.fullmatch(resource_revision):
        raise DogfoodDeploymentConfigurationError(
            "AEP_RESOURCE_REVISION must be a lowercase 40-character Git commit"
        )
    repository = (
        f"{required('AEP_REPOSITORY_OWNER')}/"
        f"{required('AEP_REPOSITORY_NAME')}"
    )
    if repository != _BOUND_REPOSITORY:
        raise DogfoodDeploymentConfigurationError(
            f"dogfood deployment repository must be {_BOUND_REPOSITORY}"
        )
    if required("AEP_WORKSPACE_NAME") != "agent-engineering-platform":
        raise DogfoodDeploymentConfigurationError(
            "dogfood deployment Workspace name must be agent-engineering-platform"
        )
    if required("AEP_WORKSPACE_VERSION") != "1.0.0":
        raise DogfoodDeploymentConfigurationError(
            "dogfood deployment Workspace version must be 1.0.0"
        )
    if required("AEP_EXECUTION_ENVIRONMENT") != "dogfood":
        raise DogfoodDeploymentConfigurationError(
            "AEP_EXECUTION_ENVIRONMENT must be dogfood"
        )

    state_root = Path(required("AEP_STATE_ROOT")).resolve()
    resource_root = Path(required("AEP_REPOSITORY_ROOT")).resolve()
    if (
        state_root == resource_root
        or state_root in resource_root.parents
        or resource_root in state_root.parents
    ):
        raise DogfoodDeploymentConfigurationError(
            "Resource checkout and durable state roots must not overlap"
        )
    disable_file = Path(required("AEP_EMERGENCY_DISABLE_FILE")).resolve()
    if disable_file != state_root / "control" / "EMERGENCY_DISABLE":
        raise DogfoodDeploymentConfigurationError(
            "emergency-disable marker must be under the durable control directory"
        )

    service_name = required("AEP_SERVICE_NAME")
    secret_names_by_service = {
        "event-controller": ("AEP_GITHUB_WEBHOOK_SECRET_FILE",),
        "workflow-runtime": (
            "AEP_GITHUB_APP_PRIVATE_KEY_FILE",
            "AEP_OPENAI_API_KEY_FILE",
        ),
        "agent-resolver": ("AEP_OPENAI_API_KEY_FILE",),
        "tool-runtime": ("AEP_GITHUB_APP_PRIVATE_KEY_FILE",),
    }
    secret_names = secret_names_by_service.get(service_name, ())
    if service_name == "workflow-runtime":
        required("AEP_DOCKER_HOST_STATE_DIRECTORY")
    if require_secrets:
        for name in secret_names:
            path = Path(required(name))
            try:
                if not path.is_file() or path.stat().st_size == 0:
                    raise OSError
            except OSError:
                raise DogfoodDeploymentConfigurationError(
                    f"{name} must identify a non-empty mounted secret"
                ) from None

        if service_name in {"workflow-runtime", "tool-runtime"}:
            github_app_provider_from_environment(values)
        if service_name in {"workflow-runtime", "agent-resolver"}:
            openai_model_adapter_from_environment("openai", environ=values)

    verify_openai_model_provider_environment("openai", environ=values)
    runtime = LocalServiceRuntime.initialize(LocalServiceConfig.from_env(values))
    return {
        "status": "DISABLED" if runtime.emergency_disabled() else "READY",
        "repository": f"github:{repository}",
        "workspace": (
            f"{runtime.resources.workspace.name}:"
            f"{runtime.resources.workspace.version}"
        ),
        "imageDigest": f"sha256:{image_digest}",
        "resourceRevision": resource_revision,
        "emergencyDisabled": runtime.emergency_disabled(),
        "modelProvider": "openai",
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="validate safe deployment inputs")
    verify.add_argument("--without-secrets", action="store_true")
    subparsers.add_parser("start-service", help="verify and start one service")
    args = parser.parse_args(argv)
    if args.command == "start-service":
        verify_dogfood_environment()
        consumer = None
        if os.environ.get("AEP_SERVICE_NAME") == "workflow-runtime":
            from aep.dogfood_runtime import DogfoodReconciliationConsumer

            consumer = DogfoodReconciliationConsumer.from_environment(os.environ)
            consumer.start()
        try:
            local_main(["serve"])
        finally:
            if consumer is not None:
                consumer.close()
        return
    print(
        json.dumps(
            verify_dogfood_environment(require_secrets=not args.without_secrets),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
