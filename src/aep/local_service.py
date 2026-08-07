"""Credential-free local service adapters for the AEP MVP topology."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
from types import MappingProxyType
from typing import Any, Final
from urllib.request import urlopen

from aep.execution_checkout import local_checkout_registry
from aep.resource_loader import ResourceCollection, ResourceLoader, format_ref


MVP_SERVICE_PORTS: Final = MappingProxyType(
    {
        "event-controller": 8081,
        "resource-controller": 8082,
        "workflow-runtime": 8083,
        "agent-resolver": 8084,
        "context-builder": 8085,
        "tool-runtime": 8086,
        "evaluation-engine": 8087,
    }
)


class LocalServiceConfigurationError(ValueError):
    """Raised when a local service is not bound to one valid MVP environment."""


@dataclass(frozen=True)
class LocalServiceConfig:
    """Explicit identity, network, and persistence configuration for one adapter."""

    service_name: str
    port: int
    repository_root: Path
    resource_schema_root: Path
    repository_provider: str
    repository_owner: str
    repository_name: str
    workspace_name: str
    workspace_version: str
    execution_environment: str
    state_root: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "LocalServiceConfig":
        values = os.environ if environ is None else environ

        def required(name: str) -> str:
            value = values.get(name, "").strip()
            if not value:
                raise LocalServiceConfigurationError(f"{name} must be configured")
            return value

        service_name = required("AEP_SERVICE_NAME")
        if service_name not in MVP_SERVICE_PORTS:
            raise LocalServiceConfigurationError(
                f"AEP_SERVICE_NAME must be one of {sorted(MVP_SERVICE_PORTS)}"
            )
        try:
            port = int(required("AEP_SERVICE_PORT"))
        except ValueError as error:
            raise LocalServiceConfigurationError("AEP_SERVICE_PORT must be an integer") from error
        if not 0 <= port <= 65535:
            raise LocalServiceConfigurationError("AEP_SERVICE_PORT must be between 0 and 65535")

        return cls(
            service_name=service_name,
            port=port,
            repository_root=Path(required("AEP_REPOSITORY_ROOT")).resolve(),
            resource_schema_root=Path(required("AEP_RESOURCE_SCHEMA_ROOT")).resolve(),
            repository_provider=required("AEP_REPOSITORY_PROVIDER"),
            repository_owner=required("AEP_REPOSITORY_OWNER"),
            repository_name=required("AEP_REPOSITORY_NAME"),
            workspace_name=required("AEP_WORKSPACE_NAME"),
            workspace_version=required("AEP_WORKSPACE_VERSION"),
            execution_environment=required("AEP_EXECUTION_ENVIRONMENT"),
            state_root=Path(required("AEP_STATE_ROOT")).resolve(),
        )


@dataclass(frozen=True)
class LocalServiceRuntime:
    config: LocalServiceConfig
    resources: ResourceCollection

    @classmethod
    def initialize(cls, config: LocalServiceConfig) -> "LocalServiceRuntime":
        resources = ResourceLoader(
            config.repository_root,
            schema_root=config.resource_schema_root,
        ).load()
        workspace = resources.workspace
        repository = workspace.data["spec"]["repository"]
        expected = (
            config.repository_provider,
            config.repository_owner,
            config.repository_name,
        )
        actual = (repository["provider"], repository["owner"], repository["name"])
        if actual != expected:
            raise LocalServiceConfigurationError(
                "configured repository does not match the Workspace repository: "
                f"configured={expected!r}, workspace={actual!r}"
            )
        if (workspace.name, workspace.version) != (
            config.workspace_name,
            config.workspace_version,
        ):
            raise LocalServiceConfigurationError(
                "configured Workspace does not match the loaded Workspace: "
                f"configured={config.workspace_name}:{config.workspace_version}, "
                f"loaded={workspace.name}:{workspace.version}"
            )

        service_state = config.state_root / config.service_name
        service_state.mkdir(parents=True, exist_ok=True)
        if config.service_name == "workflow-runtime":
            # The shared aep-state volume carries checkout ownership across
            # adapter restarts. Source objects and mutable worktrees remain in
            # separate directories under that same durable boundary.
            local_checkout_registry(config.state_root)
            (config.state_root / "repository-cache").mkdir(
                parents=True, exist_ok=True
            )
            (config.state_root / "execution-worktrees").mkdir(
                parents=True, exist_ok=True
            )
        marker = service_state / "ready.json"
        marker.write_text(
            json.dumps(
                {
                    "service": config.service_name,
                    "workspace": f"{workspace.name}:{workspace.version}",
                    "environment": config.execution_environment,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return cls(config=config, resources=resources)

    def health(self) -> dict[str, Any]:
        workspace = self.resources.workspace
        return {
            "status": "ready",
            "service": self.config.service_name,
            "environment": self.config.execution_environment,
            "repository": (
                f"{self.config.repository_provider}:"
                f"{self.config.repository_owner}/{self.config.repository_name}"
            ),
            "workspace": f"{workspace.name}:{workspace.version}",
            "persistence": "local-directory",
        }

    def resolved_configuration(self) -> dict[str, Any]:
        return {
            "workspace": format_ref(self.resources.workspace.ref),
            "resources": [format_ref(resource.ref) for resource in self.resources.resources],
        }


class LocalServiceServer(ThreadingHTTPServer):
    runtime: LocalServiceRuntime


class LocalServiceHandler(BaseHTTPRequestHandler):
    """Small read-only API shared by the local service adapters."""

    server: LocalServiceServer

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path == "/healthz":
            self._send_json(200, self.server.runtime.health())
            return
        if (
            self.path == "/v1/resources"
            and self.server.runtime.config.service_name == "resource-controller"
        ):
            self._send_json(200, self.server.runtime.resolved_configuration())
            return
        self._send_json(404, {"error": "not_found"})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status: int, value: Mapping[str, Any]) -> None:
        body = json.dumps(value, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(config: LocalServiceConfig) -> LocalServiceServer:
    runtime = LocalServiceRuntime.initialize(config)
    server = LocalServiceServer(("0.0.0.0", config.port), LocalServiceHandler)
    server.runtime = runtime
    return server


class LocalMvpComposition:
    """Credential-free thread-backed composition used by the smoke test."""

    def __init__(self, configs: Sequence[LocalServiceConfig]) -> None:
        names = [config.service_name for config in configs]
        if set(names) != set(MVP_SERVICE_PORTS) or len(names) != len(MVP_SERVICE_PORTS):
            raise LocalServiceConfigurationError(
                "local composition requires exactly one configuration for every MVP service"
            )
        self._configs = tuple(configs)
        self._servers: list[LocalServiceServer] = []
        self._threads: list[Thread] = []

    def __enter__(self) -> "LocalMvpComposition":
        try:
            for config in self._configs:
                server = create_server(config)
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                self._servers.append(server)
                self._threads.append(thread)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_: object) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=5)
        self._servers.clear()
        self._threads.clear()

    @property
    def addresses(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                server.runtime.config.service_name: (
                    f"http://127.0.0.1:{server.server_address[1]}"
                )
                for server in self._servers
            }
        )


def _serve() -> None:
    server = create_server(LocalServiceConfig.from_env())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _check_health(url: str) -> None:
    with urlopen(url, timeout=2) as response:  # noqa: S310 - explicit local health URL
        if response.status != 200:
            raise SystemExit(1)
        value = json.load(response)
        if value.get("status") != "ready":
            raise SystemExit(1)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="start the configured local service adapter")
    health_parser = subparsers.add_parser("check-health", help="verify a health endpoint")
    health_parser.add_argument("--url", required=True)
    args = parser.parse_args(argv)
    if args.command == "serve":
        _serve()
    else:
        _check_health(args.url)


if __name__ == "__main__":
    main()
