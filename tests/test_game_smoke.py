from fastapi.testclient import TestClient

import main


def test_player_can_join_a_fresh_room(monkeypatch):
    monkeypatch.setattr(main, "QUIZZES", {
        "Smoke": [{
            "type": "mcq",
            "question": "Работает?",
            "answers": ["Да", "Нет"],
            "correct": 0,
            "time": 30,
            "points": 1000,
        }]
    })
    main.rooms.clear()
    client = TestClient(main.app)

    created = client.get("/create-room", params={"quiz": "Smoke"})
    assert created.status_code == 200
    pin = created.json()["pin"]

    with client.websocket_connect(f"/ws/player?room={pin}&username=Alice") as player:
        assert player.receive_json()["type"] == "waiting"
        assignment = player.receive_json()
        assert assignment["type"] == "team_assignment"
        assert "Alice" in main.rooms[pin]["players"]


def test_unknown_quiz_is_rejected(monkeypatch):
    monkeypatch.setattr(main, "QUIZZES", {"Known": []})
    main.rooms.clear()
    response = TestClient(main.app).get("/create-room", params={"quiz": "Missing"})
    assert response.json()["error"] == "unknown_quiz"


def test_host_socket_can_submit_an_answer(monkeypatch):
    monkeypatch.setattr(main, "QUIZZES", {
        "Host answer": [{
            "type": "mcq",
            "question": "Выберите ответ",
            "answers": ["Первый", "Второй"],
            "correct": 0,
            "time": 60,
            "points": 1000,
        }]
    })
    main.rooms.clear()
    client = TestClient(main.app)
    pin = client.get("/create-room", params={"quiz": "Host answer"}).json()["pin"]

    with client.websocket_connect(f"/ws/host?room={pin}&username=host") as host:
        host.send_json({"type": "start"})
        for _ in range(5):
            if host.receive_json().get("type") == "question":
                break
        host.send_json({"type": "answer", "selected": 0})
        for _ in range(5):
            if host.receive_json().get("type") == "progress":
                break
        assert main.rooms[pin]["answers"]["Ведущий"]["selected"] == 0


def test_invalid_player_name_is_rejected(monkeypatch):
    monkeypatch.setattr(main, "QUIZZES", {"Smoke": []})
    main.rooms.clear()
    client = TestClient(main.app)
    pin = client.get("/create-room", params={"quiz": "Smoke"}).json()["pin"]

    with client.websocket_connect(f"/ws/player?room={pin}&username={'x' * 41}") as player:
        message = player.receive_json()

    assert message == {
        "type": "error",
        "message": "Имя игрока должно содержать от 1 до 40 символов",
    }
    assert main.rooms[pin]["players"] == {}
