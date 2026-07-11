from tests.conftest import auth_headers, first_workspace_id, register_user


def create_upload_session(client, token: str, workspace_id: str, filename: str = "exam.pdf", kind: str = "exam"):
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(token),
        json={
            "filename": filename,
            "mime_type": "application/pdf",
            "file_size": 1234,
            "document_kind": kind,
            "title": "Exam",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_document_access_and_artifact_creation_are_workspace_scoped(client):
    alice = register_user(client, "alice3@example.com")
    bob = register_user(client, "bob3@example.com")
    alice_workspace_id = first_workspace_id(client, alice["access_token"])
    bob_workspace_id = first_workspace_id(client, bob["access_token"])

    upload = create_upload_session(client, alice["access_token"], alice_workspace_id)
    document_id = upload["document"]["id"]

    direct_other_workspace = client.get(
        f"/api/v1/workspaces/{alice_workspace_id}/documents/{document_id}",
        headers=auth_headers(bob["access_token"]),
    )
    assert direct_other_workspace.status_code == 403

    guessed_id_in_own_workspace = client.get(
        f"/api/v1/workspaces/{bob_workspace_id}/documents/{document_id}",
        headers=auth_headers(bob["access_token"]),
    )
    assert guessed_id_in_own_workspace.status_code == 404

    artifact = client.post(
        f"/api/v1/workspaces/{bob_workspace_id}/documents/{document_id}/artifacts",
        headers=auth_headers(bob["access_token"]),
        json={
            "artifact_type": "ocr_json",
            "object_key": f"workspaces/{bob_workspace_id}/documents/{document_id}/artifacts/a/ocr_json.json",
            "mime_type": "application/json",
        },
    )
    assert artifact.status_code == 404


def test_safe_object_key_and_document_list_filtering(client):
    alice = register_user(client, "alice4@example.com")
    bob = register_user(client, "bob4@example.com")
    alice_workspace_id = first_workspace_id(client, alice["access_token"])
    bob_workspace_id = first_workspace_id(client, bob["access_token"])

    upload = create_upload_session(
        client,
        alice["access_token"],
        alice_workspace_id,
        filename="../evil path\r\n.pdf",
        kind="homework",
    )
    document = upload["document"]
    object_key = upload["object_key"]

    assert object_key.startswith(f"workspaces/{alice_workspace_id}/documents/{document['id']}/original/")
    assert ".." not in object_key
    assert "\\" not in object_key
    assert "\r" not in object_key
    assert "\n" not in object_key
    assert document["original_filename"] == "evil_path.pdf"

    create_upload_session(client, bob["access_token"], bob_workspace_id, filename="bob.pdf", kind="exam")

    alice_docs = client.get(
        f"/api/v1/workspaces/{alice_workspace_id}/documents?document_kind=homework&file_type=pdf&page=1&page_size=10",
        headers=auth_headers(alice["access_token"]),
    )
    bob_docs = client.get(
        f"/api/v1/workspaces/{bob_workspace_id}/documents",
        headers=auth_headers(bob["access_token"]),
    )

    assert alice_docs.status_code == 200
    assert [item["id"] for item in alice_docs.json()] == [document["id"]]
    assert bob_docs.status_code == 200
    assert all(item["workspace_id"] == bob_workspace_id for item in bob_docs.json())


def test_download_url_requires_workspace_access(client):
    alice = register_user(client, "alice5@example.com")
    bob = register_user(client, "bob5@example.com")
    alice_workspace_id = first_workspace_id(client, alice["access_token"])
    bob_workspace_id = first_workspace_id(client, bob["access_token"])

    upload = create_upload_session(client, alice["access_token"], alice_workspace_id)
    document_id = upload["document"]["id"]

    own_response = client.get(
        f"/api/v1/workspaces/{alice_workspace_id}/documents/{document_id}/download-url",
        headers=auth_headers(alice["access_token"]),
    )
    cross_workspace = client.get(
        f"/api/v1/workspaces/{bob_workspace_id}/documents/{document_id}/download-url",
        headers=auth_headers(bob["access_token"]),
    )

    assert own_response.status_code == 200
    assert own_response.json()["download_url"].startswith("mock://download/")
    assert cross_workspace.status_code == 404
