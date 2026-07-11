from fastapi.testclient import TestClient

from embedding_service import main


class FakeEmbeddingModel:
    def encode(self, texts, **_kwargs):
        return {"dense_vecs": [[float(index)] * 1024 for index, _text in enumerate(texts, start=1)]}


def test_health_and_embedding_contract(monkeypatch):
    monkeypatch.setattr(main.state, "model", FakeEmbeddingModel())
    monkeypatch.setattr(main.state, "error", None)
    monkeypatch.setattr(main.state, "load", lambda: None)
    with TestClient(main.app) as client:
        health = client.get("/healthz")
        response = client.post("/v1/embeddings", json={"input": ["algebra", "geometry"]})
    assert health.json()["ok"] is True
    assert response.status_code == 200
    assert response.json()["dimension"] == 1024
    assert len(response.json()["embeddings"]) == 2
    assert len(response.json()["embeddings"][0]) == 1024
