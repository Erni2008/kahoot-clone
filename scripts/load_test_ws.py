#!/usr/bin/env python3
import argparse
import asyncio
import json
import statistics
import time
import urllib.parse
import urllib.request

import websockets


DEFAULT_QUIZ = "Механики и экономика"


def create_room(base_url: str, quiz: str) -> str:
    quiz_path = urllib.parse.quote(quiz, safe="")
    with urllib.request.urlopen(f"{base_url}/create-room/{quiz_path}") as response:
        payload = json.loads(response.read().decode("utf-8"))
    pin = payload.get("pin")
    if not pin:
        raise RuntimeError(f"Failed to create room: {payload}")
    return str(pin)


def build_ws_base(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}"


async def wait_for_type(ws, expected_type: str, timeout: float):
    deadline = time.perf_counter() + timeout
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for {expected_type}")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        data = json.loads(raw)
        if data.get("type") == expected_type:
            return data


async def player_session(ws_base: str, pin: str, username: str, start_barrier: asyncio.Event, results: dict):
    url = f"{ws_base}/ws/player/{pin}/{urllib.parse.quote(username, safe='')}"
    async with websockets.connect(url, max_size=2**20) as ws:
        await wait_for_type(ws, "waiting", 10)
        await wait_for_type(ws, "team_assignment", 10)
        await start_barrier.wait()

        question = await wait_for_type(ws, "question", 15)
        results["question_times"].append(time.perf_counter())
        results["question_type"] = question.get("question_type")

        if question.get("question_type") == "wordle":
            word_length = int(question.get("word_length") or 1)
            payload = {"type": "answer", "value": "а" * word_length}
        elif question.get("question_type") == "mcq":
            payload = {"type": "answer", "selected": 0}
        elif question.get("question_type") == "numeric":
            payload = {"type": "answer", "value": "0"}
        else:
            payload = {"type": "answer", "value": "test"}

        await ws.send(json.dumps(payload))
        await wait_for_type(ws, "reveal", 20)
        results["reveal_times"].append(time.perf_counter())
        await wait_for_type(ws, "leaderboard", 20)
        results["leaderboard_times"].append(time.perf_counter())


async def main():
    parser = argparse.ArgumentParser(description="WebSocket load test for kahoot_clone")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--players", type=int, default=50)
    parser.add_argument("--quiz", default=DEFAULT_QUIZ)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    ws_base = build_ws_base(base_url)
    pin = create_room(base_url, args.quiz)

    host_url = f"{ws_base}/ws/host/{pin}/HOST"
    start_barrier = asyncio.Event()
    results = {
        "question_times": [],
        "reveal_times": [],
        "leaderboard_times": [],
        "question_type": None,
    }

    print(f"Room: {pin}")
    print(f"Quiz: {args.quiz}")
    print(f"Players: {args.players}")

    async with websockets.connect(host_url, max_size=2**20) as host_ws:
        player_tasks = [
            asyncio.create_task(player_session(ws_base, pin, f"load_{idx:03d}", start_barrier, results))
            for idx in range(args.players)
        ]

        await asyncio.sleep(2.0)
        await host_ws.send(json.dumps({"type": "start"}))
        t0 = time.perf_counter()
        start_barrier.set()

        await asyncio.gather(*player_tasks)
        t_done = time.perf_counter()

    question_offsets = [t - t0 for t in results["question_times"]]
    reveal_offsets = [t - t0 for t in results["reveal_times"]]
    leaderboard_offsets = [t - t0 for t in results["leaderboard_times"]]

    def fmt(values):
        return {
            "min": round(min(values), 3),
            "avg": round(statistics.mean(values), 3),
            "p95": round(sorted(values)[max(0, int(len(values) * 0.95) - 1)], 3),
            "max": round(max(values), 3),
        }

    print()
    print(f"Question type: {results['question_type']}")
    print(f"Question delivery: {fmt(question_offsets)}")
    print(f"Reveal delivery: {fmt(reveal_offsets)}")
    print(f"Leaderboard delivery: {fmt(leaderboard_offsets)}")
    print(f"Total scenario time: {round(t_done - t0, 3)}s")


if __name__ == "__main__":
    asyncio.run(main())
