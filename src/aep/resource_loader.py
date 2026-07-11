"""Load and validate repository-local AEP resources."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource as SchemaResource
from referencing.jsonschema import DRAFT202012


RESOURCE_DIRECTORIES: dict[str, tuple[str, ...]] = {
    "Workflow": ("workflows",),
    "Task": ("tasks",),
    "Agent": ("agents",),
    "Prompt": ("prompts",),
    "Model": ("models",),
    "Tool": ("tools",),
    "KnowledgeBase": ("knowledge", "knowledgebases"),
    "Policy": ("policies",),
    "Evaluation": ("evaluations",),
    "Event": ("events",),
}

KIND_ORDER = {
    "Workspace": 0,
    "Workflow": 1,
    "Task": 2,
    "Agent": 3,
    "Prompt": 4,
    "Model": 5,
    "Tool": 6,
    "KnowledgeBase": 7,
    "Policy": 8,
    "Evaluation": 9,
    "Event": 10,
}


class ResourceLoaderError(Exception):
    """Base error for resource loading failures."""


class ResourceFileNotFoundError(ResourceLoaderError):
    """Raised when required resource files are missing."""


class ResourceParseError(ResourceLoaderError):
    """Raised when a resource file cannot be parsed."""


class ResourceValidationError(ResourceLoaderError):
    """Raised when a resource does not satisfy its schema."""


class DuplicateResourceError(ResourceLoaderError):
    """Raised when two files declare the same resource version."""


class DuplicateWorkspaceError(ResourceLoaderError):
    """Raised when more than one Workspace resource is discovered."""


class MissingResourceReferenceError(ResourceLoaderError):
    """Raised when a resource references another resource that was not loaded."""


class FloatingResourceVersionError(ResourceLoaderError):
    """Raised when a resource or reference uses a floating version."""


@dataclass(frozen=True, order=True)
class ResourceRef:
    kind: str
    name: str
    version: str

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ResourceRef":
        return cls(
            kind=str(value["kind"]),
            name=str(value["name"]),
            version=str(value["version"]),
        )


@dataclass(frozen=True)
class Resource:
    ref: ResourceRef
    path: Path
    data: dict[str, Any]
    references: tuple[ResourceRef, ...]

    @property
    def kind(self) -> str:
        return self.ref.kind

    @property
    def name(self) -> str:
        return self.ref.name

    @property
    def version(self) -> str:
        return self.ref.version


@dataclass(frozen=True)
class ResourceCollection:
    workspace: Resource
    resources: tuple[Resource, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_index", {resource.ref: resource for resource in self.resources})

    def get(self, ref: ResourceRef) -> Resource | None:
        return self._index.get(ref)

    def by_kind(self, kind: str) -> tuple[Resource, ...]:
        return tuple(resource for resource in self.resources if resource.kind == kind)


class ResourceLoader:
    """Loads AEP resources from a repository-local `.ai/` directory."""

    def __init__(self, repo_root: Path | str, schema_root: Path | str | None = None) -> None:
        self.repo_root = Path(repo_root)
        self.ai_root = self.repo_root / ".ai"
        self.schema_root = Path(schema_root) if schema_root else Path(__file__).parents[2] / "schemas" / "resources" / "v1"
        self._validators = self._build_validators()

    def load(self) -> ResourceCollection:
        workspace_path = self.ai_root / "workspace.yaml"
        if not workspace_path.is_file():
            raise ResourceFileNotFoundError(f"Missing required workspace file: {workspace_path}")

        resources = [self._load_resource(workspace_path)]
        for directory_names in RESOURCE_DIRECTORIES.values():
            for directory_name in directory_names:
                directory = self.ai_root / directory_name
                if directory.is_dir():
                    resources.extend(self._load_resource(path) for path in self._resource_files(directory))

        workspace_resources = [resource for resource in resources if resource.kind == "Workspace"]
        if len(workspace_resources) > 1:
            paths = ", ".join(str(resource.path) for resource in workspace_resources)
            raise DuplicateWorkspaceError(f"Workspace must be declared only in {workspace_path}; found: {paths}")

        index: dict[ResourceRef, Resource] = {}
        for resource in resources:
            if resource.ref in index:
                first_path = index[resource.ref].path
                raise DuplicateResourceError(
                    f"Duplicate resource version {format_ref(resource.ref)} in {first_path} and {resource.path}"
                )
            index[resource.ref] = resource

        for resource in resources:
            for reference in resource.references:
                if reference not in index:
                    raise MissingResourceReferenceError(
                        f"{resource.path} references missing resource {format_ref(reference)}"
                    )

        ordered = tuple(sorted(resources, key=_resource_sort_key))
        return ResourceCollection(workspace=resources[0], resources=ordered)

    def _load_resource(self, path: Path) -> Resource:
        data = _load_mapping(path)
        kind = str(data.get("kind", ""))
        if kind not in self._validators:
            raise ResourceValidationError(f"{path} declares unsupported resource kind: {kind!r}")

        self._validate_schema(path, data, kind)
        ref = ResourceRef.from_mapping({"kind": kind, **data["metadata"]})
        _reject_floating_version(path, ref)
        references = tuple(sorted(_find_resource_refs(data), key=_ref_sort_key))
        for reference in references:
            _reject_floating_version(path, reference)
        return Resource(ref=ref, path=path, data=data, references=references)

    def _validate_schema(self, path: Path, data: dict[str, Any], kind: str) -> None:
        errors = sorted(self._validators[kind].iter_errors(data), key=lambda error: list(error.path))
        if errors:
            message = _format_validation_error(errors[0])
            raise ResourceValidationError(f"{path}: {message}")

    def _build_validators(self) -> dict[str, Draft202012Validator]:
        schemas: dict[str, dict[str, Any]] = {}
        for path in self.schema_root.glob("*.schema.json"):
            schema = json.loads(path.read_text(encoding="utf-8"))
            schemas[path.name] = schema

        registry = Registry()
        for schema in schemas.values():
            registry = registry.with_resource(
                schema["$id"],
                SchemaResource.from_contents(schema, default_specification=DRAFT202012),
            )

        validators: dict[str, Draft202012Validator] = {}
        for kind in KIND_ORDER:
            if kind == "Workspace":
                schema_name = "workspace.schema.json"
            else:
                schema_name = f"{kind.lower()}.schema.json"
            if schema_name in schemas:
                validators[kind] = Draft202012Validator(schemas[schema_name], registry=registry)
        return validators

    @staticmethod
    def _resource_files(directory: Path) -> tuple[Path, ...]:
        return tuple(
            sorted(
                path
                for path in directory.rglob("*")
                if path.is_file() and path.suffix.lower() in {".json", ".yaml", ".yml"}
            )
        )


def load_resources(repo_root: Path | str) -> ResourceCollection:
    return ResourceLoader(repo_root).load()


def format_ref(ref: ResourceRef) -> str:
    return f"{ref.kind}/{ref.name}:{ref.version}"


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as json_error:
        if path.suffix.lower() not in {".yaml", ".yml"}:
            raise ResourceParseError(f"{path}: invalid JSON: {json_error}") from json_error
        try:
            import yaml  # type: ignore[import-untyped]
        except ModuleNotFoundError as yaml_error:
            raise ResourceParseError(
                f"{path}: YAML syntax requires PyYAML; JSON content is supported without it"
            ) from yaml_error
        value = yaml.safe_load(text)

    if not isinstance(value, dict):
        raise ResourceParseError(f"{path}: expected a resource object")
    return value


def _find_resource_refs(value: Any) -> Iterable[ResourceRef]:
    if isinstance(value, dict):
        if set(value) == {"kind", "name", "version"}:
            yield ResourceRef.from_mapping(value)
            return
        for child in value.values():
            yield from _find_resource_refs(child)
    elif isinstance(value, list):
        for item in value:
            yield from _find_resource_refs(item)


def _reject_floating_version(path: Path, ref: ResourceRef) -> None:
    if ref.version == "latest":
        raise FloatingResourceVersionError(f"{path}: floating resource version is not allowed in {format_ref(ref)}")


def _resource_sort_key(resource: Resource) -> tuple[int, str, tuple[int, int, int, str], str]:
    return (
        KIND_ORDER.get(resource.kind, 100),
        resource.name,
        _semver_sort_key(resource.version),
        str(resource.path),
    )


def _ref_sort_key(ref: ResourceRef) -> tuple[int, str, tuple[int, int, int, str]]:
    return (KIND_ORDER.get(ref.kind, 100), ref.name, _semver_sort_key(ref.version))


def _semver_sort_key(version: str) -> tuple[int, int, int, str]:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(.*)$", version)
    if not match:
        return (0, 0, 0, version)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4))


def _format_validation_error(error: ValidationError) -> str:
    location = ".".join(str(part) for part in error.absolute_path)
    if location:
        return f"{location}: {error.message}"
    return error.message
