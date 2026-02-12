from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import random
import string
from typing import Dict

app = FastAPI()


rooms: Dict[str, dict] = {}

QUESTIONS = [
    {
        "question": "Сколько ударов у 2 спецухи Ареса?",
        "answers": ["6", "7", "8", "9"],
        "correct": 2
    },
    {
        "question": "В каком году вышел MCoC?",
        "answers": ["2013", "2014", "2015", "2016"],
        "correct": 1
    }
]

def generate_pin():
    while True:
        pin = "".join(random.choices(string.digits, k=6))
        if pin not in rooms:
            return pin

@app.get("/create-room")
async def create_room():
    pin = generate_pin()
    rooms[pin] = {
        "host": None,
        "players": {},
        "scores": {},
        "answers": {},
        "question_index": 0
    }
    return {"pin": pin}

@app.websocket("/ws/{role}/{room}/{username}")
async def websocket_endpoint(websocket: WebSocket, role: str, room: str, username: str):
    await websocket.accept()

    if room not in rooms:
        await websocket.close()
        return

    if role == "host":
        rooms[room]["host"] = websocket
    else:
        rooms[room]["players"][username] = websocket
        rooms[room]["scores"][username] = 0
        await notify_host_players(room)

    try:
        while True:
            data = await websocket.receive_json()

            if data["type"] == "start":
                await send_question(room)

            if data["type"] == "answer":
                rooms[room]["answers"][username] = data["selected"]
                await notify_host_answered(room)

            if data["type"] == "reveal":
                correct = QUESTIONS[rooms[room]["question_index"]]["correct"]

                for user, ans in rooms[room]["answers"].items():
                    if ans == correct:
                        rooms[room]["scores"][user] += 1000

                await broadcast(room, {
                    "type": "result",
                    "correct": correct,
                    "scores": rooms[room]["scores"]
                })

                rooms[room]["answers"] = {}
                rooms[room]["question_index"] += 1

    except WebSocketDisconnect:
        pass

async def send_question(room):
    question = QUESTIONS[rooms[room]["question_index"]]
    await broadcast(room, {
        "type": "question",
        "question": question["question"],
        "answers": question["answers"]
    })

async def broadcast(room, message):
    if rooms[room]["host"]:
        await rooms[room]["host"].send_json(message)

    for ws in rooms[room]["players"].values():
        await ws.send_json(message)

async def notify_host_players(room):
    if rooms[room]["host"]:
        await rooms[room]["host"].send_json({
            "type": "players_update",
            "players": list(rooms[room]["players"].keys())
        })

async def notify_host_answered(room):
    if rooms[room]["host"]:
        await rooms[room]["host"].send_json({
            "type": "answered_count",
            "count": len(rooms[room]["answers"]),
            "total": len(rooms[room]["players"])
        })
app.mount("/", StaticFiles(directory="static", html=True), name="static")