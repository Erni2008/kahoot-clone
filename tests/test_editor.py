import copy

import pytest
from fastapi.testclient import TestClient

import main
from quiz_app.access import AccessStore


@pytest.fixture()
def editor_client(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "QUIZZES", {"Исходный": []})
    monkeypatch.setattr(main, "QUIZ_DATA_FILE", tmp_path / "quizzes.json")
    monkeypatch.setattr(main, "EDITOR_TOKEN", "test-editor-token")
    monkeypatch.setattr(main, "ACCESS_STORE", AccessStore(tmp_path / "access.sqlite3"))
    monkeypatch.setattr(main, "QUIZ_REVISION", 10)
    return TestClient(main.app), {"Authorization": "Bearer test-editor-token"}


def test_public_list_does_not_expose_answers(editor_client):
    client, _ = editor_client
    response = client.get("/api/quizzes")
    assert response.status_code == 200
    assert response.json()["quizzes"] == [
        {"name": "Исходный", "question_count": 0, "question_types": []}
    ]
    assert "correct" not in response.text


def test_editor_requires_token(editor_client):
    client, _ = editor_client
    assert client.get("/api/editor/state").status_code == 401
    assert client.get("/admin-snapshot").status_code == 401


def test_admin_snapshot_accepts_editor_token(editor_client):
    client, headers = editor_client
    response = client.get("/admin-snapshot", headers=headers)
    assert response.status_code == 200
    assert "rooms" in response.json()


def test_editor_full_question_lifecycle(editor_client):
    client, headers = editor_client

    created = client.post(
        "/api/editor/quiz",
        headers=headers,
        json={"name": "Новый тест", "revision": 10},
    )
    assert created.status_code == 200
    revision = created.json()["revision"]

    question = {
        "type": "mcq",
        "question": "Сколько будет 2 + 2?",
        "answers": ["3", "4", "5"],
        "correct": 1,
        "time": 20,
        "points": 500,
    }
    added = client.post(
        "/api/editor/question",
        headers=headers,
        json={"quiz": "Новый тест", "question": question, "revision": revision},
    )
    assert added.status_code == 200
    assert added.json()["questions"][0]["correct"] == 1
    revision = added.json()["revision"]

    changed = copy.deepcopy(question)
    changed["question"] = "Сколько будет два плюс два?"
    updated = client.put(
        "/api/editor/question",
        headers=headers,
        json={"quiz": "Новый тест", "index": 0, "question": changed, "revision": revision},
    )
    assert updated.status_code == 200
    assert updated.json()["questions"][0]["question"] == changed["question"]
    revision = updated.json()["revision"]

    deleted = client.delete(
        f"/api/editor/question?quiz=Новый%20тест&index=0&revision={revision}",
        headers=headers,
    )
    assert deleted.status_code == 200
    assert deleted.json()["questions"] == []


def test_invalid_mcq_is_rejected(editor_client):
    client, headers = editor_client
    response = client.post(
        "/api/editor/question",
        headers=headers,
        json={
            "quiz": "Исходный",
            "revision": 10,
            "question": {
                "type": "mcq",
                "question": "Некорректный вопрос",
                "answers": ["Только один вариант"],
                "correct": 0,
            },
        },
    )
    assert response.status_code == 422


def test_running_room_receives_an_independent_question_copy(monkeypatch):
    source = {"Тест": [{"type": "text", "question": "Старый", "correct": "Ответ"}]}
    monkeypatch.setattr(main, "QUIZZES", source)
    room_copy = main.build_room_quiz("Тест")
    source["Тест"][0]["question"] = "Новый"
    assert room_copy[0]["question"] == "Старый"


def test_owner_can_create_and_revoke_an_editor(editor_client):
    client, headers = editor_client
    created = client.post(
        "/api/editor/collaborators",
        headers=headers,
        json={"name": "Мария", "role": "editor"},
    )
    assert created.status_code == 200
    token = created.json()["token"]
    collaborator_id = created.json()["collaborator"]["id"]
    assert token.startswith("qz_")

    editor_headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/editor/state", headers=editor_headers).status_code == 200
    assert client.post(
        "/api/editor/quiz",
        headers=editor_headers,
        json={"name": "От редактора", "revision": 10},
    ).status_code == 200

    assert client.delete(
        f"/api/editor/collaborators/{collaborator_id}", headers=headers
    ).status_code == 200
    assert client.get("/api/editor/state", headers=editor_headers).status_code == 401


def test_viewer_cannot_modify_quizzes(editor_client):
    client, headers = editor_client
    created = client.post(
        "/api/editor/collaborators",
        headers=headers,
        json={"name": "Наблюдатель", "role": "viewer"},
    ).json()
    viewer_headers = {"Authorization": f"Bearer {created['token']}"}
    assert client.get("/api/editor/state", headers=viewer_headers).status_code == 200
    denied = client.post(
        "/api/editor/quiz",
        headers=viewer_headers,
        json={"name": "Запрещено", "revision": 10},
    )
    assert denied.status_code == 403


def test_health_endpoints(editor_client):
    client, _ = editor_client
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").status_code == 200
