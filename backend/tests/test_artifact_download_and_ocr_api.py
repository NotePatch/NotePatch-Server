from tests.conftest import auth_headers, first_workspace_id, register_user
from tests.test_document_permissions import create_upload_session


def _create_artifact(
    client,
    fake_storage,
    token: str,
    workspace_id: str,
    document_id: str,
    *,
    artifact_type: str,
    suffix: str,
    mime_type: str = "text/plain",
    metadata: dict | None = None,
):
    object_key = f"workspaces/{workspace_id}/documents/{document_id}/artifacts/test-{artifact_type}/{suffix}"
    fake_storage.objects[(fake_storage.bucket, object_key)] = {
        "file_size": 12,
        "mime_type": mime_type,
        "metadata": {},
        "body": b"artifact-data",
    }
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/artifacts",
        headers=auth_headers(token),
        json={
            "artifact_type": artifact_type,
            "object_key": object_key,
            "mime_type": mime_type,
            "file_size": 12,
            "metadata": metadata or {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_artifact_download_url_is_workspace_scoped(client, fake_storage):
    alice = register_user(client, "artifact-download-a@example.com")
    bob = register_user(client, "artifact-download-b@example.com")
    alice_workspace_id = first_workspace_id(client, alice["access_token"])
    bob_workspace_id = first_workspace_id(client, bob["access_token"])
    alice_upload = create_upload_session(client, alice["access_token"], alice_workspace_id)
    bob_upload = create_upload_session(client, bob["access_token"], bob_workspace_id)

    artifact = _create_artifact(
        client,
        fake_storage,
        alice["access_token"],
        alice_workspace_id,
        alice_upload["document"]["id"],
        artifact_type="ocr_markdown",
        suffix="ocr.md",
        mime_type="text/markdown",
    )

    own_response = client.get(
        f"/api/v1/workspaces/{alice_workspace_id}/documents/{alice_upload['document']['id']}/artifacts/{artifact['id']}/download-url",
        headers=auth_headers(alice["access_token"]),
    )
    guessed_in_own_workspace = client.get(
        f"/api/v1/workspaces/{bob_workspace_id}/documents/{bob_upload['document']['id']}/artifacts/{artifact['id']}/download-url",
        headers=auth_headers(bob["access_token"]),
    )
    direct_other_workspace = client.get(
        f"/api/v1/workspaces/{alice_workspace_id}/documents/{alice_upload['document']['id']}/artifacts/{artifact['id']}/download-url",
        headers=auth_headers(bob["access_token"]),
    )

    assert own_response.status_code == 200
    payload = own_response.json()
    assert payload["artifact_id"] == artifact["id"]
    assert payload["document_id"] == alice_upload["document"]["id"]
    assert payload["artifact_type"] == "ocr_markdown"
    assert payload["filename"] == "ocr.md"
    assert payload["mime_type"] == "text/markdown"
    assert payload["expires_in"] == 900
    assert payload["download_url"].startswith("mock://download/")
    assert guessed_in_own_workspace.status_code == 404
    assert direct_other_workspace.status_code == 403


def test_artifact_download_url_rejects_artifact_from_another_document(client, fake_storage):
    user = register_user(client, "artifact-download-doc-mismatch@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    first_upload = create_upload_session(client, user["access_token"], workspace_id, filename="first.pdf")
    second_upload = create_upload_session(client, user["access_token"], workspace_id, filename="second.pdf")

    artifact = _create_artifact(
        client,
        fake_storage,
        user["access_token"],
        workspace_id,
        second_upload["document"]["id"],
        artifact_type="ocr_text",
        suffix="ocr.txt",
        mime_type="text/plain",
    )

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/documents/{first_upload['document']['id']}/artifacts/{artifact['id']}/download-url",
        headers=auth_headers(user["access_token"]),
    )

    assert response.status_code == 404


def test_ocr_artifacts_query_returns_latest_complete_set_with_optional_download_urls(client, fake_storage):
    user = register_user(client, "ocr-artifact-query@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = create_upload_session(client, user["access_token"], workspace_id, filename="ocr.pdf")
    document_id = upload["document"]["id"]

    _create_artifact(
        client,
        fake_storage,
        user["access_token"],
        workspace_id,
        document_id,
        artifact_type="ocr_json",
        suffix="old.json",
        mime_type="application/json",
        metadata={"ocr_run_id": "old"},
    )
    for artifact_type, suffix, mime_type in (
        ("ocr_json", "ocr.json", "application/json"),
        ("ocr_markdown", "ocr.md", "text/markdown"),
        ("ocr_text", "ocr.txt", "text/plain"),
    ):
        _create_artifact(
            client,
            fake_storage,
            user["access_token"],
            workspace_id,
            document_id,
            artifact_type=artifact_type,
            suffix=suffix,
            mime_type=mime_type,
            metadata={"ocr_run_id": "complete-run"},
        )

    metadata_response = client.get(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/ocr",
        headers=auth_headers(user["access_token"]),
    )
    with_urls_response = client.get(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/ocr?include_download_url=true&expires_seconds=600",
        headers=auth_headers(user["access_token"]),
    )

    assert metadata_response.status_code == 200
    metadata_payload = metadata_response.json()
    assert metadata_payload["document_id"] == document_id
    assert [artifact["artifact_type"] for artifact in metadata_payload["artifacts"]] == [
        "ocr_json",
        "ocr_markdown",
        "ocr_text",
    ]
    assert all(artifact["download_url"] is None for artifact in metadata_payload["artifacts"])

    assert with_urls_response.status_code == 200
    with_urls_payload = with_urls_response.json()
    assert [artifact["artifact_type"] for artifact in with_urls_payload["artifacts"]] == [
        "ocr_json",
        "ocr_markdown",
        "ocr_text",
    ]
    assert all((artifact["download_url"] or "").startswith("mock://download/") for artifact in with_urls_payload["artifacts"])
