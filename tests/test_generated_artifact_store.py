from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import pytest

from aep.generated_artifact_store import (
    ContentIntegrityError,
    GeneratedArtifactValidationError,
    ImmutableGeneratedArtifactError,
    InMemoryContentAddressedStore,
    InMemoryGeneratedArtifactStore,
)
from aep.runtime_store import InMemoryRuntimeObjectStore


TASK_EXECUTION_ID = "taskexecution-123456789abc"


def artifact_metadata(artifact_id: str, *, artifact_type: str = "IMPLEMENTATION_PLAN"):
    return {
        "apiVersion": "aep.dev/v1alpha1",
        "kind": "GeneratedArtifact",
        "id": artifact_id,
        "traceId": "trace-generated-artifact",
        "createdAt": "2026-07-27T10:00:00Z",
        "updatedAt": "2026-07-27T10:00:00Z",
        "provenance": {
            "actor": "artifact-store",
            "workflowExecutionId": "workflowexecution-123456789abc",
            "taskExecutionId": TASK_EXECUTION_ID,
            "repositoryRevision": "abc1234",
            "resourceRefs": [],
        },
        "taskExecutionId": TASK_EXECUTION_ID,
        "artifactType": artifact_type,
        "repositoryRevision": "abc1234",
        "mediaType": "application/json",
    }


def test_publish_separates_metadata_from_content_and_verifies_digest() -> None:
    store = InMemoryGeneratedArtifactStore()
    content = b"# Implementation plan\n"
    expected_address = f"sha256:{sha256(content).hexdigest()}"

    artifact = store.publish(
        artifact_metadata("generatedartifact-123456789abc"), content
    )

    assert artifact["contentAddress"] == expected_address
    assert artifact["publishedAt"] == artifact["createdAt"]
    assert "content" not in artifact
    assert store.get_content(artifact["id"]) == content


def test_duplicate_structured_content_has_one_content_object() -> None:
    content_store = InMemoryContentAddressedStore()
    store = InMemoryGeneratedArtifactStore(content_store=content_store)

    first = store.publish(
        artifact_metadata("generatedartifact-123456789abc"),
        {"steps": ["inspect", "change"], "risk": "low"},
    )
    second = store.publish(
        artifact_metadata(
            "generatedartifact-abcdef123456", artifact_type="DESIGN_DOCUMENT"
        ),
        {"risk": "low", "steps": ["inspect", "change"]},
    )

    assert first["contentAddress"] == second["contentAddress"]
    assert first["id"] != second["id"]
    assert content_store.object_count == 1


def test_lookup_by_task_execution_returns_metadata_in_publication_order() -> None:
    store = InMemoryGeneratedArtifactStore()
    first = store.publish(
        artifact_metadata("generatedartifact-123456789abc"), b"plan"
    )
    second = store.publish(
        artifact_metadata(
            "generatedartifact-abcdef123456", artifact_type="PATCH"
        ),
        b"patch",
    )

    artifacts = store.list_by_task_execution(TASK_EXECUTION_ID)

    assert [artifact["id"] for artifact in artifacts] == [
        first["id"],
        second["id"],
    ]
    assert artifacts[0]["provenance"]["actor"] == "artifact-store"
    assert artifacts[0]["traceId"] == "trace-generated-artifact"


def test_republication_is_idempotent_but_mutation_is_rejected() -> None:
    content_store = InMemoryContentAddressedStore()
    store = InMemoryGeneratedArtifactStore(content_store=content_store)
    metadata = artifact_metadata("generatedartifact-123456789abc")
    published = store.publish(metadata, b"original")

    assert store.publish(metadata, b"original") == published

    with pytest.raises(ImmutableGeneratedArtifactError):
        store.publish(metadata, b"changed")
    assert content_store.object_count == 1

    changed_metadata = artifact_metadata("generatedartifact-123456789abc")
    changed_metadata["repositoryRevision"] = "def5678"
    with pytest.raises(ImmutableGeneratedArtifactError):
        store.publish(changed_metadata, b"original")
    assert content_store.object_count == 1

    assert store.get_content(published["id"]) == b"original"


def test_expected_content_address_must_match_content() -> None:
    store = InMemoryGeneratedArtifactStore()
    metadata = artifact_metadata("generatedartifact-123456789abc")
    metadata["contentAddress"] = f"sha256:{'0' * 64}"

    with pytest.raises(ContentIntegrityError, match="does not match"):
        store.publish(metadata, b"actual")


def test_returned_metadata_cannot_mutate_published_evidence() -> None:
    store = InMemoryGeneratedArtifactStore()
    artifact_id = "generatedartifact-123456789abc"
    published = store.publish(artifact_metadata(artifact_id), b"original")

    with pytest.raises(TypeError):
        published["artifactType"] = "PATCH"  # type: ignore[index]
    published["provenance"]["actor"] = "caller mutation"

    persisted = store.get(artifact_id)
    assert persisted["provenance"]["actor"] == "artifact-store"


def test_concurrent_publication_indexes_artifact_once() -> None:
    store = InMemoryGeneratedArtifactStore()
    metadata = artifact_metadata("generatedartifact-123456789abc")

    with ThreadPoolExecutor(max_workers=8) as executor:
        artifacts = list(
            executor.map(lambda _: store.publish(metadata, b"plan"), range(32))
        )

    assert all(artifact == artifacts[0] for artifact in artifacts)
    assert len(store.list_by_task_execution(TASK_EXECUTION_ID)) == 1


def test_task_execution_lookup_survives_artifact_store_recreation() -> None:
    runtime_store = InMemoryRuntimeObjectStore()
    first_adapter = InMemoryGeneratedArtifactStore(runtime_store=runtime_store)
    first_adapter.publish(
        artifact_metadata("generatedartifact-123456789abc"), b"plan"
    )

    second_adapter = InMemoryGeneratedArtifactStore(runtime_store=runtime_store)

    artifacts = second_adapter.list_by_task_execution(TASK_EXECUTION_ID)
    assert [artifact["id"] for artifact in artifacts] == [
        "generatedartifact-123456789abc"
    ]


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("apiVersion", "not-aep"),
        ("id", "bad"),
        ("createdAt", "not-a-timestamp"),
        ("updatedAt", "also-not-a-timestamp"),
        ("artifactType", "NOT_A_REAL_TYPE"),
        ("provenance", {}),
    ],
)
def test_publish_rejects_metadata_that_violates_runtime_schema(
    field: str, invalid_value: object
) -> None:
    content_store = InMemoryContentAddressedStore()
    runtime_store = InMemoryRuntimeObjectStore()
    store = InMemoryGeneratedArtifactStore(
        content_store=content_store,
        runtime_store=runtime_store,
    )
    metadata = artifact_metadata("generatedartifact-123456789abc")
    metadata[field] = invalid_value

    with pytest.raises(GeneratedArtifactValidationError):
        store.publish(metadata, b"invalid")

    assert content_store.object_count == 0
    assert runtime_store.get("generatedartifact-123456789abc") is None
