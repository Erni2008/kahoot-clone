from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
import random
import string
import time
import asyncio
import json
from typing import Dict
from difflib import SequenceMatcher
from collections import deque

import re
import html

# =====================
# Answer normalization & aliases (ANTI-DISPUTE)
# =====================
# You can extend this map over time. Keys and values should be written in a human way;
# both sides will be normalized internally.
ANSWER_ALIASES = {
    # examples (extend as needed):
    # "симба": "саблезуб",
    # "бастик": "бастион",
}

_PUNCT_RE = re.compile(r"[^0-9a-zа-яё\s]+", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+", re.UNICODE)
# =====================
# Fuzzy matching (typo tolerance)
# =====================
# If RapidFuzz is available it will be used (faster + better for typos).
# Otherwise we fallback to Python's SequenceMatcher.
try:
    from rapidfuzz import fuzz as _rf_fuzz  # type: ignore
    from rapidfuzz.distance import Levenshtein as _rf_lev  # type: ignore
    _HAS_RAPIDFUZZ = True
except Exception:
    _rf_fuzz = None
    _rf_lev = None
    _HAS_RAPIDFUZZ = False

# Global similarity threshold (0..100). Higher = stricter.
# For very short answers we tighten the threshold automatically.
FUZZY_THRESHOLD_DEFAULT = 88
WS_SEND_TIMEOUT = 2.0
ADMIN_LOG_LIMIT = 2000
WARNING_PENALTY_POINTS = 500
WORDLE_HINT_COST = 350

def _similarity_percent(a: str, b: str) -> int:
    if not a or not b:
        return 0
    if _HAS_RAPIDFUZZ and _rf_fuzz is not None:
        # token_set_ratio is tolerant to extra spaces/word order
        return int(_rf_fuzz.token_set_ratio(a, b))
    # Fallback (0..100)
    return int(round(SequenceMatcher(None, a, b).ratio() * 100))


# Levenshtein distance helper (edit distance)
def _levenshtein_distance(a: str, b: str, max_dist: int | None = None) -> int:
    """Compute Levenshtein distance; if max_dist is set, may early-exit when distance exceeds it."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    # RapidFuzz fast path
    if _HAS_RAPIDFUZZ and _rf_lev is not None:
        try:
            if max_dist is None:
                return int(_rf_lev.distance(a, b))
            return int(_rf_lev.distance(a, b, score_cutoff=max_dist))
        except Exception:
            pass

    # Python DP (optimized for small strings)
    if len(a) < len(b):
        a, b = b, a
    # now len(a) >= len(b)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        # optional early-exit band
        row_min = current[0]
        for j, cb in enumerate(b, start=1):
            ins = current[j - 1] + 1
            dele = previous[j] + 1
            sub = previous[j - 1] + (0 if ca == cb else 1)
            val = ins if ins < dele else dele
            if sub < val:
                val = sub
            current.append(val)
            if val < row_min:
                row_min = val
        previous = current
        if max_dist is not None and row_min > max_dist:
            # can't be <= max_dist anymore
            return max_dist + 1
    return previous[-1]

def is_fuzzy_match(user_norm: str, correct_norms: list[str], threshold: int | None = None) -> bool:
    """Return True if user's normalized answer is close enough to any correct normalized answer."""
    if not user_norm:
        return False

    # Dynamic threshold: short strings require higher similarity to avoid false positives
    base = FUZZY_THRESHOLD_DEFAULT if threshold is None else int(threshold)
    ulen = len(user_norm)
    if ulen <= 3:
        base = max(base, 96)
    elif ulen <= 5:
        base = max(base, 92)

    for c in correct_norms:
        if not c:
            continue
        if user_norm == c:
            return True

        # Primary: similarity threshold
        if _similarity_percent(user_norm, c) >= base:
            return True

        # Fallback: allow small edit distance for single-token, same-length-ish answers
        # Helps for common typos like duplicated/missed letters.
        max_edits = 1
        max_len = max(len(user_norm), len(c))
        if max_len >= 9:
            max_edits = 2
        if max_len >= 14:
            max_edits = 3

        # Only apply edit-distance fallback when the length difference is reasonable
        if abs(len(user_norm) - len(c)) <= max_edits:
            if _levenshtein_distance(user_norm, c, max_dist=max_edits) <= max_edits:
                return True
    return False

def normalize_answer(text: str) -> str:
    """Normalize free-text answers for tolerant matching."""
    if text is None:
        return ""
    s = str(text).strip().lower()
    # Cyrillic normalization
    s = s.replace("ё", "е")
    # Remove punctuation/symbols, keep letters/numbers/spaces
    s = _PUNCT_RE.sub(" ", s)
    s = _SPACE_RE.sub(" ", s).strip()
    return s

def apply_alias(norm_text: str) -> str:
    """Map common nicknames/shortcuts to canonical answers."""
    if not norm_text:
        return norm_text
    # normalize alias keys once on lookup
    for k, v in ANSWER_ALIASES.items():
        if normalize_answer(k) == norm_text:
            return normalize_answer(v)
    return norm_text


def is_valid_wordle_guess(guess: str, target: str, question: dict) -> bool:
    if not guess:
        return False
    return True


def evaluate_wordle_guess(target: str, guess: str) -> list[str]:
    """Return per-letter statuses: correct, present, absent."""
    target_chars = list(target)
    guess_chars = list(guess)
    statuses = ["absent"] * len(guess_chars)
    remaining: dict[str, int] = {}

    for index, char in enumerate(target_chars):
        if index < len(guess_chars) and guess_chars[index] == char:
            statuses[index] = "correct"
        else:
            remaining[char] = remaining.get(char, 0) + 1

    for index, char in enumerate(guess_chars):
        if statuses[index] == "correct":
            continue
        if remaining.get(char, 0) > 0:
            statuses[index] = "present"
            remaining[char] -= 1

    return statuses


app = FastAPI()

from fastapi import Request
from fastapi.responses import Response

@app.middleware("http")
async def add_ngrok_header(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["ngrok-skip-browser-warning"] = "true"
    return response

rooms: Dict[str, dict] = {}
admin_logs = deque(maxlen=ADMIN_LOG_LIMIT)
admin_clients: Dict[str, dict] = {}

TEAM_PRESETS = [
    {"name": "Изумрудные", "color": "#22c55e"},
    {"name": "Бирюзовые", "color": "#14b8a6"},
    {"name": "Янтарные", "color": "#f59e0b"},
    {"name": "Рубиновые", "color": "#ef4444"},
    {"name": "Сапфировые", "color": "#3b82f6"},
    {"name": "Аметистовые", "color": "#8b5cf6"},
]


def build_room_quiz(quiz_name: str):
    quiz = list(QUIZZES.get(quiz_name, []))
    if len(quiz) <= 1:
        return quiz

    first_question = quiz[0]
    remaining_questions = quiz[1:]
    random.shuffle(remaining_questions)
    return [first_question, *remaining_questions]


def compute_team_leaderboard(room_data: dict):
    if not room_data.get("team_mode"):
        return []

    teams = room_data.get("teams") or {}
    if not teams:
        return []

    scores = room_data.get("scores") or {}
    team_totals = []

    for team in teams:
        members = [member for member in team.get("members", []) if member in scores]
        total = sum(int(scores.get(member, 0)) for member in members)
        team_totals.append({
            "id": team.get("id"),
            "name": team.get("name") or f"Команда {team.get('id', '?')}",
            "members": members,
            "score": total,
        })

    team_totals.sort(key=lambda item: item["score"], reverse=True)
    return team_totals


def add_player_timeline_event(room_data: dict, username: str, event: str, **details):
    if not username or username == "HOST":
        return
    timelines = room_data.setdefault("player_timelines", {})
    items = timelines.setdefault(username, [])
    items.append({
        "ts": time.time(),
        "event": event,
        "question_index": int(room_data.get("question_index", 0) or 0),
        **details,
    })
    if len(items) > 40:
        del items[:-40]


def compute_player_suspicion(metric: dict, flags: dict | None = None, late_joiner: bool = False) -> dict:
    metric = metric if isinstance(metric, dict) else {}
    flags = flags if isinstance(flags, dict) else {}
    offline_seconds = float(metric.get("offline_total_ms", 0) or 0) / 1000
    if metric.get("offline_started_at"):
        offline_seconds += max(0.0, time.time() - float(metric.get("offline_started_at")))
    offline_before_answer_seconds = float(metric.get("offline_before_answer_ms", 0) or 0) / 1000
    tab_switches = int(metric.get("tab_switches", 0) or 0)
    warning_count = int(flags.get("warning_count", 0) or 0)
    penalties_applied = int(flags.get("penalties_applied", 0) or 0)

    score = 0
    score += min(40, int(round(offline_before_answer_seconds * 2)))
    score += min(25, tab_switches * 8)
    score += min(20, warning_count * 4)
    score += min(10, penalties_applied * 5)
    if late_joiner:
        score += 5

    if score >= 40:
        risk = "high"
    elif score >= 15:
        risk = "medium"
    else:
        risk = "low"

    return {
        "score": min(score, 100),
        "risk": risk,
        "offline_seconds": round(offline_seconds, 2),
        "offline_before_answer_seconds": round(offline_before_answer_seconds, 2),
        "tab_switches": tab_switches,
    }


async def broadcast_team_update(room: str):
    if room not in rooms:
        return
    room_data = rooms[room]
    payload = {
        "type": "team_assignment",
        "enabled": bool(room_data.get("team_mode")),
        "team_size": int(room_data.get("team_size", 2)),
        "teams": room_data.get("teams", []),
        "team_leaderboard": compute_team_leaderboard(room_data),
    }
    await broadcast(room, payload)


def build_room_snapshot(room: str, room_data: dict) -> dict:
    players = room_data.get("players", {})
    answers = room_data.get("answers", {})
    disconnected = room_data.get("disconnected", {})
    scores = room_data.get("scores", {})
    appeals = room_data.get("appeals", [])
    question_history = room_data.get("question_history", [])
    current_question_metrics = room_data.get("current_question_metrics", {})
    player_flags = room_data.get("player_flags", {})
    late_joiners = room_data.get("late_joiners", {})
    current_answers = []
    for username, answer_data in answers.items():
        if not isinstance(answer_data, dict):
            continue
        flags = player_flags.get(username, {}) if isinstance(player_flags.get(username), dict) else {}
        metric = current_question_metrics.get(username, {}) if isinstance(current_question_metrics.get(username), dict) else {}
        suspicion = compute_player_suspicion(metric, flags, bool(late_joiners.get(username)))
        entry = {
            "username": username,
            "value": answer_data.get("value"),
            "selected": answer_data.get("selected"),
            "selected_list": answer_data.get("selected_list"),
            "remaining": answer_data.get("remaining"),
            "finalized": bool(answer_data.get("finalized")),
            "solved": bool(answer_data.get("solved")),
            "offline_before_answer_seconds": float(answer_data.get("offline_before_answer_seconds", 0) or 0),
            "suspicion_score": suspicion["score"],
            "risk": suspicion["risk"],
            "manual_suspicious": bool(metric.get("manual_suspicious")),
            "manual_suspicious_note": metric.get("manual_suspicious_note") or "",
        }
        attempts = answer_data.get("attempts")
        if isinstance(attempts, list):
            entry["attempts"] = attempts
        current_answers.append(entry)
    pending_appeals = [
        {
            "id": index,
            "ts": entry.get("ts"),
            "username": entry.get("username"),
            "question_index": entry.get("question_index"),
            "question_type": entry.get("question_type"),
            "question": entry.get("question"),
            "answer": entry.get("answer"),
            "reason": entry.get("reason"),
            "points": entry.get("points"),
            "correct": entry.get("correct"),
            "resolved": bool(entry.get("resolved")),
            "resolution": entry.get("resolution"),
            "delta": entry.get("delta"),
        }
        for index, entry in enumerate(appeals)
        if isinstance(entry, dict) and "question_index" in entry
    ]
    player_history = {}
    player_timelines = room_data.get("player_timelines", {})
    for username in players.keys():
        history_items = []
        for item in question_history[-15:]:
            users_answers = item.get("users_answers", {}) or {}
            points_awarded = item.get("points_awarded", {}) or {}
            if username not in users_answers and username not in points_awarded:
                continue
            history_items.append({
                "question_index": item.get("question_index"),
                "question": item.get("question"),
                "question_type": item.get("question_type"),
                "answer": users_answers.get(username),
                "points_awarded": points_awarded.get(username, 0),
                "response_time_ms": ((item.get("response_times_ms") or {}).get(username)),
                "tab_switches": ((item.get("tab_switches") or {}).get(username, 0)),
                "offline_before_answer_seconds": round(float(((item.get("offline_before_answer_ms") or {}).get(username, 0) or 0)) / 1000, 2),
                "offline_seconds": round(float(((item.get("offline_ms") or {}).get(username, 0) or 0)) / 1000, 2),
            })
        player_history[username] = history_items
    current_question_monitor = []
    now_ts = time.time()
    for name in players.keys():
        if name == "HOST":
            continue
        metric = current_question_metrics.get(name, {}) if isinstance(current_question_metrics.get(name), dict) else {}
        answer_data = answers.get(name, {}) if isinstance(answers.get(name), dict) else {}
        answer_preview = None
        if answer_data.get("attempts") is not None:
            answer_preview = answer_data.get("attempts")
        elif answer_data.get("selected_list") is not None:
            answer_preview = answer_data.get("selected_list")
        elif answer_data.get("selected") is not None:
            answer_preview = answer_data.get("selected")
        else:
            answer_preview = answer_data.get("value")
        flags = player_flags.get(name, {}) if isinstance(player_flags.get(name), dict) else {}
        offline_total_ms = float(metric.get("offline_total_ms", 0) or 0)
        offline_started_at = metric.get("offline_started_at")
        if name in disconnected and offline_started_at:
            offline_total_ms += max(0.0, now_ts - float(offline_started_at)) * 1000
        suspicion = compute_player_suspicion(metric, flags, bool(late_joiners.get(name)))
        current_question_monitor.append({
            "username": name,
            "connected": name not in disconnected,
            "answered": name in answers,
            "finalized": bool(answer_data.get("finalized")) if isinstance(answer_data, dict) else False,
            "answer": answer_preview,
            "response_time_ms": metric.get("response_time_ms"),
            "answered_at": metric.get("answered_at"),
            "tab_switches": int(metric.get("tab_switches", 0) or 0),
            "offline_seconds": round(offline_total_ms / 1000, 2),
            "offline_before_answer_seconds": suspicion["offline_before_answer_seconds"],
            "warning_count": int(flags.get("warning_count", 0) or 0),
            "penalties_applied": int(flags.get("penalties_applied", 0) or 0),
            "late_joiner": bool(late_joiners.get(name)),
            "suspicion_score": suspicion["score"],
            "risk": suspicion["risk"],
            "manual_suspicious": bool(metric.get("manual_suspicious")),
            "manual_suspicious_note": metric.get("manual_suspicious_note") or "",
        })
    return {
        "room": room,
        "quiz": room_data.get("quiz"),
        "status": room_data.get("status"),
        "question_index": int(room_data.get("question_index", 0)),
        "players_count": len(players),
        "connected_players": len([name for name in players.keys() if name not in disconnected]),
        "answers_count": len(answers),
        "paused": bool(room_data.get("paused")),
        "team_mode": bool(room_data.get("team_mode")),
        "teams_count": len(room_data.get("teams", [])),
        "current_view": room_data.get("current_view"),
        "players": [
            {
                "username": name,
                "score": int(scores.get(name, 0)),
                "connected": name not in disconnected,
                "answered": name in answers,
                "finalized": bool((answers.get(name) or {}).get("finalized")) if isinstance(answers.get(name), dict) else False,
                "team_id": room_data.get("team_assignments", {}).get(name),
                "warning_count": int(((player_flags.get(name) or {}).get("warning_count", 0)) if isinstance(player_flags.get(name), dict) else 0),
                "penalties_applied": int(((player_flags.get(name) or {}).get("penalties_applied", 0)) if isinstance(player_flags.get(name), dict) else 0),
                "late_joiner": bool(late_joiners.get(name)),
                "suspicion_score": compute_player_suspicion(
                    current_question_metrics.get(name, {}) if isinstance(current_question_metrics.get(name), dict) else {},
                    player_flags.get(name, {}) if isinstance(player_flags.get(name), dict) else {},
                    bool(late_joiners.get(name)),
                )["score"],
                "risk": compute_player_suspicion(
                    current_question_metrics.get(name, {}) if isinstance(current_question_metrics.get(name), dict) else {},
                    player_flags.get(name, {}) if isinstance(player_flags.get(name), dict) else {},
                    bool(late_joiners.get(name)),
                )["risk"],
                "manual_suspicious": bool(((current_question_metrics.get(name) or {}).get("manual_suspicious")) if isinstance(current_question_metrics.get(name), dict) else False),
                "manual_suspicious_note": (((current_question_metrics.get(name) or {}).get("manual_suspicious_note")) if isinstance(current_question_metrics.get(name), dict) else "") or "",
            }
            for name in players.keys()
        ],
        "teams": room_data.get("teams", []),
        "pending_appeals": pending_appeals,
        "current_answers": current_answers,
        "current_question_monitor": current_question_monitor,
        "question_history": [
            {
                "question_index": item.get("question_index"),
                "question": item.get("question"),
                "question_type": item.get("question_type"),
                "answered_count": item.get("answered_count"),
                "points": item.get("points"),
                "offline_before_answer_ms": item.get("offline_before_answer_ms", {}),
                "response_times_ms": item.get("response_times_ms", {}),
                "tab_switches": item.get("tab_switches", {}),
                "offline_ms": item.get("offline_ms", {}),
            }
            for item in question_history[-10:]
        ],
        "player_history": player_history,
        "player_timelines": {
            username: list(player_timelines.get(username, []))[-20:]
            for username in players.keys()
            if username != "HOST"
        },
        "latest_logs": [
            entry for entry in list(admin_logs)[-50:]
            if entry.get("room") in {None, room}
        ],
    }


def build_server_snapshot() -> dict:
    return {
        "rooms": [build_room_snapshot(room, room_data) for room, room_data in rooms.items()],
        "logs": list(admin_logs),
    }


async def safe_send_admin(client_id: str, message: dict) -> bool:
    client = admin_clients.get(client_id)
    if not client:
        return False
    websocket = client.get("ws")
    lock = client.get("lock")
    if websocket is None or lock is None:
        return False
    try:
        async with lock:
            await asyncio.wait_for(websocket.send_json(message), timeout=WS_SEND_TIMEOUT)
        return True
    except Exception:
        admin_clients.pop(client_id, None)
        return False


async def broadcast_admin(message: dict):
    if not admin_clients:
        return
    await asyncio.gather(*[
        safe_send_admin(client_id, message)
        for client_id in list(admin_clients.keys())
    ], return_exceptions=True)


def log_event(event_type: str, room: str | None = None, **details):
    entry = {
        "ts": round(time.time(), 3),
        "event": event_type,
        "room": room,
        "details": details,
    }
    admin_logs.append(entry)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(broadcast_admin({"type": "admin_log", "entry": entry}))
    if room and room in rooms:
        snapshot = build_room_snapshot(room, rooms[room])
        loop.create_task(broadcast_admin({"type": "room_snapshot", "room": room, "snapshot": snapshot}))


def stringify_export_value(value) -> str:
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("guess") is not None:
                parts.append(str(item.get("guess")))
            else:
                parts.append(str(item))
        return " | ".join(parts)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def remove_player_from_teams(room_data: dict, username: str):
    if not room_data.get("team_mode"):
        return
    teams = []
    for team in room_data.get("teams", []):
        members = [member for member in team.get("members", []) if member != username]
        if not members:
            continue
        teams.append({
            **team,
            "members": members,
        })
    room_data["teams"] = teams
    room_data.get("team_assignments", {}).pop(username, None)


def apply_warning_count(room: str, room_data: dict, username: str, new_count_raw) -> dict:
    try:
        new_count = max(0, int(new_count_raw))
    except Exception:
        return {"ok": False, "error": "warning count must be an integer"}

    if username not in room_data.get("scores", {}):
        return {"ok": False, "error": "Player not found"}

    flags = room_data.setdefault("player_flags", {})
    player_flags = flags.setdefault(username, {"warning_count": 0, "penalties_applied": 0})
    old_count = int(player_flags.get("warning_count", 0) or 0)
    old_over = max(0, old_count - 5)
    new_over = max(0, new_count - 5)
    extra_penalties = max(0, new_over - old_over)
    penalty_points = extra_penalties * WARNING_PENALTY_POINTS

    player_flags["warning_count"] = new_count
    if extra_penalties:
        player_flags["penalties_applied"] = int(player_flags.get("penalties_applied", 0) or 0) + extra_penalties
        room_data["scores"][username] = int(room_data["scores"].get(username, 0)) - penalty_points

    packet = {
        "type": "anti_cheat_warning",
        "warning_count": new_count,
        "visible_count": min(new_count, 5),
        "penalty_points": penalty_points,
    }
    player_ws = room_data.get("players", {}).get(username)
    if player_ws is not None:
        asyncio.create_task(safe_send_json(room_data, player_ws, packet, f"player:{username}"))

    return {
        "ok": True,
        "username": username,
        "warning_count": new_count,
        "penalty_points": penalty_points,
    }


def build_team_meta(team_index: int):
    preset = TEAM_PRESETS[team_index % len(TEAM_PRESETS)]
    cycle = (team_index // len(TEAM_PRESETS)) + 1
    name = preset["name"] if cycle == 1 else f"{preset['name']} {cycle}"
    return {
        "name": name,
        "color": preset["color"],
    }

QUIZZES = {
    "Механики и экономика": [
        {
  "type": "numeric",
  "question": "Сколько всего нетленных в игре?", 
  "correct": 5,
  "time": 30,
  "points": 500
}, 
        {
  "type": "numeric",
  "question": "Сколько надо потратить очков мастерства чтобы получить полный доступ КО ВСЕМ ячейкам с суицидальным мастерством?",
  "correct": 17,
  "time": 30,
  "points": 1300
},
{
  "type": "mcq",
  "question": "Сколько стоит 6* кристалл в магазине за артефакты вторжения?",
  "answers": [
    "51000",
    "54000",
    "61000",
    "64000"
  ],
  "correct": 1,
  "time": 30,
  "points": 600
},
        {
  "type": "mcq",
  "question": "Какое АКТИВНОЕ усиление или ослабление не связано с мастерством?",
  "answers": [
    "Кровь",
    "Точность",
    "Жестокость",
    "Нет правильного ответа"
  ],
  "correct": 2,
  "time": 30,
  "points": 1000
},
        
        {
  "type": "mcq",
  "question": "Какой шанс выпадения 7* чемпиона с кристалла совершенства (от 4 до 7 звезд)",
  "answers": [
    "1.25",
    "1.60",
    "2.5",
    "4"
  ],
  "correct": 0,
  "time": 30,
  "points": 800
},
        {
  "type": "numeric",
  "question": "Сколько ботов стоит в Мире Легенд?",

  "correct": 11,
  "time": 40,
  "points": 800
},
        {
  "type": "mcq",
  "question": "Кем приходится Карина Грандмастеру в сюжете игры?",
  "answers": ["Дочь", "Племянница", "Сестра", "Помощница"],
  "correct": 1,
  "time": 25,
  "points": 1200
},
{
    "type": "mcq",
    "question": "Без чего игрок не сможет пройти босса Церастеса?",
    "answers": [
        "Без 7★ чемпиона",
        "Без чемпиона с реликвией",
        "Без мистического чемпиона",
        "Без космического чемпиона"
    ],
    "correct": 1,
    "time": 35,
    "points": 1400
},
        {
    "type": "mcq",
    "question": "Какой вариант наград не соответствует за ИССЛЕДОВАНИЯ всей 'бесконечности боли'?",
    "answers": ["1 золото", "6* повышалка с 3 на 4", "Аватар","100 6* серых коронных камней"],
    "correct": 3,
    "time": 40,
    "points": 800
    },
        {
    "type": "mcq",
    "question": "Какой предмет появлялся чаще всего в диалогах игры?",
    "answers": ["Стул", "Лампа", "Стол","Табуретка"],
    "correct": 0,
    "time": 30,
    "points": 1000
    },
        {
    "type": "numeric",
    "question": "Сколько надо элементов мастерства чтобы прокачать 'Интеллект' на максимум?",
    "correct": 5,
    "time": 30,
    "points": 1400
    },
        {
    "type": "mcq",
    "question": "Какой чемпион не был в базовом пуле чемпионов с выходом 7* кристалла?",
    "answers": ["Горр", "Сокол", "Джо Фиксит","Эбони Мо"],
    "correct": 3,
    "time": 30,
    "points": 1100
    },
    {
    "type": "numeric",
    "question": "Сколько надо фрагментов катализатора чтобы собрался базовый ёж 7 категории",
    "correct": 63000,
    "time": 30,
    "points": 1000
    },
    {
    "type": "numeric",
    "question": "Сколько дают серы за ислледование 3 акта ( в счёт только за исследование )",
    "correct": 250,
    "time": 30,
    "points": 700
    },
        {
    "type": "mcq",
    "question": "Какой был первый персонаж который мог наносить разрывы?",
    "answers": ["Ринтра", "Скорп", "Тигра","Сасквоч"],
    "correct": 2,
    "time": 30,
    "points": 1100
    },
    
{
        "type": "mcq",
        "question": "В каком варианте дают награды не такие как в других трёх?",
        "answers": ["Дедпулуза", "Атака Альтрона", "Время Пауков", "Заражение"],
        "correct": 1,
        "time": 30,
        "points": 1200
        },
{
        "type": "numeric",
        "question": "Сколько в большей части вариантах заданий?",
        "correct": 6,
        "time": 20,
        "points": 1000
        },
{
            "type": "mcq",
            "question": "Сколько всего классов чемпионов в игре?",
            "answers": ["5", "6", "7", "8"],
            "correct": 2,
            "time": 30,      
            "points": 100
        },

        {
            "type": "text",
            "question": "Какой самый перстижный чемпион в MCOC?",
            "correct": "Эволюционер",
            "time": 30,      
            "points": 1000

        },

        {
            "type": "mcq",
            "question": "Сколько энергии восстанавливается за 1 час?",
            "answers": ["6", "8", "10", "12"],
            "correct": 2,
            "time": 30,
            "points": 800
        },


        ],

    "Синергии": [
        ],

    "Некрополис": [

                
        ],
    "Эпоха Боли": [
        {
            "type": "mcq",
            "question": "Сколько ударов у 2 спецухи Ареса?",
            "answers": ["6", "7", "8", "9"],
            "correct": 0,
            "time": 30,      
            "points": 1750
        },
        {
    "type": "numeric",
    "question": "Сколько пассивных эффектов Бессмертия снимается у Ареса при прерывании его атаки?",
    "correct": 2,
    "time": 30,
    "points": 800
},
{
    "type": "numeric",
    "question": "Сколько пассивных эффектов Бессмертия снимается у Ареса, если от удара его специальной атаки уклоняются?",
    "correct": 2,
    "time": 35,
    "points": 1800
},
        {
    "type": "numeric",
    "question": "Сколько всего противников в «Эпохе Боли»?",
    "correct": 25,
    "time": 40,
    "points": 1500
},   
{
    "type": "numeric",
    "question": "Сколько пассивных эффектов Бессмертия суммарно имеет Арес за бой?",
    "correct": 99,
    "time": 30,
    "points": 1800
},
{
    "type": "numeric",
    "question": "Сколько фаз имеет Арес в «Эпохе Боли»?",
    "correct": 5,
    "time": 25,
    "points": 1000
},
{
    "type": "numeric",
    "question": "Сколько энергии стоит одна клетка продвижения в «Эпохе Боли»?",
    "correct": 3,
    "time": 25,
    "points": 900
},
{
    "type": "numeric",
    "question": "Правда ли, что на каждой ветке «Эпохи Боли» присутствуют все 6 классов чемпионов? (напиши ДА или НЕТ)",

    "correct": "ДА",
    "time": 30,
    "points": 1000
},
{
    "type": "numeric",
    "question": "Сколько пассивных эффектов Бессмертия снимается у Ареса, если от удара его специальной атаки уклоняются?",
    "correct": 2,
    "time": 35,
    "points": 1800
},
{
    "type": "mcq",
    "question": "У какого персонажа на ветке стоит усиление «Так держать» (стенка как у Змея)?",
    "answers": [
        "Железный Кулак",
        "Натиск",
        "Анигилус",
        "Эйгон"
    ],
    "correct": 0,
    "time": 35,
    "points": 1300
},
{
    "type": "mcq",
    "question": "Без чего игрок НЕ сможет пройти «Эпоху Боли»?",
    "answers": [
        "Без 6★ чемпиона",
        "Без 7★ чемпиона",
        "Без реликвий",
        "Без мастерств"
    ],
    "correct": 1,
    "time": 35,
    "points": 1200
},



    ],

    "Бесконечность боли":[

    ],

    "Горн":[

    ],
    "Колесо судьбы":[

                {
        "type": "mcq",
        "question": "Какого персонажа точно НЕТУ в первом колесе?",
        "answers": ["Чаровница", "Ртуть", "Самурай", "Меченый"],
        "correct": 1,
        "time": 35,
        "points": 1200
        },
        {
    "type": "mcq",
    "question": "Какой тег имеет чемпион, который находится в первом колесе сразу после Меченого?",
    "answers": [
        "Гидра",
        "Паучья вселенная",
        "Мутант",
        "Злодеи"
    ],
    "correct": 1,
    "time": 35,
    "points": 1200
},
{
    "type": "numeric",
    "question": "Сколько чемпионов находится в первом колесе?",
    "correct": 13,
    "time": 30,
    "points": 1200
},
{
    "type": "numeric",
    "question": "Чемпиона какого года нужно использовать, чтобы зайти в первое колесо?",
    "correct": 2024,
    "time": 30,
    "points": 1200
},
{
    "type": "mcq",
    "question": "Установите правильную последовательность расположения чемпионов в первом колесе:",
    "answers": [
        "Аркада → Леди смертельный удар → Факел → Лидер",
        "Леди смертельный удар → Аркада → Лидер → Факел",
        "Леди смертельный удар → Факел → Аркада → Лидер",
        "Леди смертельный удар → Лидер → Аркада → Факел"
    ],
    "correct": 1,
    "time": 40,
    "points": 1500
},
        
    ],

    "Чемпионы и теги":[
        {
  "type": "mcq",
  "question": "Какой чемпион НЕ является Глаштаем в синергии с Галаном?",
  "answers": [
    "Король Грут",
    "Идущий по воздуху",
    "Терракс",
    "КПГ"
    
  ], 
  "correct": 0,
  "time": 30,
  "points": 1000
},
{
  "type": "mcq",
  "question": "Какой чемпион НЕ является 'Всадником Апокалипсиса' в синергии с Апокалипсисом?",
  "answers": [
    "Шторм (Пирамида Х)",
    "Псайлок",
    "Гамбит",
    "Колосс"
    
  ], 
  "correct": 3,
  "time": 30,
  "points": 1000
},
{
  "type": "mcq",
  "question": "Какой чемпион НЕ может парировать Арнима-Зола?",
  "answers": [
    "Джек Фонарь",
    "Капитан Америка (Сэм Уилсон)",
    "Солварх",
    "Бастион"
    
  ], 
  "correct": 1,
  "time": 30,
  "points": 1000
},
{
  "type": "mcq",
  "question": "Какой чемпион МОЖЕТ уклониться от Фотон при её режиме?",
  "answers": [
    "Ослепительная",
    "Спирать",
    "Лю Матрикс",
    "Тигра"
    
  ], 
  "correct": 0,
  "time": 30,
  "points": 1000
},
        {
  "type": "mcq",
  "question": "Какой чемпион имеет иммунитет к пеплу?",
  "answers": [
    "Серебрянный сёрфер",
    "Каролина Дин",
    "Супер-скрулл",
    "Натиск"
  ], 
  "correct": 2,
  "time": 30,
  "points": 1000
},
{
  "type": "mcq",
  "question": "Какой чемпион НЕ имеет способность к использования тяжелой атаки во время комбо атаки (пример - шелк )?",
  "answers": [
    "Каролина Дин",
    "Чаровница",
    "Тор Джейн Фостер",
    "Серебряный Соболь"
  ], 
  "correct": 3,
  "time": 30,
  "points": 1000
}, 
{
  "type": "mcq",
  "question": "Какой чемпион НЕ имеет способность связанную с полной луной?",
  "answers": [
    "Лунный рыцарь",
    "Мистер найт",
    "Оборотень",
    "Дракула"
  ], 
  "correct": 3,
  "time": 30,
  "points": 1000
},
        {
  "type": "numeric",
  "question": "Сколько ударов наносит третяя специальная атака Окойе?",
  "correct": 8,
  "time": 30,
  "points": 1000
},
{
  "type": "numeric",
  "question": "Сколько ударов наносит вторая специальная атака Уорлока",
  "correct": 10,
  "time": 30,
  "points": 1000
},

{
  "type": "mcq",
  "question": "Кто из перечисленных чемпионов НЕ входил в состав Иллюминатов по тегу?",
  "answers": [
    "Чёрный гром",
    "Халк",
    "Росомаха",
    "Зверь"
  ],
  "correct": 2,
  "time": 30,
  "points": 700
},
{
  "type": "mcq",
  "question": "Кто из перечисленных чемпионов не относится к тегу #X-Force?",
  "answers": [
    "Циклоп",
    "Магнето",
    "Шторм",
    "Джин Грей"
  ],
  "correct": 0,
  "time": 30,
  "points": 1300
},
        {
    "type": "mcq",
    "question": "Кто из перечисленных НЕ входит в тег #Зловещая шестёрка?",
    "answers": [
        "Шокер",
        "Зелёный гоблин",
        "Патриот",
        "Мистер Негатив"
    ],
    "correct": 3,
    "time": 35,
    "points": 1000
},
        {
  "type": "mcq",
  "question": "Сколько вариантов Карателя (каких можно получить) есть в игре?",
  "answers": ["2", "3", "4", "5"],
  "correct": 1,
  "time": 30,
  "points": 1300
},

{
    "type": "numeric",
    "question": "Сколько чемпионов относятся к симбиотам (УНИКАЛЬНЫЕ СИМБИОИДЫ не в счет!!)?",
    "correct": 12,
    "time": 90,
    "points": 2000
},
           {
    "type": "numeric",
    "question": "Сколько максимум удалей может накопить Ртуть?",
    "correct": 0,
    "time": 30,
    "points": 700
},
    {
    "type": "mcq",
    "question": "какой из мутантов не входит в тег #братство мутантов",
    "answers": ["Алая ведьма", "Магнето (ДОМ ИКС)", "Эмма Фрост", "Саурон"],
    "correct": 2,
    "time": 60,
    "points": 1000
},
    {
    "type": "numeric",
    "question": "Сколько копит зарядов воспламенения масакр?",
    "correct": 9,
    "time": 30,
    "points": 700
},
        {
    "type": "mcq",
    "question": "Как называется коронная способность у Гаморы",
    "answers": ["Самая опасная женщина в галактике", "Пронзающий меч", "Охотник на богов","Крушитель богов"],
    "correct": 0,
    "time": 30,
    "points": 700
    },
        {
    "type": "mcq",
    "question": "Сколько ударов в базовой комбо-серии (MLLLM) у Ртути?",
    "answers": ["12", "13", "14", "Нет правильного ответа"],
    "correct": 1,
    "time": 20,
    "points": 900
    },
        {
    "type": "numeric",
    "question": "Сколько зарядов ИСКАЖЕНИЯ РЕАЛЬНОСТИ может максимально получить Часовой?",
    "correct": 10,
    "time": 25,
    "points": 1500
        },
        {
        "type": "mcq",
        "question": "Какой чемпион стал первым с механикой «Постоянные заряды»?",
        "answers": ["Эйгон", "Корвус Глейв", "Нэмор", "Геркулес"],
        "correct": 0,
        "time": 35,
        "points": 1500
        },
                {
        "type": "mcq",
        "question": "Какой чемпион первым получил механику фазирования (Phase)?",
        "answers": ["Призрак", "Китти Прайд", "Мисти Найт", "Страйф"],
        "correct": 0,
        "time": 35,
        "points": 900
        },

        {
    "type": "mcq",
    "question": "Какой чемпион НЕ относится к тегу #Гидра?",
    "answers": ["Красный Череп", "Барон Земо", "Кроссбоунс", "Капитан Америка"],
    "correct": 3,
    "time": 35,
    "points": 1500
},
{
        "type": "mcq",
        "question": "Сколько секунд длится фазирование Китти Прайд?",
        "answers": ["1", "1.2", "1.3", "1.5"],
        "correct": 1,
        "time": 30,
        "points": 1300
        },
{
            "type": "mcq",
            "question": "Сколько ударов наносит базовая комбо-серия (MLLLM) у Тигры?",
            "answers": ["6", "7", "8", "9"],
            "correct": 1,
            "time": 25,      
            "points": 750
        },

        {
    "type": "mcq",
    "question": "Какой чемпион НЕ относится к гамма-мутантам?",
    "answers": ["Маэстро", "Гладиатор Халк", "Женщина Халк(Нетленная)", "Существо"],
    "correct": 3,
    "time": 35,
    "points": 800
},


        {
    "type": "mcq",
    "question": "Какое усиление Алая Ведьма НЕ может получить при нанесении критического удара?",
    "answers": ["Регенерация", "Ярость", "Жестокость", "Точность"],
    "correct": 3,
    "time": 30,
    "points": 900
},

        {
        "type": "mcq",
        "question": "Какой дебафф Алая Ведьма НЕ может наложить на противника?",
        "answers": ["Повреждение", "Пробитие брони", "Кража власти", "Слабость", "Оглушение", "Потрясение"],
    "correct": 5,
    "time": 35,
    "points": 1500
},
{
    "type": "numeric",
    "question": "Сколько максимум уникальных усилений может украсть Доктор Дум через специальную атаку?",
    "correct": 3,
    "time": 25,
    "points": 700
    },
    {
    "type": "mcq",
    "question": "У какого персонажа нет огнестрельного оружия?",
    "answers": ["Таскмастер", "Эволюционер", "Элена Белова","Ронан"],
    "correct": 3,
    "time": 30,
    "points": 1000
    },
    {
    "type": "mcq",
    "question": "Какой чемпион не относится к тегу #Герой?",
    "answers": ["Серебрянный самурай", "Феникс", "Мисти Найт","Нэмор"],
    "correct": 0,
    "time": 30,
    "points": 1000
    },
    {
    "type": "mcq",
    "question": "Правда ли, что Пыль и Железное сердце вышли в один и тот же день?",
    "answers": [
        "Да, они вышли одновременно",
        "Нет, они вышли в одном месяце, но в разные даты",
        "Нет, между их релизами прошёл год",
        "Железное сердце вышла позже Пыли"
    ],
    "correct": 1,
    "time": 30,
    "points": 1000
    },
    {
    "type": "numeric",
    "question": "Сколько всего видов Пауков в игре?",
    "correct": 14,
    "time": 45,
    "points": 1000
    },
    {
    "type": "mcq",
    "question": "Какой чемпион не относится к тегу #Злодей?",
    "answers": ["Творец", "Таскмастер", "Джо Фиксит", "Бродяга"],
    "correct": 3,
    "time": 30,
    "points": 1000
    },
    {
    "type": "numeric",
    "question": "Сколько всего Дедпулов в игре ( хенчпулы не в счёт )?",
    "correct": 7,
    "time": 45,
    "points": 1300
    },
    {
    "type": "mcq",
    "question": "С кем синергия у Одина называемая 'Разжигатель войны'?",
    "answers": ["Танос", "Мефисто", "Хела", "Мангог"],
    "correct": 2,
    "time": 30,
    "points": 1250
    },
    {
    "type": "mcq",
    "question": "Как называется коронная способность у Поглотителя",
    "answers": ["Поглощение силы", "Усиление формы", "Усиленное морфирование","Сокрушитель Воли"],
    "correct": 3,
    "time": 30,
    "points": 700
    },
    ],

    "Акты, титулы и сюжет":[
{
  "type": "text",
  "question": "Как называется 5 акт?",
  "correct": "Бич Старейшины",
  "time": 30,
  "points": 800
},
{
  "type": "mcq",
  "question": "В каком задании ЗА ИССЛЕДОВАНИЕ НЕ дают аватарку?",
  "answers": ["Некрополь", "Бездна Легенд", "Мавзолей Нетленных","Эпоха Боли"],
  "correct": 2,
  "time": 30,
  "points": 1500
},
{
  "type": "mcq",
  "question": "Как называется титул за полное исследование 'Бездны легенд'?",
  "answers": ["Бездна посмотрела в ответ", "Повелитель Бездны ", "Бездна смотрит в ответ", "Нету правильного ответа"],
  "correct": 0,
  "time": 30,
  "points": 500
},
{
  "type": "mcq",
  "question": "Сколько энергии стоит вход в Бездну Легенд?",
  "answers": ["1", "2", "3", "5"],
  "correct": 0,
  "time": 20,
  "points": 800
},
{
  "type": "mcq",
  "question": "В каком году вышла игра Marvel Contest of Champions?",
  "answers": ["2013", "2014", "2015", "2016"],
  "correct": 1,
  "time": 20,
  "points": 750
},
        {
  "type": "mcq",
  "question": "Какое описание у титула 'Гроза престолов'?",
  "answers": [
    "Призыватель победил Гроссмейстера в его собственной игре и полностью исследовал акт 6",
    "Призыватель победил Гроссмейстера и полностью исследовал акт 6",
    "Призыватель победил Гроссмейстера в его собственной игре и прошел акт 6",
    "Призыватель победил Гроссмейстера и полностью исследовал 6 акт"
  ],
  "correct": 0,
  "time": 30,
  "points": 1000
},
{
  "type": "mcq",
  "question": "Какой дают титул за полное исследование 4 акта?",
  "answers": [
    "Призыватель победил Маэстро и полностью исследовал акт 4",
    "Бич старейшины",
    "Верховный призыватель",
    "Нету правильного ответа"
  ],
  "correct": 3,
  "time": 30,
  "points": 1000
},
        {
  "type": "text",
  "question": "Как называется 8 акт?",
  "correct": "Сияние",
  "time": 30,
  "points": 800
},
        {
    "type": "numeric",
    "question": "За сколько пройденных зон давался титул 'Легенда' во вторжениях?",
    "correct": 25,
    "time": 30,
    "points": 750
    },
    {
    "type": "mcq",
    "question": "Сколько стоит 'Двойной кристалл сплава класса 3 категории'?",
    "answers": ["1700", "2300", "2800", "3200"],
    "correct": 2,
    "time": 30,
    "points": 750
    },
        {
    "type": "mcq",
    "question": "Кто является первым противником в «Лабиринте легенд» по прямой?",
    "answers": ["Звёздный Лорд", "Красный Халк", "Ракета", "Дормамму"],
    "correct": 1,
    "time": 30,
    "points": 1500
    },
        {
            "type": "mcq",
            "question": "Сколько заданий в 4 акте?",
            "answers": ["12", "18", "24", "6"],
            "correct": 2,
            "time": 30,      
            "points": 500
        },
        {
            "type": "mcq",
            "question": "Сколько энергии стоит обычная дорожка в 6 акте?",
            "answers": ["1","2", "3", "4"],
            "correct": 1,
            "time": 20,      
            "points": 500

        },
        {
    "type": "mcq",
    "question": "Какая правильная последовательность боссов «Лето боли» 2021 года (первая неделя — Шельма)?",
    "answers": [
        "Шельма → Синистер → Мистерио → Темный Ястреб → Веном → Адапдоид → Симбиот → Эмма Фрост → Призрак → Гроссмейстер",
        "Шельма → Мистерио → Синистер → Темный Ястреб → Веном → Адапдоид → Симбиот → Эмма Фрост → Призрак → Гроссмейстер",
        "Шельма → Синистер → Темный Ястреб → Мистерио → Веном → Симбиот → Адапдоид → Эмма Фрост → Призрак → Гроссмейстер",
        "Шельма → Темный Ястреб → Синистер → Мистерио → Веном → Адапдоид → Эмма Фрост → Симбиот → Призрак → Гроссмейстер"
    ],
    "correct": 0,
    "time": 60,
    "points": 2500    
    },
    {
        "type": "text",
        "question": "Сколько всего стоит противников в задании 'Перчатка гросмейстера'?",
        "correct": "21",
        "time": 40,
        "points": 1500
        },
        {
        "type": "numeric",
        "question": "Сколько серы можно получить за исследование любой ветки 7 акта?",
        "correct": 90,
        "time": 25,
        "points": 800
        },

    ],

    "Угадай персонажа по звуку": [
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Терракс.mp3",
            "correct": ["Терракс","Теракс"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Фьюри.mp3",
            "correct": ["Фьюри","Ник Фьюри"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Хеймдаль.mp3",
            "correct": ["Хеймдаль"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Призрак.mp3",
            "correct": ["Призрак"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Эйгон.mp3",
            "correct": ["Эйгон"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Ртуть.mp3",
            "correct": ["Ртуть"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/СпецКула.mp3",
            "correct": ["Кулл Обсидиан","Кулл"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/ТяжКула.mp3",
            "correct": ["Кулл Обсидиан","Кулл"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Минору.mp3",
            "correct": ["Нико Минору","Минору"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/модок.mp3",
            "correct": ["М.О.Д.О.К","Модок"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Оса.mp3",
            "correct": ["Оса"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Печенька.mp3",
            "correct": ["Печенька"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Призрак.mp3",
            "correct": ["Призрак"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Анига.mp3",
            "correct": ["Анигилус","Анига"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Апок.mp3",
            "correct": ["Апокалипсис","Апок"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/ГражданскийВоин.mp3",
            "correct": ["Воин","Гражданский Воин","Гражданский"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/ДжекФонарь.mp3",
            "correct": ["Джек","Джек Фонарь","Фонарь"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Джессика.mp3",
            "correct": ["Джессика Джонс","Джессика"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Домино.mp3",
            "correct": ["Домино"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Икарис.mp3",
            "correct": ["Икарис"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Кабель.mp3",
            "correct": ["Кабель"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Корг.mp3",
            "correct": ["Корг"],
            "time": 25,
            "points": 1500
        },

        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Меджик.mp3",
            "correct": ["Меджик"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи полное имя персонажа)",
            "audio": "/static/audios/Факел.mp3",
            "correct": ["Человек факел", "Факел"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Фантастик.mp3",
            "correct": ["Мистер фантастик","Фантастик"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Хавок.mp3",
            "correct": ["Хавок"],
            "time": 25,
            "points": 1500
        },
        

        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Кросбоунс.mp3",
            "correct": ["Череп и кости","КроссБоунс","КросБоунс"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Печенька.mp3",
            "correct": ["Существо","Печенька"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Сокол.mp3",
            "correct": ["Сокол"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Стэлс.mp3",
            "correct": ["Стэлс","Стэлс","Стэлс паук","Паук стэлс","Стелс паук","паук стелс"],
            "time": 25,
            "points": 1500
        },

        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/Урка.mp3",
            "correct": ["Уорлок","Урка"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "1. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/БЖЧ.mp3",
            "correct": ["БЖЧ"],
            "time": 25,
            "points": 1500
        },
        {
            "type": "text",
            "question": "2. Кто это по звуку? (введи имя персонажа)",
            "audio": "/static/audios/ЧВСО.mp3",
            "correct": ["Черная вдова", "Белая вдова","Черная вдова смертельно опасная","Черная вдова (Смертельно опасная)"],
            "time": 25,
            "points": 1500
        }
    ],

    "Угадай персонажа с картинки": [

        {
        "type": "text",
        "question": "1. Чей предмет изображен на картинке?",
        "image": "/static/images/масакр.png",
    "image_zoom": 16,
    "image_position": "50% 32.2%",
    "image_offset": {
        "x": -1,
        "y": 0
    },
        "correct": ["Масакр"],
        "time": 30,      
        "points": 1500
        },

        {
        "type": "text",
        "question": "1. Чей предмет изображен на картинке?",
        "image": "/static/images/Гиля.jpg",
    "image_zoom": 6.1,
    "image_position": "27% 66%",
    "image_offset": {
        "x": 0,
        "y": 0
        },
        "correct": ["Гильотина","Гиля","Гильотина 2099","Гиля 2099"],
        "time": 30,      
        "points": 1500
        },
        

        {
        "type": "text",
        "question": "1. Чей предмет изображен на картинке?",
        "image": "/static/images/Спираль.jpg",
    "image_zoom": 4.7,
    "image_position": "20% 25%",
    "image_offset": {
        "x": 0,
        "y": 0
        },
        "correct": ["Cпираль"],
        "time": 30,      
        "points": 1500
        },

        {
        "type": "text",
        "question": "1. Чей предмет изображен на картинке?",
        "image": "/static/images/Окойе.jpg",
        "image_zoom": 7.6,
        "image_position": "34% 54%",
        "image_offset": {
            "x": 0,
            "y": 0
        },
        "correct": ["Окойе"],
        "time": 30,      
        "points": 1500
        },

        {
        "type": "text",
        "question": "1. Кто изображен на картинке?",
        "image": "/static/images/Шершень.jpg",
        "image_zoom": 4.9,
        "image_position": "50% 34%",
        "image_offset": {
            "x": 0,
            "y": 0
        },
        "correct": ["Шершень", "Желтый шершень"],
        "time": 30,      
        "points": 1500
        },





        {
        "type": "text",
        "question": "1. Чей предмет изображен на картинке?",
        "image": "/static/images/Изофина.PNG",
        "image_zoom": 4.6,
        "image_position": "12.4% 36%",
        "image_offset": {
            "x": -126,
            "y": 240
        },
        "correct": ["Изофина", "Исофина"],
        "time": 30,      
        "points": 1500
        },

        {
        "type": "text",
        "question": "1. Кто изображен на картинке?",
        "image": "/static/images/Симбиот.PNG",
        "image_zoom": 8.7,
        "image_position": "48% 6%",
        "image_offset": {
            "x": -126,
            "y": 240
        },
        "correct": ["Симбиот", "Призванный симбиот", "Симбиоид", "Призванный симбиоид"],
        "time": 30,      
        "points": 750
        },




        {
        "type": "text",
        "question": "1. Кто изображен на картинке?",
        "image": "/static/images/Разрушитель.PNG",
        "image_zoom": 7.3,
        "image_position": "37% 57%",
        "image_offset": {
            "x": -126,
            "y": 240
        },
        "correct": ["Разрушитель"],
        "time": 30,      
        "points": 900
        },

        {
        "type": "mcq",
        "question": "1. Кто изображен на картинке?",
        "image": "/static/images/Мордо.PNG",
        "image_zoom": 5.3,
        "image_position": "48% 54%",
        "image_offset": {
            "x": -184,
            "y": 126
        },
        "answers": ["Киндред", "Люк Кейдж", "Лунный рыцарь", "Нет правильного ответа"],
        "correct": 3,
        "time": 30,      
        "points": 500
        },

        {
        "type": "mcq",
        "question": "1. Кто изображен на картинке?",
        "image": "/static/images/паук2.PNG",
        "image_zoom": 8.7,
        "image_position": "54% 38%",
        "image_offset": {
            "x": -184,
            "y": 126
        },
        "answers": ["Крейвен", "Люк Кейдж", "Человек Паук (Панк)", "Человек Паук (прабхакар)"],
        "correct": 3,
        "time": 30,      
        "points": 750
        },

        {
        "type": "text",
        "question": "1. Кто изображен на картинке?",
        "image": "/static/images/Утка веном.PNG",
        "image_zoom": 4.9,
        "image_position": "70% 74%",
        "image_offset": {
            "x": -184,
            "y": 126
        },
        "correct": ["Утка Веном"],
        "time": 30,      
        "points": 1000
        },

        {
        "type": "text",
        "question": "1. Кто изображен на картинке?",
        "image": "/static/images/эйгон.PNG",
        "image_zoom": 8.3,
        "image_position": "42% 49.5%",
        "image_offset": {
            "x": -184,
            "y": 126
        },
        "correct": ["Эйгон"],
        "time": 30,      
        "points": 1000

        },
        
        {
        "type": "text",
        "question": "1. Кто изображен на картинке?",
        "image": "/static/images/лидер.PNG",
        "image_zoom": 8.1,
        "image_position": "49% 93%",
        "image_offset": {
            "x": -196,
            "y": 126
        },
        "correct": ["Лидер"],
        "time": 30,      
        "points": 1250
        },

        {
        "type": "text",
        "question": "2. Кто изображен на картинке?",
        "image": "/static/images/сокол.PNG",
        "image_zoom": 6.3,
        "image_position": "45% 42%",
        "image_offset": {
            "x": -125,
            "y": 265
        },
        "correct": ["Сокол"],
        "time": 30,      
        "points": 750
        },

        {
        "type": "text",
        "question": "3. Кто изображен на картинке?",
        "image": "/static/images/модок.PNG",
        "image_zoom": 9.7,
        "image_position": "48% 35%",
        "image_offset": {
            "x": -125,
            "y": 265
        },
        "correct": ["Модок", "М.О.Д.О.К"],
        "time": 30,      
        "points": 1100
        },

        {
        "type": "text",
        "question": "4. Чья культяпка изображена на картинке?",
        "image": "/static/images/крот.PNG",
        "image_zoom": 8,
        "image_position": "39% 71%",
        "image_offset": {
            "x": -125,
            "y": 265
        },
        "correct": ["Крот", "Человек крот"],
        "time": 30,      
        "points": 1000
        },
        {
        "type": "mcq",
        "question": "5. Кто изображенный на картинке не пропускает день спины?",
        "image": "/static/images/Фантастик.png",
        "image_zoom": 5.5,
        "image_position": "43% 34%",
        "image_offset": {
            "x": -125,
            "y": 265
        },
        "answers": ["Человек-Факел", "Черный гром", "Паук Стелс", "Мистер Фантастик"],
        "correct": 3,
        "time": 30,      
        "points": 850
        },
        
        {
        "type": "text",
        "question": "6. Кто изображён на картинке? (Напиши полное название!)",
        "image": "/static/images/ПАУК.PNG",
        "image_zoom": 8.9,
        "image_position": "46% 50%",
        "image_offset": {
            "x": -134,
            "y": 300
        },
        "correct": ["Паук Симбиот", "Паук Веном", "Веном Паук"],
        "time": 30,      
        "points": 750
        },
        {
        "type": "text",
        "question": "7. Кто изображён на картинке? (Напиши полное название!)",
        "image": "/static/images/Кейт.PNG",
        "image_zoom": 10.5,
        "image_position": "37% 32%",
        "image_offset": {
            "x": 34,
            "y": 148
        },
        "correct": ["Кейт Бишоп", "Кейт"],
        "time": 30,      
        "points": 750
        },

        {
        "type": "text",
        "question": "8. Кто изображён на картинке? (Напиши полное название!)",
        "image": "/static/images/Америкос.png",
        "image_zoom": 9.1,
        "image_position": "60% 42%",
        "image_offset": {
            "x": 33,
            "y": 148
        },
        "correct": "Патриот",
        "time": 30,      
        "points": 850
        },

        {
        "type": "mcq",
        "question": "9. Кто изображен на картинке?",
        "image": "/static/images/Боеголовка.png",
        "image_zoom": 12,
        "image_position": "45.5% 48%",
        "image_offset": {
            "x": 275,
            "y": -300
        },
        "answers": ["Джин Грей", "Циклоп ( синяя команда )", "Боеголовка", "Крейвен"],
        "correct": 2,
        "time": 30,      
        "points": 1000
        },
        {
        "type": "mcq",
        "question": "10. Кто изображен на картинке?",
        "image": "/static/images/апок2.png",
        "image_zoom": 4.9,
        "image_position": "46% 42%",
        "image_offset": {
            "x": 10,
            "y": -300
        },
        "correct": ["Апокалипсис", "Апок"],
        "time": 30,      
        "points": 1500
        },
        {
        "type": "mcq",
        "question": "11. Чья прическа изображена на картинке?",
        "image": "/static/images/Змей Ночной.png",
        "image_zoom": 8.5,
        "image_position": "48% 11.5%",
        "image_offset": {
            "x": -74,
            "y": -300
        },
        "answers": ["Кушала", "Джессика Джонс", "Полярная звезда", "Ночной Змей"],
        "correct": 3,
        "time": 30,      
        "points": 1000
        },
        {
            "type": "mcq",
            "question": "12. Кто изображён на фрагменте?",
            "image": "/static/images/satra.png",
            "image_zoom": 6.6,
            "image_position": "44% 37%",
            "image_offset": {
                "x": 6,
                "y": -300
            },
            "answers": ["Утка веном", "Налл", "Шатра", "Человек Паук 2099"],
            "correct": 2,
            "time": 30,      
            "points": 750
        },
        {
        "type": "mcq",
        "question": "13. Чей элемент изображен на картинке?",
        "image": "/static/images/МБАКУ.png",
        "image_zoom": 4.9,
        "image_position": "80% 26%",
        "image_offset": {
            "x": 6,
            "y": -80
        },
        "answers": ["М'Баку", "Геркулес", "Морнингстар", "Маэстро"],
        "correct": 0,
        "time": 30,      
        "points": 1000
    },
    
    
    {
        "type": "mcq",
        "question": "14. Кто изображен на картинке?",
        "image": "/static/images/Росомаха.png",
        "image_zoom": 8.8,
        "image_position": "52% 34%",
        "image_offset": {
            "x": -71,
            "y": -234
        },
        "answers": ["Геркулес", "Росомаха (Оружие ИКС)", "Старик Логан", "М.О.Д.О.К"],
        "correct": 1,
        "time": 30,      
        "points": 1050
    },
    

],
    "Угадай персонажа по описанию": [

        {
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "После боя я по привычке трогаю дракончика по носу — если всё сделано правильно, он довольный… если нет — он сожжёт всё, что видит. Тут решают не «сила» и не «удача»У меня всё работает как переключатель: угадал момент — я неуязвима. Не угадал — и тебя наказывают за одну секунду",
  "correct": ["Китти Прайд", "Китти"],
  "time": 40,
  "points": 1800
},
        
        {
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Я существую между мирами: то меня бьют — то нет. Я переворачиваю логику боя и выигрываю там, где это кажется невозможным. Очень хорошо стакается с Осой.",
  "correct": ["Призрак", "приза"],
  "time": 35,
  "points": 1600
},

  {
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Олдскульная легенда: поначалу выглядит «никаким», но затем набирает ход. Любит накапливать заряды, разгоняться с каждым комбо и в затяжных боях становится настоящим убийцей. Главное правило — держать серию: лишние удары получать не рекомендуется.",
  "correct": ["Эйгон"],
  "time": 35,
  "points": 1600
},

{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Я не просто «танк» — я превращаю бой в свой спектакль. Богат усилениями? Отлично, я их заберу. А когда начинаю раздавать фирменные «лящи» — соперник очень быстро понимает, кто тут главный.",
  "correct": ["Доктор Дум", "Дум"],
  "time": 40,
  "points": 1800
},

{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Я из тех, кто приходит и делает грязную работу без лишних слов. Могу включать регены, люблю пули и холодную эффективность. А когда моя нанотехнология перезарядилась — меня уже почти невозможно остановить!",
  "correct": ["Каратель 2099", "Каратель2099"],
  "time": 35,
  "points": 1400
},

{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Я не про «везение» — я про неизбежность. Дай мне пару раз разогнаться, и дальше с первых секунд бой превращается в избиение без права на камбэк: урон ровный, стабильный и только растёт. А если ты вдруг не понял, кто тут главная волосатая грудь — это я!",
  "correct": ["Геркулес", "Герк"],
  "time": 35,
  "points": 1400
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Голова — как у босса, тело — а оно там есть?: сижу на троне и выгляжу так, будто меня собрали в гараже из высокомерия и злости. Но внешность не обманывает — я 7★ эксклюзив, так что уважение включать обязательно.",
  "correct": ["Модок", "М.О.Д.О.К"],
  "time": 35,
  "points": 1500
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Я — идеальный пример «почему ты умер без касания». Темп бешеный, наказания резкие: я тебя просто отхлыстаю — и ты поймёшь это только тогда, когда твоя полоска ХП будет на нуле.",
  "correct": ["Ртуть"],
  "time": 35,
  "points": 1700
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Конкретный материал — мой союзник: мои тяжи сначала кажутся не такими страшными… а потом ты уже не успеваешь сказать: «как это было больно».",
  "correct": ["Магнето", "Магнит", "Красный магнит","Магнит красный"],
  "time": 35,
  "points": 1400
},

{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Миека у меня всегда на подхвате. «Броня»? Не, не слышал. А если ты начнёшь играть слишком прямолинейно — я в нужный момент превращаю бой в мокрую пытку!",
  "correct": ["Корг"],
  "time": 35,
  "points": 1500
},

{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Самое неприятное — ты не сразу понимаешь, что проиграл. Один точный укол хвостом — и дальше ты уже дерёшься с последствиями: от меня отходят долго.",
  "correct": ["Скорпион", "Скорп"],
  "time": 35,
  "points": 1500
},

{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Когда-то я был обычным физиком. Но если меня разозлить — я превращаюсь в ходячую катастрофу: хватаю всё, что под рукой — хоть кусок асфальта, хоть ствол дерева, каким я отправлю тебя в другой конец города одним ударом.",
  "correct": ["Сасквоч", "Сасквотч"],
  "time": 35,
  "points": 1600
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Я выгляжу как «милый зверёк», люблю вилять хвостом после победы над противником… но это ровно до тех пор, пока ты не поймёшь: у меня всё строится на темпе и точных тройных ударах. Если ты послушный противник — я тебя просто разорву на части, а ты даже не сразу поймёшь, что произошло.",
  "correct": ["Тигра", "Tigra"],
  "time": 35,
  "points": 1500
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Мои волосы внушают страх, а арсенал — уважение: клинки, меч и копьё . Я чередую удары, меняя оружие по очереди, и даже если где-то оступлюсь — у меня есть право на одну ошибку, (если конечно я в дубле) после которой расплата становится твоей.",
  "correct": ["Хела", "Hela"],
  "time": 35,
  "points": 1600
},

{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Я выгляжу так, будто вышел из океана и даже не обсох —  каждый мой удар идёт с брызгами воды, будто тебя лупят приливом. А моё оружие — шипастый костяной клинок: широкий, зазубренный, как плавник, и сбоку торчит крупный «крюк-шип».",
  "correct": ["Аттума", "Attuma"],
  "time": 35,
  "points": 1600
},
  {
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Мой стиль — чистое кунг-фу: без оружия, только техника, дисциплина и контроль Ци. Я знаю десятки стилей ушу, а в бой почти всегда выхожу в красном — так меня легче запомнить.",
  "correct": ["Шанг-Чи", "Шан-Чи"],
  "time": 35,
  "points": 1500
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Я не люблю шум — я люблю порядок. Правая рука большого босса, командир армии и тот, кто решает исход боя холодной головой. Мой главный аргумент — глефа: ей я не просто бью, я буквально «прорезаю» строй врагов и делаю проход там, где его быть не должно.",
  "correct": ["Корвус Глэйв", "Корвус"],
  "time": 35,
  "points": 1600
},

{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Плащ на мне сидит как живой, а пальцы складываются в жесты быстрее, чем ты успеешь моргнуть. Когда-то я был звездой нейрохирургии, а теперь — забытая, но всё ещё легенда. В 2015 я был одним из самых лучших чемпионов,",
  "correct": ["Доктор Стрэндж", "Доктор Стрендж", "Стрэндж", "Стрендж"],
  "time": 35,
  "points": 1600
},

  {
    "type": "text",
    "question": "Угадай персонажа по описанию:",
    "prompt": "Я — идеальный пример самого бесполезного чемпиона в игре! Да, я умею настакивать горы брони и потом превращать её в ярость, да, у меня есть щит — но это всё равно не делает меня хорошим чемпионом :( За что мне такая участь?",
    "correct": ["Гражданский Воин", "Воин", "Гражданский"],
    "time": 35,
    "points": 1500
  },
  {
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Я — ходячая удача. Иногда крит прилетает так жирно, что кажется — ты только что потратил удачу на год вперёд. А если фортуна не на твоей стороне — придётся ждать, когда она снова улыбнётся в твою сторону.",
  "correct": ["Домино"],
  "time": 35,
  "points": 1500
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "С виду — обычный путник, а на деле могу быть сразу в нескольких точках. Руки оказываются там, где ты вообще не ждёшь: для меня дистанция — вещь условная. И да, я умею подстроиться под любые твои особые атаки!",
  "correct": ["Мистер Фантастик", "Мистер фантастик"],
  "time": 35,
  "points": 1600
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Я, конечно, не Кэрол Дэнверс, но мои кулаки ей точно не уступают: могу вмазать так же мощно. А после каждой победы у меня традиция — сделать селфи.",
  "correct": ["Мисс Марвел", "Мисс Марвелл", "Камала Хан" "миссис марвел"],
  "time": 35,
  "points": 1500
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Я — не герой, а ходячий мультяшный троллинг. Люблю заманить тебя в «безопасное» место, дёрнуть ниточку — и сверху внезапно прилетает наковальня. ",
  "correct": ["Свин-Паук", "Свин паук", "Свинпаук"],
  "time": 35,
  "points": 1500
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Я — не из «старой школы», но у меня есть два козыря, от которых у соперника начинается паника: могу внезапно стать невидимым и так же внезапно выдать электрический разряд. Один момент — ты думаешь, что контролируешь бой… следующий — и ты уже ловишь мой сюрприз.",
  "correct": ["Майлз Моралес", "Майлз", "Miles Morales", "Miles"],
  "time": 35,
  "points": 1600
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "После одной судьбоносной ночи с наёмником я понял: «прощения мало — нужен приговор». Так я быстро переквалифицировался в наёмники и оказался там, где платят за результат, а мораль не задаёт вопросов.",
  "correct": ["Масакр"],
  "time": 35,
  "points": 1600
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "С виду я могу казаться спокойным, но это обман. Мне достаточно пары удачных моментов, чтобы бой вышел из-под твоего контроля: дальше ты уже не атакуешь, а просто пытаешься пережить мою следующую вспышку.",
  "correct": ["Хавок", "Havok"],
  "time": 35,
  "points": 1600
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Я один из тех, кто превращает терпение в оружие. Чем дольше тянется бой, тем сильнее я раскрываюсь, и в какой-то момент соперник осознаёт, что всё это время сам копал себе яму.",
  "correct": ["Корвус Глейв", "Корвус", "Corvus Glaive", "Corvus"],
  "time": 35,
  "points": 1600
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Мне не нужно честно перебивать тебя по урону — я просто превращаю твои привычки в слабость. Пока ты играешь в свой обычный бой, я шаг за шагом лишаю тебя шансов делать это дальше.",
  "correct": ["Паук 2099", "Человек-Паук 2099", "Человек Паук 2099", "Spider-Man 2099", "Spider Man 2099"],
  "time": 35,
  "points": 1600
},
{
  "type": "text",
  "question": "Угадай персонажа по описанию:",
  "prompt": "Я не давлю грубой силой с первой секунды — я методично собираю преимущества, и в какой-то момент бой ломается. С этого момента ты уже не дерёшься, а просто стоишь в очереди на поражение.",
  "correct": ["Китти Прайд", "Китти", "Kitty Pryde", "Kitty"],
  "time": 35,
  "points": 1600
}
],



####

"Угадай слово связанное с игрой": [
{
  "question": "Назови игровой термин!",
  "type": "wordle",
  "prompt": "То, что собирается в бою и легко потерять.",
  "correct": "комбо",
  "time": 150,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай чемпиона.",
  "type": "wordle",
  "prompt": "Чемпион мистического класса.",
  "correct": "Мангог",
  "time": 150,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Назови игровой ресурс",
  "type": "wordle",
  "prompt": "Базовый игровой ресурс (единственное число).",

  "correct": "Единица",
  "time": 150,
  "points": 1000,
  "max_attempts": 7
},
{
  "question": "Назови игровой ресурс",
  "type": "wordle",
  "prompt": "Базовый игровой ресурс (единственное число).",

  "correct": "Сплав",
  "time": 150,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Назови игровой контент!",
  "type": "wordle",
  "prompt": "Контент.",

  "correct": "Горн",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай персонажа",
  "type": "wordle",
  "prompt": "Чемпион космического класса.",
  "correct": "Веном",
  "time": 150,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай персонажа",
  "type": "wordle",
  "prompt": "Чемпион мистического класса.",

  "correct": "Поглотитель",
  "time": 180,
  "points": 1300,
  "max_attempts": 8
},
{
  "question": "Угадай персонажа",
  "type": "wordle",
  "prompt": "Чемпион Научного класса.",
  "correct": "Ящер",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай контент связанный с игрой",
  "type": "wordle",
  "prompt": "Контент.",
  "correct": "Вариант",
  "time": 150,
  "points": 1100,
  "max_attempts": 7
},
{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "Накопительный ресурс.",

  "correct": "Жетоны",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай персонажа",
  "type": "wordle",
  "prompt": "Чемпион научного класса",

  "correct": "Фотон",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "НАЗВАНИЕ контента связанного с игрой",

  "correct": "Сияние",
  "time": 150,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "Титул.",

  "correct": "Доблестный",
  "time": 180,
  "points": 1200,
  "max_attempts": 8
},
{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "Статус",

  "correct": "Титул",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай персонажа",
  "type": "wordle",
  "prompt": "Чемпион космического класса.",

  "correct": "Нова",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай персонажа",
  "type": "wordle",
  "prompt": "Чемпион технического класса.",

  "correct": "Призрак",
  "time": 150,
  "points": 1000,
  "max_attempts": 7
},
{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "Расходный ресурс.",

  "correct": "Зелье",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "Усиление",

  "correct": "Ярость",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "Укращающее профиль",

  "correct": "Аватар",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "Ключевой ресурс!",

  "correct": "Катализатор",
  "time": 150,
  "points": 1400,
  "max_attempts": 8
},
{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "Кто-то предпочитает это, а кто-то нет...",

  "correct": "Союз",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "Их всего 5",

  "correct": "Нетленный",
  "time": 150,
  "points": 1200,
  "max_attempts": 8
},

{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "Мастерство",

  "correct": "Отчаяние",
  "time": 150,
  "points": 1200,
  "max_attempts": 8
},
{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "Мастерство",

  "correct": "Отдача",
  "time": 150,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "Эффект - наносящий урон",

  "correct": "Разрыв",
  "time": 150,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "Контент(единсвенное число).",

  "correct": "Акт",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},

{
  "question": "Угадай слово связанное с игрой",
  "type": "wordle",
  "prompt": "Эффект",

  "correct": "Печать",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай персонажа",
  "type": "wordle",
  "prompt": "Чемпион научного класса - нельзя получить",

  "correct": "Бахамет",
  "time": 150,
  "points": 1000,
  "max_attempts": 7
},

{
  "question": "Угадай персонажа",
  "type": "wordle",
  "prompt": "Персонаж.",

  "correct": "Вокс",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай что-то про чемпиона",
  "type": "wordle",
  "prompt": "Что-то вне Земли",

  "correct": "Космос",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},
{
  "question": "Угадай персонажа",
  "type": "wordle",
  "prompt": "Базовая боевая механика.",

  "correct": "Блок",
  "time": 120,
  "points": 1000,
  "max_attempts": 6
},






]
}

# Supported question types:
# mcq        - multiple choice (default)
# numeric    - numeric input
# text       - short text answer
# wordle     - word guessing with multiple attempts and colored letter feedback
# poll       - no correct answer (voting)
# fastest    - first correct wins bonus

def generate_pin():
    while True:
        pin = "".join(random.choices(string.digits, k=6))
        if pin not in rooms:
            return pin


def get_socket_lock(room_data: dict, connection_key: str) -> asyncio.Lock:
    socket_locks = room_data.setdefault("socket_locks", {})
    lock = socket_locks.get(connection_key)
    if lock is None:
        lock = asyncio.Lock()
        socket_locks[connection_key] = lock
    return lock


async def safe_send_json(room_data: dict, websocket: WebSocket | None, message: dict, connection_key: str) -> bool:
    if websocket is None:
        return False
    lock = get_socket_lock(room_data, connection_key)
    try:
        async with lock:
            await asyncio.wait_for(websocket.send_json(message), timeout=WS_SEND_TIMEOUT)
        return True
    except Exception:
        return False


async def broadcast(room: str, message: dict):
    if room not in rooms:
        return
    room_data = rooms[room]
    send_tasks = []

    host_ws = room_data.get("host")
    if host_ws:
        send_tasks.append(safe_send_json(room_data, host_ws, message, "host"))

    for username, ws in list(room_data.get("players", {}).items()):
        send_tasks.append(safe_send_json(room_data, ws, message, f"player:{username}"))

    if send_tasks:
        await asyncio.gather(*send_tasks, return_exceptions=True)


async def close_room_and_notify(room: str, message: str = "Тест завершен"):
    if room not in rooms:
        return False

    room_data = rooms[room]
    log_event("room_closing", room, message=message, players=len(room_data.get("players", {})))

    abort_payload = {
        "type": "test_aborted",
        "message": message
    }
    player_items = list(room_data.get("players", {}).items())
    await asyncio.gather(*[
        safe_send_json(room_data, ws, abort_payload, f"player:{username}")
        for username, ws in player_items
    ], return_exceptions=True)
    await asyncio.gather(*[
        ws.close()
        for _, ws in player_items
    ], return_exceptions=True)

    if room_data.get("host"):
        await safe_send_json(room_data, room_data["host"], {
            "type": "return_to_lobby",
            "message": "Тест завершён. Игроки удалены."
        }, "host")

    del rooms[room]
    try:
        asyncio.get_running_loop().create_task(broadcast_admin({"type": "room_removed", "room": room}))
    except RuntimeError:
        pass
    log_event("room_closed", room, message=message)
    return True

@app.get("/")
async def root():
    return FileResponse("static/host.html")

@app.get("/host.html")
async def host_page():
    return FileResponse("static/host.html")

@app.get("/player.html")
async def player_page():
    return FileResponse("static/player.html")

@app.get("/image_editor.html")
async def image_editor_page():
    return FileResponse("static/image_editor.html")

@app.get("/admin.html")
async def admin_page():
    return FileResponse("static/admin.html")


async def execute_admin_action(room: str, action: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    if room not in rooms:
        return {"ok": False, "error": "Room not found"}

    room_data = rooms[room]

    if action == "kick_player":
        target = str(payload.get("username") or "").strip()
        if not target:
            return {"ok": False, "error": "username is required"}
        player_ws = room_data.get("players", {}).get(target)
        if player_ws is None:
            return {"ok": False, "error": "Player not found"}
        await safe_send_json(room_data, player_ws, {"type": "kicked"}, f"player:{target}")
        try:
            await player_ws.close()
        except Exception:
            pass
        room_data["players"].pop(target, None)
        room_data["scores"].pop(target, None)
        room_data["answers"].pop(target, None)
        room_data.get("disconnected", {}).pop(target, None)
        room_data.get("current_question_metrics", {}).pop(target, None)
        room_data.get("player_flags", {}).pop(target, None)
        room_data.get("late_joiners", {}).pop(target, None)
        remove_player_from_teams(room_data, target)
        await update_players(room)
        await broadcast_team_update(room)
        log_event("admin_kick_player", room, username=target)
        return {"ok": True, "username": target}

    if action == "send_message":
        target = str(payload.get("username") or "").strip()
        message_text = str(payload.get("message") or payload.get("text") or "").strip()
        if not message_text:
            return {"ok": False, "error": "message is required"}
        packet = {
            "type": "admin_message",
            "message": message_text,
            "target": target or "all",
        }
        if target:
            player_ws = room_data.get("players", {}).get(target)
            if player_ws is None:
                return {"ok": False, "error": "Player not found"}
            ok = await safe_send_json(room_data, player_ws, packet, f"player:{target}")
            log_event("admin_message_sent", room, username=target, ok=ok, message=message_text)
            return {"ok": ok, "target": target}
        await broadcast(room, packet)
        log_event("admin_message_sent", room, username="all", ok=True, message=message_text)
        return {"ok": True, "target": "all"}

    if action == "set_manual_suspicious":
        target = str(payload.get("username") or "").strip()
        if not target:
            return {"ok": False, "error": "username is required"}
        metrics = room_data.setdefault("current_question_metrics", {}).setdefault(target, {
            "tab_switches": 0,
            "response_time_ms": None,
            "answered_at": None,
            "offline_total_ms": 0,
            "offline_started_at": None,
            "offline_before_answer_ms": 0,
        })
        metrics["manual_suspicious"] = bool(payload.get("value", True))
        metrics["manual_suspicious_note"] = str(payload.get("note") or "").strip()
        log_event(
            "admin_manual_suspicious",
            room,
            username=target,
            value=bool(payload.get("value", True)),
            note=metrics["manual_suspicious_note"],
            question_index=room_data.get("question_index"),
        )
        return {"ok": True, "username": target, "manual_suspicious": metrics["manual_suspicious"], "note": metrics["manual_suspicious_note"]}

    if action == "adjust_score":
        target = str(payload.get("username") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        try:
            delta = int(payload.get("delta"))
        except Exception:
            return {"ok": False, "error": "delta must be an integer"}
        if target not in room_data.get("scores", {}):
            return {"ok": False, "error": "Player not found"}
        room_data["scores"][target] += delta
        room_data.setdefault("appeals", []).append({
            "ts": time.time(),
            "username": target,
            "delta": delta,
            "reason": reason or "Admin score adjustment",
            "resolved": True,
            "resolution": "manual_adjustment",
        })
        leaderboard = sorted(room_data["scores"].items(), key=lambda x: x[1], reverse=True)
        await broadcast(room, {
            "type": "appeal_awarded",
            "username": target,
            "delta": delta,
            "reason": reason or "Admin score adjustment",
            "leaderboard": leaderboard,
            "team_leaderboard": compute_team_leaderboard(room_data),
            "total_scores": room_data["scores"],
        })
        log_event("admin_adjust_score", room, username=target, delta=delta, reason=reason)
        return {"ok": True, "username": target, "delta": delta}

    if action == "set_warning_count":
        target = str(payload.get("username") or "").strip()
        if not target:
            return {"ok": False, "error": "username is required"}
        result = apply_warning_count(room, room_data, target, payload.get("warning_count", 0))
        if result.get("ok"):
            leaderboard = sorted(room_data["scores"].items(), key=lambda x: x[1], reverse=True)
            await broadcast(room, {
                "type": "appeal_awarded",
                "username": target,
                "delta": -int(result.get("penalty_points", 0) or 0),
                "reason": "Anti-cheat warning penalty" if int(result.get("penalty_points", 0) or 0) > 0 else "Anti-cheat warning updated",
                "leaderboard": leaderboard,
                "team_leaderboard": compute_team_leaderboard(room_data),
                "total_scores": room_data["scores"],
            })
            log_event(
                "admin_warning_updated",
                room,
                username=target,
                warning_count=result.get("warning_count"),
                penalty_points=result.get("penalty_points", 0),
            )
        return result

    if action == "resolve_appeal":
        try:
            appeal_id = int(payload.get("appeal_id"))
        except Exception:
            return {"ok": False, "error": "appeal_id must be an integer"}
        resolution = str(payload.get("resolution") or "rejected").strip() or "rejected"
        reason = str(payload.get("reason") or "").strip()
        try:
            delta = int(payload.get("delta", 0))
        except Exception:
            return {"ok": False, "error": "delta must be an integer"}
        appeals = room_data.setdefault("appeals", [])
        if appeal_id < 0 or appeal_id >= len(appeals):
            return {"ok": False, "error": "Appeal not found"}
        appeal = appeals[appeal_id]
        if not isinstance(appeal, dict) or "question_index" not in appeal:
            return {"ok": False, "error": "Appeal item is not actionable"}
        if appeal.get("resolved"):
            return {"ok": False, "error": "Appeal already resolved"}

        appeal["resolved"] = True
        appeal["resolution"] = resolution
        appeal["resolved_ts"] = time.time()
        if reason:
            appeal["admin_reason"] = reason

        result = {"ok": True, "appeal_id": appeal_id, "resolution": resolution}
        if resolution == "approved" and delta != 0:
            target = appeal.get("username")
            if target in room_data.get("scores", {}):
                room_data["scores"][target] += delta
                appeal["delta"] = delta
                leaderboard = sorted(room_data["scores"].items(), key=lambda x: x[1], reverse=True)
                await broadcast(room, {
                    "type": "appeal_awarded",
                    "username": target,
                    "delta": delta,
                    "reason": reason or appeal.get("reason") or "Admin appeal resolution",
                    "leaderboard": leaderboard,
                    "team_leaderboard": compute_team_leaderboard(room_data),
                    "total_scores": room_data["scores"],
                })
                result["delta"] = delta
        await broadcast(room, {
            "type": "appeal_resolved",
            "appeal_id": appeal_id,
            "username": appeal.get("username"),
            "question_index": appeal.get("question_index"),
            "resolution": resolution,
            "delta": int(appeal.get("delta", 0) or 0),
            "reason": reason or appeal.get("admin_reason") or "",
        })
        log_event("admin_resolve_appeal", room, appeal_id=appeal_id, resolution=resolution, delta=delta, reason=reason)
        return result

    if action == "start_game":
        if room_data.get("status") != "waiting":
            return {"ok": False, "error": "Game is not in waiting state"}
        if room_data.get("team_mode") and not room_data.get("teams"):
            return {"ok": False, "error": "Teams must be shuffled first"}
        room_data["status"] = "active"
        room_data["paused"] = False
        room_data["remaining_time"] = None
        room_data["current_payload"] = None
        room_data["current_view"] = "question"
        await send_question(room)
        log_event("admin_start_game", room, quiz=room_data.get("quiz"))
        return {"ok": True}

    if action == "pause_toggle":
        if room_data.get("status") != "active":
            return {"ok": False, "error": "Game is not active"}
        if not room_data.get("paused"):
            elapsed = time.time() - room_data["question_start"]
            remaining = max(0, room_data["question_duration"] - elapsed)
            room_data["remaining_time"] = remaining
            room_data["paused"] = True
            await broadcast(room, {"type": "paused", "time": remaining})
            log_event("admin_pause", room, remaining=round(remaining, 3))
            return {"ok": True, "paused": True, "remaining": remaining}

        room_data["paused"] = False
        room_data["question_start"] = time.time()
        room_data["question_duration"] = room_data["remaining_time"]
        await broadcast(room, {"type": "resume", "time": room_data["remaining_time"]})
        log_event("admin_resume", room, remaining=room_data["remaining_time"])
        return {"ok": True, "paused": False, "remaining": room_data["remaining_time"]}

    if action == "force_reveal":
        if room_data.get("status") != "active":
            return {"ok": False, "error": "Game is not active"}
        await send_reveal(room, room_data["question_index"])
        log_event("admin_force_reveal", room, question_index=room_data["question_index"])
        return {"ok": True}

    if action == "next":
        if room_data.get("status") != "active":
            return {"ok": False, "error": "Game is not active"}
        room_data["paused"] = False
        room_data["remaining_time"] = None
        room_data["question_index"] += 1
        room_data["current_payload"] = None
        room_data["current_view"] = "question"
        await send_question(room)
        log_event("admin_next_question", room, question_index=room_data["question_index"])
        return {"ok": True, "question_index": room_data["question_index"]}

    if action == "close_room":
        closed = await close_room_and_notify(room, "Комната закрыта из admin")
        log_event("admin_close_room", room, closed=closed)
        return {"ok": bool(closed)}

    if action == "end_game":
        player_items = list(room_data.get("players", {}).items())
        await asyncio.gather(*[
            safe_send_json(room_data, ws_player, {
                "type": "test_aborted",
                "message": "Тест завершен из admin"
            }, f"player:{username_player}")
            for username_player, ws_player in player_items
        ], return_exceptions=True)
        await asyncio.gather(*[
            ws_player.close()
            for _, ws_player in player_items
        ], return_exceptions=True)
        room_data["players"] = {}
        room_data["scores"] = {}
        room_data["answers"] = {}
        room_data["question_index"] = 0
        room_data["status"] = "waiting"
        room_data["team_mode"] = False
        room_data["team_size"] = 2
        room_data["teams"] = []
        room_data["team_assignments"] = {}
        room_data["paused"] = False
        room_data["remaining_time"] = None
        room_data["revealed"] = False
        room_data["appeals"] = []
        room_data["appeal_dedup"] = {}
        room_data["question_history"] = []
        room_data["current_question_metrics"] = {}
        room_data["player_flags"] = {}
        room_data["current_payload"] = None
        room_data["current_view"] = "waiting"
        if room_data.get("host"):
            await safe_send_json(room_data, room_data["host"], {
                "type": "return_to_lobby",
                "message": "Тест завершён из admin. Игроки удалены."
            }, "host")
        log_event("admin_end_game", room)
        return {"ok": True}

    if action == "snapshot":
        return {"ok": True, "snapshot": build_room_snapshot(room, room_data)}

    return {"ok": False, "error": f"Unknown action: {action}"}

@app.get("/create-room/{quiz}")
async def create_room(quiz: str, request: Request):
    # ===== РАСФОРМИРОВАНИЕ СУЩЕСТВУЮЩИХ КОМНАТ =====
    for existing_pin in list(rooms.keys()):
        room_data = rooms[existing_pin]
        player_items = list(room_data.get("players", {}).items())

        # уведомляем всех игроков
        await asyncio.gather(*[
            safe_send_json(room_data, ws, {
                "type": "room_closed",
                "message": "Комната расформирована ведущим"
            }, f"player:{username}")
            for username, ws in player_items
        ], return_exceptions=True)

        # уведомляем хоста (если есть)
        if room_data.get("host"):
            await safe_send_json(room_data, room_data["host"], {
                "type": "room_closed",
                "message": "Комната расформирована"
            }, "host")

        # даём фронту время обработать сообщение
        await asyncio.sleep(0.15)

        # закрываем все соединения игроков
        await asyncio.gather(*[
            ws.close()
            for _, ws in player_items
        ], return_exceptions=True)

        # закрываем соединение хоста
        if room_data.get("host"):
            try:
                await room_data["host"].close()
            except:
                pass

        # удаляем комнату
        del rooms[existing_pin]
        await broadcast_admin({"type": "room_removed", "room": existing_pin})

    # ===== СОЗДАНИЕ НОВОЙ КОМНАТЫ =====
    pin = generate_pin()

    rooms[pin] = {
        "host": None,
        "players": {},
        "scores": {},
        "streaks": {},
        "answers": {},
        "quiz": quiz,
        "quiz_questions": build_room_quiz(quiz),
        "team_mode": False,
        "team_size": 2,
        "teams": [],
        "team_assignments": {},
        "question_index": 0,
        "status": "waiting",
        "question_start": None,
        "question_duration": 30,
        "paused": False,
        "remaining_time": None,
        "appeals": [],
        "appeal_dedup": {},
        "disconnected": {},  # map username -> last_seen_ts
        "disconnect_grace": 180,  # seconds
        "question_history": [],
        "current_question_metrics": {},
        "player_flags": {},
        "late_joiners": {},
        "current_payload": None,
        "current_view": "waiting",
    }

    log_event("room_created", pin, quiz=quiz)

    return {"pin": pin}


@app.post("/close-room/{room}")
async def close_room(room: str):
    closed = await close_room_and_notify(room, "Тест завершен")
    return {"ok": closed}

@app.websocket("/ws/{role}/{room}/{username}")
async def websocket_endpoint(websocket: WebSocket, role: str, room: str, username: str):
    await websocket.accept()

    if room not in rooms:
        await websocket.send_json({
            "type": "error",
            "message": "Комната с таким кодом не существует"
        })
        await websocket.close()
        return

    if room not in rooms:
        return
    room_data = rooms[room]
    log_event("socket_connected", room, role=role, username=username)
    

    if role == "host":
        room_data["host"] = websocket
        log_event("host_attached", room, username=username)
        await update_players(room)
    else:
        # === Player connect / reconnect handling ===
        # Allow reconnect with the same username (common mobile background/screen-off case).
        # New players are only allowed while waiting.

        existing_ws = room_data.get("players", {}).get(username)
        existing_score = room_data.get("scores", {}).get(username)
        is_known_player = existing_score is not None
        was_disconnected = username in room_data.get("disconnected", {})

        # If there is an existing socket for this username, replace it (reconnect)
        if existing_ws is not None:
            try:
                await existing_ws.close()
            except Exception:
                pass

        room_data.setdefault("players", {})[username] = websocket
        log_event("player_attached", room, username=username, known_player=is_known_player, status=room_data.get("status"))

        # Preserve score on reconnect, otherwise initialize
        if existing_score is None:
            room_data.setdefault("scores", {})[username] = 0
            room_data.setdefault("late_joiners", {})[username] = room_data.get("status") != "waiting"
        elif username not in room_data.setdefault("late_joiners", {}):
            room_data["late_joiners"][username] = False

        # Clear disconnected marker on successful reconnect
        room_data.setdefault("disconnected", {}).pop(username, None)

        if room_data.get("status") == "waiting":
            await websocket.send_json({"type": "waiting"})
            await websocket.send_json({
                "type": "team_assignment",
                "enabled": bool(room_data.get("team_mode")),
                "team_size": int(room_data.get("team_size", 2)),
                "teams": room_data.get("teams", []),
                "team_leaderboard": compute_team_leaderboard(room_data),
            })
        else:
            current_payload = room_data.get("current_payload")
            if isinstance(current_payload, dict):
                payload_to_send = dict(current_payload)
                if room_data.get("current_view") == "question":
                    if payload_to_send.get("question_type") == "wordle":
                        payload_to_send["wordle_hint_prefix"] = str((room_data.get("wordle_hints") or {}).get(username, "") or "")
                        payload_to_send["wordle_hint_cost"] = WORDLE_HINT_COST
                    if room_data.get("paused"):
                        payload_to_send["time"] = max(0, int(round(float(room_data.get("remaining_time") or 0))))
                    else:
                        question_start = room_data.get("question_start")
                        question_duration = float(room_data.get("question_duration") or 0)
                        if question_start:
                            elapsed = time.time() - float(question_start)
                            remaining = max(0.0, question_duration - elapsed)
                            payload_to_send["time"] = max(0, int(remaining + 0.999))
                await websocket.send_json(payload_to_send)
                if room_data.get("paused") and room_data.get("current_view") == "question":
                    await websocket.send_json({
                        "type": "paused",
                        "time": room_data.get("remaining_time", 0)
                    })
                await websocket.send_json({
                    "type": "team_assignment",
                    "enabled": bool(room_data.get("team_mode")),
                    "team_size": int(room_data.get("team_size", 2)),
                    "teams": room_data.get("teams", []),
                    "team_leaderboard": compute_team_leaderboard(room_data),
                })

        if was_disconnected:
            metrics = room_data.setdefault("current_question_metrics", {}).get(username)
            if isinstance(metrics, dict) and metrics.get("offline_started_at"):
                metrics["offline_total_ms"] = float(metrics.get("offline_total_ms", 0) or 0) + max(0.0, time.time() - float(metrics["offline_started_at"])) * 1000
                metrics["offline_started_at"] = None
            await notify_host_player_connection(room, username, "online")
            log_event("player_reconnected", room, username=username)
        await update_players(room)

    try:
        while True:
            data = await websocket.receive_json()

            # === Keep-alive / background safety ===
            msg_type = data.get("type") if isinstance(data, dict) else None
            if msg_type in {"ping", "pong", "resume"}:
                if msg_type == "ping":
                    try:
                        await websocket.send_json({"type": "pong", "t": data.get("t")})
                    except Exception:
                        pass
                # Mark as alive
                if role == "player":
                    metrics = room_data.setdefault("current_question_metrics", {}).get(username)
                    if isinstance(metrics, dict) and metrics.get("offline_started_at"):
                        metrics["offline_total_ms"] = float(metrics.get("offline_total_ms", 0) or 0) + max(0.0, time.time() - float(metrics["offline_started_at"])) * 1000
                        metrics["offline_started_at"] = None
                        add_player_timeline_event(room_data, username, "back_online")
                    room_data.setdefault("disconnected", {}).pop(username, None)
                continue

            if role == "player" and msg_type == "tab_hidden":
                metrics = room_data.setdefault("current_question_metrics", {}).setdefault(username, {
                    "tab_switches": 0,
                    "response_time_ms": None,
                    "answered_at": None,
                    "offline_total_ms": 0,
                    "offline_started_at": None,
                    "offline_before_answer_ms": 0,
                })
                metrics["tab_switches"] = int(metrics.get("tab_switches", 0) or 0) + 1
                if room_data.get("current_view") == "question" and not metrics.get("offline_started_at"):
                    metrics["offline_started_at"] = time.time()
                room_data.setdefault("disconnected", {})[username] = time.time()
                log_event(
                    "player_tab_hidden",
                    room,
                    username=username,
                    question_index=room_data.get("question_index"),
                    tab_switches=metrics["tab_switches"],
                )
                add_player_timeline_event(room_data, username, "went_offline", tab_switches=metrics["tab_switches"])
                continue

            # Guard against malformed payloads
            if not msg_type:
                continue

            # APPEAL REQUEST (player -> host)
            if role == "player" and data.get("type") == "appeal_request":
                # Accept flexible payload from player.html
                reason = (data.get("reason") or "").strip()
                answer_value = data.get("answer")
                q_index = data.get("question_index")

                # Try to infer current question index if not provided
                if q_index is None:
                    q_index = room_data.get("question_index", 0)

                try:
                    q_index_int = int(q_index)
                except Exception:
                    q_index_int = int(room_data.get("question_index", 0))

                # Deduplicate: 1 appeal per user per question
                dedup = room_data.setdefault("appeal_dedup", {})
                dedup_key = f"{username}:{q_index_int}"
                if dedup.get(dedup_key):
                    # Already appealed for this question
                    await websocket.send_json({
                        "type": "appeal_received",
                        "ok": False,
                        "message": "Апелляция по этому вопросу уже отправлена."
                    })
                    continue
                dedup[dedup_key] = True

                # If player didn't send answer explicitly, use server-stored answer
                if answer_value is None and username in room_data.get("answers", {}):
                    stored = room_data["answers"].get(username) or {}
                    # Prefer text/numeric value
                    if stored.get("value") is not None:
                        answer_value = stored.get("value")
                    else:
                        # For mcq/multi, store selected/selected_list
                        if stored.get("selected_list") is not None:
                            answer_value = stored.get("selected_list")
                        else:
                            answer_value = stored.get("selected")

                # Collect question context (safe even if index out of range)
                quiz_name = room_data.get("quiz")
                quiz_list = room_data.get("quiz_questions") or (QUIZZES.get(quiz_name, []) if quiz_name else [])
                q_obj = quiz_list[q_index_int] if 0 <= q_index_int < len(quiz_list) else None

                question_points = (q_obj.get("points", 1000) if isinstance(q_obj, dict) else 1000)
                question_type = (q_obj.get("type", "mcq") if isinstance(q_obj, dict) else "mcq")
                question_text = (q_obj.get("question") if isinstance(q_obj, dict) else None)
                correct_raw = (q_obj.get("correct") if isinstance(q_obj, dict) else None)

                appeal_item = {
                    "ts": time.time(),
                    "room": room,
                    "username": username,
                    "question_index": q_index_int,
                    "question_type": question_type,
                    "question": question_text,
                    "answer": answer_value,
                    "reason": reason,
                    "points": question_points,
                    "correct": correct_raw,
                }

                appeals = room_data.setdefault("appeals", [])
                appeals.append(appeal_item)
                appeal_item["appeal_id"] = len(appeals) - 1
                log_event("appeal_requested", room, username=username, question_index=q_index_int, reason=reason)
                add_player_timeline_event(room_data, username, "appeal_requested", reason=reason, question=question_text)

                # Notify host immediately
                if room_data.get("host"):
                    try:
                        await room_data["host"].send_json({
                            "type": "appeal_request",
                            **appeal_item
                        })
                    except Exception:
                        pass

                # Acknowledge player
                await websocket.send_json({
                    "type": "appeal_received",
                    "ok": True,
                    "message": "Апелляция отправлена ведущему."
                })
                continue

            if role == "player" and data.get("type") == "buy_wordle_hint":
                quiz = room_data.get("quiz_questions") or QUIZZES.get(room_data.get("quiz"), [])
                question_index = int(room_data.get("question_index", 0) or 0)
                current_question = quiz[question_index] if 0 <= question_index < len(quiz) else {}
                if room_data.get("current_view") != "question" or current_question.get("type") != "wordle":
                    await websocket.send_json({"type": "wordle_hint_result", "ok": False, "message": "Подсказка доступна только во время wordle-вопроса."})
                    continue
                answer_state = room_data.get("answers", {}).get(username) or {}
                if isinstance(answer_state, dict) and answer_state.get("finalized"):
                    await websocket.send_json({"type": "wordle_hint_result", "ok": False, "message": "Для этого слова подсказка уже недоступна."})
                    continue
                target = apply_alias(normalize_answer(current_question.get("correct")))
                if not target:
                    await websocket.send_json({"type": "wordle_hint_result", "ok": False, "message": "Не удалось определить слово."})
                    continue
                hints = room_data.setdefault("wordle_hints", {})
                current_prefix = str(hints.get(username, "") or "")
                if len(current_prefix) >= len(target):
                    await websocket.send_json({"type": "wordle_hint_result", "ok": False, "message": "Все буквы уже открыты."})
                    continue
                current_score = int(room_data.get("scores", {}).get(username, 0) or 0)
                if current_score < WORDLE_HINT_COST:
                    await websocket.send_json({"type": "wordle_hint_result", "ok": False, "message": f"Нужно минимум {WORDLE_HINT_COST} очков."})
                    continue
                next_prefix = target[:len(current_prefix) + 1]
                hints[username] = next_prefix
                room_data["scores"][username] = current_score - WORDLE_HINT_COST
                leaderboard = sorted(room_data["scores"].items(), key=lambda x: x[1], reverse=True)
                await safe_send_json(room_data, websocket, {
                    "type": "wordle_hint_update",
                    "ok": True,
                    "revealed_prefix": next_prefix,
                    "revealed_count": len(next_prefix),
                    "cost": WORDLE_HINT_COST,
                }, f"player:{username}")
                await broadcast(room, {
                    "type": "appeal_awarded",
                    "username": username,
                    "delta": -WORDLE_HINT_COST,
                    "reason": "Wordle hint purchase",
                    "leaderboard": leaderboard,
                    "team_leaderboard": compute_team_leaderboard(room_data),
                    "total_scores": room_data["scores"],
                })
                log_event("wordle_hint_purchased", room, username=username, revealed_count=len(next_prefix), cost=WORDLE_HINT_COST)
                continue

            # APPEAL (host manual score adjustment)
            if role == "host" and data.get("type") == "appeal_award":
                target = data.get("username")
                delta_raw = data.get("delta")
                reason = (data.get("reason") or "").strip()

                # Validate payload
                if not target or delta_raw is None:
                    await websocket.send_json({
                        "type": "error",
                        "message": "appeal_award requires username and delta"
                    })
                    continue

                try:
                    delta = int(delta_raw)
                except Exception:
                    await websocket.send_json({
                        "type": "error",
                        "message": "delta must be an integer"
                    })
                    continue

                # Allow appeals only during active game or right after reveal (also OK in finished state)
                # Ensure player exists in score table
                if target not in room_data.get("scores", {}):
                    await websocket.send_json({
                        "type": "error",
                        "message": "Player not found in scores"
                    })
                    continue

                room_data["scores"][target] += delta
                log_event("appeal_awarded", room, username=target, delta=delta, reason=reason)

                # Log
                room_data.setdefault("appeals", []).append({
                    "ts": time.time(),
                    "username": target,
                    "delta": delta,
                    "reason": reason
                })

                leaderboard = sorted(
                    room_data["scores"].items(),
                    key=lambda x: x[1],
                    reverse=True
                )

                await broadcast(room, {
                    "type": "appeal_awarded",
                    "username": target,
                    "delta": delta,
                    "reason": reason,
                    "leaderboard": leaderboard,
                    "team_leaderboard": compute_team_leaderboard(room_data),
                    "total_scores": room_data["scores"],
                })
                continue

            if role == "host" and data.get("type") == "set_team_mode":
                if room_data.get("status") != "waiting":
                    continue
                enabled = bool(data.get("enabled"))
                room_data["team_mode"] = enabled
                log_event("team_mode_changed", room, enabled=enabled)
                if not enabled:
                    room_data["teams"] = []
                    room_data["team_assignments"] = {}
                await update_players(room)
                await broadcast_team_update(room)
                continue

            if role == "host" and data.get("type") == "set_team_size":
                if room_data.get("status") != "waiting":
                    continue
                try:
                    team_size = int(data.get("team_size", 2))
                except Exception:
                    team_size = 2
                room_data["team_size"] = 2 if team_size < 2 else 4 if team_size > 4 else team_size
                room_data["team_mode"] = True
                log_event("team_size_changed", room, team_size=room_data["team_size"])
                room_data["teams"] = []
                room_data["team_assignments"] = {}
                await update_players(room)
                await broadcast_team_update(room)
                continue

            if role == "host" and data.get("type") == "shuffle_teams":
                if room_data.get("status") != "waiting":
                    continue
                active_players = [
                    p for p in room_data.get("players", {}).keys()
                    if p != "HOST" and p not in room_data.get("disconnected", {})
                ]
                if not active_players:
                    continue
                room_data["team_mode"] = True
                team_size = int(room_data.get("team_size", 2))
                shuffled = active_players[:]
                random.shuffle(shuffled)
                teams = []
                assignments = {}
                for idx in range(0, len(shuffled), team_size):
                    members = shuffled[idx:idx + team_size]
                    team_id = len(teams) + 1
                    team_meta = build_team_meta(len(teams))
                    team = {
                        "id": team_id,
                        "name": team_meta["name"],
                        "color": team_meta["color"],
                        "members": members,
                    }
                    teams.append(team)
                    for member in members:
                        assignments[member] = team_id
                room_data["teams"] = teams
                room_data["team_assignments"] = assignments
                log_event("teams_shuffled", room, teams_count=len(teams), players_count=len(active_players))
                await update_players(room)
                await broadcast_team_update(room)
                continue

            # ANSWER (player OR host)
            if msg_type == "answer" and (role == "player" or role == "host"):

                answer_user = username if role == "player" else "HOST"

                # ensure HOST exists in scores
                if role == "host" and answer_user not in room_data["scores"]:
                    room_data["scores"][answer_user] = 0
                quiz = room_data.get("quiz_questions") or QUIZZES.get(room_data.get("quiz"), [])
                question_index = room_data.get("question_index", 0)
                current_question = quiz[question_index] if 0 <= question_index < len(quiz) else {}
                q_type = current_question.get("type", "mcq")

                current_time = time.time()
                elapsed = current_time - room_data["question_start"]
                remaining = max(0, room_data["question_duration"] - elapsed)

                if q_type == "wordle":
                    target_raw = current_question.get("correct")
                    target = apply_alias(normalize_answer(target_raw))
                    guess_raw = data.get("value")
                    guess = apply_alias(normalize_answer(guess_raw))
                    max_attempts = max(1, int(current_question.get("max_attempts", 6)))
                    existing = room_data["answers"].get(answer_user)

                    if not target or not guess or len(guess) != len(target):
                        continue
                    if existing and existing.get("finalized"):
                        continue

                    attempts = list(existing.get("attempts", [])) if isinstance(existing, dict) else []
                    if len(attempts) >= max_attempts:
                        continue

                    attempt_entry = {
                        "guess": guess,
                        "statuses": evaluate_wordle_guess(target, guess),
                    }
                    attempts.append(attempt_entry)
                    solved = (guess == target)
                    finalized = solved or len(attempts) >= max_attempts

                    room_data["answers"][answer_user] = {
                        "value": guess,
                        "attempts": attempts,
                        "remaining": remaining,
                        "finalized": finalized,
                        "solved": solved,
                    }
                    metrics = room_data.setdefault("current_question_metrics", {}).setdefault(answer_user, {
                        "tab_switches": 0,
                        "response_time_ms": None,
                        "answered_at": None,
                        "offline_total_ms": 0,
                        "offline_started_at": None,
                        "offline_before_answer_ms": 0,
                        "manual_suspicious": False,
                        "manual_suspicious_note": "",
                    })
                    if metrics.get("offline_started_at"):
                        metrics["offline_total_ms"] = float(metrics.get("offline_total_ms", 0) or 0) + max(0.0, current_time - float(metrics["offline_started_at"])) * 1000
                        metrics["offline_started_at"] = None
                    if metrics.get("offline_before_answer_ms") in {None, 0}:
                        metrics["offline_before_answer_ms"] = float(metrics.get("offline_total_ms", 0) or 0)
                    offline_before_answer_seconds = round(float(metrics.get("offline_before_answer_ms", 0) or 0) / 1000, 2)
                    if finalized or metrics.get("response_time_ms") is None:
                        metrics["answered_at"] = current_time
                        metrics["response_time_ms"] = int(max(0.0, elapsed) * 1000)
                    room_data["answers"][answer_user]["offline_before_answer_seconds"] = offline_before_answer_seconds
                    log_event("wordle_answer", room, username=answer_user, attempts=len(attempts), solved=solved, finalized=finalized)
                    add_player_timeline_event(
                        room_data,
                        answer_user,
                        "answered",
                        answer=guess,
                        response_time_ms=metrics.get("response_time_ms"),
                        offline_before_answer_seconds=offline_before_answer_seconds,
                    )

                    total_expected = len(room_data["players"])
                    finalized_players = [
                        player_name for player_name in room_data["players"].keys()
                        if room_data["answers"].get(player_name, {}).get("finalized")
                    ]
                    await broadcast(room, {
                        "type": "progress",
                        "answered": len(finalized_players),
                        "total": total_expected,
                        "answered_players": finalized_players
                    })

                    try:
                        await websocket.send_json({
                            "type": "wordle_feedback",
                            "attempts": attempts,
                            "attempts_used": len(attempts),
                            "attempts_left": max(0, max_attempts - len(attempts)),
                            "max_attempts": max_attempts,
                            "solved": solved,
                            "finalized": finalized,
                        })
                    except Exception:
                        pass

                    if len(finalized_players) >= total_expected:
                        await broadcast(room, {"type": "all_answered"})
                        current_index = room_data["question_index"]
                        asyncio.create_task(reveal_after_delay(room, 0, current_index))
                    continue

                if answer_user not in room_data["answers"]:
                    room_data["answers"][answer_user] = {
                        "value": data.get("value"),
                        "selected": data.get("selected"),
                        "selected_list": data.get("selected_list"),
                        "remaining": remaining
                    }
                    metrics = room_data.setdefault("current_question_metrics", {}).setdefault(answer_user, {
                        "tab_switches": 0,
                        "response_time_ms": None,
                        "answered_at": None,
                        "offline_total_ms": 0,
                        "offline_started_at": None,
                        "offline_before_answer_ms": 0,
                        "manual_suspicious": False,
                        "manual_suspicious_note": "",
                    })
                    if metrics.get("offline_started_at"):
                        metrics["offline_total_ms"] = float(metrics.get("offline_total_ms", 0) or 0) + max(0.0, current_time - float(metrics["offline_started_at"])) * 1000
                        metrics["offline_started_at"] = None
                    if metrics.get("offline_before_answer_ms") in {None, 0}:
                        metrics["offline_before_answer_ms"] = float(metrics.get("offline_total_ms", 0) or 0)
                    offline_before_answer_seconds = round(float(metrics.get("offline_before_answer_ms", 0) or 0) / 1000, 2)
                    metrics["answered_at"] = current_time
                    metrics["response_time_ms"] = int(max(0.0, elapsed) * 1000)
                    room_data["answers"][answer_user]["offline_before_answer_seconds"] = offline_before_answer_seconds
                    log_event("answer_received", room, username=answer_user, question_type=q_type)
                    add_player_timeline_event(
                        room_data,
                        answer_user,
                        "answered",
                        answer=data.get("value", data.get("selected_list", data.get("selected"))),
                        response_time_ms=metrics.get("response_time_ms"),
                        offline_before_answer_seconds=offline_before_answer_seconds,
                    )

                    # live progress for host/player screens
                    total_expected = len(room_data["players"])
                    answered_count = sum(
                        1 for player_name in room_data["players"].keys()
                        if player_name in room_data["answers"]
                    )
                    answered_players = [
                        player_name for player_name in room_data["players"].keys()
                        if player_name in room_data["answers"]
                    ]

                    await broadcast(room, {
                        "type": "progress",
                        "answered": answered_count,
                        "total": total_expected,
                        "answered_players": answered_players
                    })

                    # early reveal only if ALL players answered (host optional)
                    if len(room_data["answers"]) >= total_expected:
                        await broadcast(room, {"type": "all_answered"})
                        current_index = room_data["question_index"]
                        asyncio.create_task(reveal_after_delay(room, 0, current_index))

            # START GAME
            if role == "host" and msg_type == "start":
                if room_data.get("team_mode") and not room_data.get("teams"):
                    await websocket.send_json({
                        "type": "error",
                        "message": "Сначала перемешайте команды"
                    })
                    continue
                room_data["status"] = "active"
                room_data["paused"] = False
                room_data["remaining_time"] = None
                room_data["current_payload"] = None
                room_data["current_view"] = "question"
                log_event("game_started", room, quiz=room_data.get("quiz"))
                await send_question(room)

            # PAUSE / RESUME
            if role == "host" and data["type"] == "pause":
                if not room_data["paused"]:
                    elapsed = time.time() - room_data["question_start"]
                    remaining = max(0, room_data["question_duration"] - elapsed)

                    room_data["remaining_time"] = remaining
                    room_data["paused"] = True

                    await broadcast(room, {
                        "type": "paused",
                        "time": remaining
                    })
                    log_event("game_paused", room, remaining=round(remaining, 3))
                else:
                    room_data["paused"] = False

                    room_data["question_start"] = time.time()
                    room_data["question_duration"] = room_data["remaining_time"]

                    await broadcast(room, {
                        "type": "resume",
                        "time": room_data["remaining_time"]
                    })
                    log_event("game_resumed", room, remaining=room_data["remaining_time"])

            # KICK PLAYER
            if role == "host" and data["type"] == "kick":
                user = data["username"]
                if user in room_data["players"]:
                    # уведомляем игрока
                    await room_data["players"][user].send_json({
                        "type": "kicked"
                    })
                    await room_data["players"][user].close()

                    del room_data["players"][user]
                    del room_data["scores"][user]
                    room_data.get("current_question_metrics", {}).pop(user, None)
                    room_data.get("player_flags", {}).pop(user, None)
                    room_data.get("late_joiners", {}).pop(user, None)
                    remove_player_from_teams(room_data, user)
                    log_event("player_kicked", room, username=user)

                    await update_players(room)
                    await broadcast_team_update(room)

            # NEXT
            if role == "host" and data["type"] == "next":
                if room_data["status"] != "active":
                    continue

                room_data["paused"] = False
                room_data["remaining_time"] = None

                room_data["question_index"] += 1
                room_data["current_payload"] = None
                room_data["current_view"] = "question"
                log_event("next_question", room, question_index=room_data["question_index"])
                await send_question(room)

            if role == "host" and data["type"] == "force_reveal":
                if room_data["status"] != "active":
                    continue
                log_event("force_reveal", room, question_index=room_data["question_index"])
                await send_reveal(room, room_data["question_index"])

            # END GAME (manual by host)
            if role == "host" and data["type"] == "end_game":
                if room not in rooms:
                    continue

                room_data = rooms[room]

                # Notify and kick ALL players
                player_items = list(room_data["players"].items())
                await asyncio.gather(*[
                    safe_send_json(room_data, ws_player, {
                        "type": "test_aborted",
                        "message": "Тест завершен"
                    }, f"player:{username_player}")
                    for username_player, ws_player in player_items
                ], return_exceptions=True)
                await asyncio.gather(*[
                    ws_player.close()
                    for _, ws_player in player_items
                ], return_exceptions=True)

                # Clear only players and game state
                room_data["players"] = {}
                room_data["scores"] = {}
                room_data["answers"] = {}
                room_data["question_index"] = 0
                room_data["status"] = "waiting"
                room_data["team_mode"] = False
                room_data["team_size"] = 2
                room_data["teams"] = []
                room_data["team_assignments"] = {}
                room_data["paused"] = False
                room_data["remaining_time"] = None
                room_data["revealed"] = False
                room_data["appeals"] = []
                room_data["appeal_dedup"] = {}
                room_data["question_history"] = []
                room_data["current_payload"] = None
                room_data["current_view"] = "waiting"

                # Inform host to reset UI (but DO NOT close host socket)
                await websocket.send_json({
                    "type": "return_to_lobby",
                    "message": "Тест завершён. Игроки удалены."
                })
                log_event("game_ended_by_host", room)

                continue

    except WebSocketDisconnect:
        # Очистка при отключении
        if room in rooms:
            room_data = rooms[room]

            if role == "player" and room_data.get("players", {}).get(username) is websocket:
                # Mark disconnected, keep score/state for a short grace window.
                room_data.setdefault("disconnected", {})[username] = time.time()
                metrics = room_data.setdefault("current_question_metrics", {}).setdefault(username, {
                    "tab_switches": 0,
                    "response_time_ms": None,
                    "answered_at": None,
                    "offline_total_ms": 0,
                    "offline_started_at": None,
                })
                if room_data.get("current_view") == "question" and not metrics.get("offline_started_at"):
                    metrics["offline_started_at"] = time.time()
                log_event("player_disconnected", room, username=username)
                await notify_host_player_connection(room, username, "offline")
                await update_players(room)

                async def _cleanup_if_not_back(pin: str, user: str):
                    await asyncio.sleep(int(rooms.get(pin, {}).get("disconnect_grace", 180)))
                    if pin not in rooms:
                        return
                    rd = rooms[pin]
                    last_seen = rd.get("disconnected", {}).get(user)
                    if not last_seen:
                        return  # reconnected
                    # Still disconnected -> remove
                    ws_old = rd.get("players", {}).get(user)
                    try:
                        if ws_old:
                            await ws_old.close()
                    except Exception:
                        pass
                    rd.get("players", {}).pop(user, None)
                    rd.get("scores", {}).pop(user, None)
                    rd.get("answers", {}).pop(user, None)
                    rd.get("appeal_dedup", {}).pop(user, None)
                    rd.get("disconnected", {}).pop(user, None)
                    rd.get("current_question_metrics", {}).pop(user, None)
                    rd.get("player_flags", {}).pop(user, None)
                    rd.get("late_joiners", {}).pop(user, None)
                    remove_player_from_teams(rd, user)
                    log_event("player_removed_after_grace", pin, username=user)
                    await update_players(pin)
                    await broadcast_team_update(pin)

                asyncio.create_task(_cleanup_if_not_back(room, username))

            if role == "host":
                # Если ведущий отключился — завершить тест для всех игроков
                log_event("host_disconnected", room, username=username)
                await broadcast(room, {
                    "type": "test_aborted",
                    "message": "Тест завершен"
                })

                await asyncio.gather(*[
                    ws.close()
                    for ws in room_data.get("players", {}).values()
                ], return_exceptions=True)

                del rooms[room]
                await broadcast_admin({"type": "room_removed", "room": room})
                log_event("room_closed_after_host_disconnect", room)


@app.websocket("/ws/admin")
async def admin_websocket(websocket: WebSocket):
    await websocket.accept()
    client_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    admin_clients[client_id] = {
        "ws": websocket,
        "lock": asyncio.Lock(),
    }
    await safe_send_admin(client_id, {
        "type": "admin_init",
        **build_server_snapshot(),
    })
    log_event("admin_connected", client_id=client_id)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type") if isinstance(data, dict) else None
            if msg_type == "admin_ping":
                await safe_send_admin(client_id, {"type": "admin_pong", "ts": time.time()})
                continue
            if msg_type == "admin_refresh":
                await safe_send_admin(client_id, {
                    "type": "admin_init",
                    **build_server_snapshot(),
                })
                continue
            if msg_type == "admin_action":
                room = str(data.get("room") or "").strip()
                action = str(data.get("action") or "").strip()
                result = await execute_admin_action(room, action, data)
                await safe_send_admin(client_id, {
                    "type": "admin_action_result",
                    "room": room,
                    "action": action,
                    "result": result,
                })
                continue
    except WebSocketDisconnect:
        pass
    finally:
        admin_clients.pop(client_id, None)
        log_event("admin_disconnected", client_id=client_id)

async def send_question(room):
    if room not in rooms:
        return
    room_data = rooms[room]
    quiz = room_data.get("quiz_questions") or QUIZZES[room_data["quiz"]]
    index = room_data["question_index"]

    if index >= len(quiz):
        # Финальный leaderboard
        sorted_scores = sorted(room_data["scores"].items(), key=lambda x: x[1], reverse=True)

        game_over_payload = {
            "type": "game_over",
            "leaderboard": sorted_scores,
            "team_leaderboard": compute_team_leaderboard(room_data),
            "streaks": room_data.get("streaks", {})
        }
        await broadcast(room, game_over_payload)
        room_data["current_payload"] = game_over_payload
        room_data["current_view"] = "game_over"
        log_event("game_over", room, players=len(sorted_scores))

        # ⚠️ НЕ удаляем комнату автоматически
        # Ожидаем, пока ведущий создаст новую игру
        room_data["status"] = "finished"
        return

    question = quiz[index]
    # --- Auto numbering & clean manual numbering ---
    raw_question_text = question["question"]
    cleaned_question_text = re.sub(r'^\d+\.\s*', '', raw_question_text).strip()
    display_question_text = f"{index + 1}. {cleaned_question_text}"

    room_data["answers"] = {}
    room_data["wordle_hints"] = {}
    room_data["revealed"] = False
    room_data["current_question_metrics"] = {
        username: {
            "tab_switches": 0,
            "response_time_ms": None,
            "answered_at": None,
            "offline_total_ms": 0,
            "offline_started_at": None,
            "offline_before_answer_ms": 0,
            "manual_suspicious": False,
            "manual_suspicious_note": "",
        }
        for username in room_data.get("players", {}).keys()
        if username != "HOST"
    }
    room_data.setdefault("appeal_dedup", {})
    # clear dedup for this question only
    # (keep dict but remove any keys for previous questions)
    room_data["appeal_dedup"] = {}

    # Полный сброс состояния таймера
    room_data["paused"] = False
    room_data["remaining_time"] = None

    question_time = question.get("time", 30)
    room_data["question_start"] = time.time()
    room_data["question_duration"] = question_time

    # Determine max_select (allow selecting only number of correct answers if multi-correct)
    max_select = 1
    if question.get("type") == "mcq" and isinstance(question.get("correct"), list):
        max_select = len(question.get("correct"))

    await broadcast(room, {
        "type": "question",
        "question_type": question.get("type", "mcq"),
        "question": display_question_text,
        "prompt": question.get("prompt"),
        "answers": question.get("answers"),
        "image": question.get("image"),
        "audio": question.get("audio"),
        "image_zoom": question.get("image_zoom"),
        "image_position": question.get("image_position"),
        "image_offset": question.get("image_offset"),
        "points": question.get("points", 1000),
        "time": question_time,
        "max_select": max_select,
        "word_length": len(apply_alias(normalize_answer(question.get("correct")))) if question.get("type") == "wordle" else None,
        "max_attempts": max(1, int(question.get("max_attempts", 6))) if question.get("type") == "wordle" else None,
        "wordle_hint_cost": WORDLE_HINT_COST if question.get("type") == "wordle" else None,
    })
    room_data["current_payload"] = {
        "type": "question",
        "question_type": question.get("type", "mcq"),
        "question": display_question_text,
        "prompt": question.get("prompt"),
        "answers": question.get("answers"),
        "image": question.get("image"),
        "audio": question.get("audio"),
        "image_zoom": question.get("image_zoom"),
        "image_position": question.get("image_position"),
        "image_offset": question.get("image_offset"),
        "points": question.get("points", 1000),
        "time": question_time,
        "max_select": max_select,
        "word_length": len(apply_alias(normalize_answer(question.get("correct")))) if question.get("type") == "wordle" else None,
        "max_attempts": max(1, int(question.get("max_attempts", 6))) if question.get("type") == "wordle" else None,
        "wordle_hint_cost": WORDLE_HINT_COST if question.get("type") == "wordle" else None,
    }
    room_data["current_view"] = "question"
    for username in room_data.get("players", {}).keys():
        if username == "HOST":
            continue
        add_player_timeline_event(room_data, username, "question_opened", question=display_question_text, question_type=question.get("type", "mcq"))

    current_index = room_data["question_index"]
    log_event("question_sent", room, question_index=index, question_type=question.get("type", "mcq"), time=question_time)
    asyncio.create_task(reveal_after_delay(room, question_time, current_index))

async def send_reveal(room, question_index):
    if room not in rooms:
        return
    room_data = rooms[room]
    if question_index != room_data["question_index"]:
        return
    if room_data.get("revealed"):
        return
    room_data["revealed"] = True

    quiz = room_data.get("quiz_questions") or QUIZZES[room_data["quiz"]]
    question = quiz[question_index]
    raw_question_text = question["question"]
    cleaned_question_text = re.sub(r'^\d+\.\s*', '', raw_question_text).strip()
    display_question_text = f"{question_index + 1}. {cleaned_question_text}"
    q_type = question.get("type", "mcq")
    correct_value = question.get("correct")

    if q_type in ["mcq", "fastest"]:
        if not isinstance(correct_value, list):
            correct = [correct_value]
        else:
            correct = correct_value
    elif q_type == "text":
        if isinstance(correct_value, list):
            correct = correct_value
        else:
            correct = [correct_value]
    elif q_type == "wordle":
        correct = correct_value
    else:
        correct = correct_value

    stats = {i: 0 for i in range(len(question.get("answers", [])))}
    for user, data_answer in room_data["answers"].items():
        selected = data_answer.get("selected")
        selected_list = data_answer.get("selected_list")

        if selected_list is not None:
            for idx in selected_list:
                if idx in stats:
                    stats[idx] += 1
        elif selected in stats:
            stats[selected] += 1

    users_answers = {}
    for user, data_answer in room_data["answers"].items():
        if q_type in {"numeric", "text"}:
            users_answers[user] = data_answer.get("value")
        elif q_type == "wordle":
            users_answers[user] = data_answer.get("attempts", [])
        elif data_answer.get("selected_list") is not None:
            users_answers[user] = data_answer.get("selected_list")
        else:
            users_answers[user] = data_answer.get("selected")

    points_awarded = {}
    fixed_points = question.get("points", 1000)

    for user, data_answer in room_data["answers"].items():
        selected = data_answer.get("selected")
        selected_list = data_answer.get("selected_list")
        value = data_answer.get("value")

        if q_type == "poll":
            points_awarded[user] = 0
            continue

        if q_type == "numeric":
            corr = question.get("correct")

            if isinstance(corr, (int, float)):
                try:
                    user_num = float(str(value).strip())
                except Exception:
                    points_awarded[user] = 0
                    continue

                corr_num = float(corr)

                if user_num == corr_num:
                    room_data["scores"][user] += fixed_points
                    points_awarded[user] = fixed_points
                else:
                    points_awarded[user] = 0
                continue

            user_norm = apply_alias(normalize_answer(value))
            if isinstance(corr, list):
                corr_norms = [apply_alias(normalize_answer(c)) for c in corr]
                is_correct = user_norm in corr_norms
            else:
                corr_norm = apply_alias(normalize_answer(corr))
                is_correct = (user_norm == corr_norm)

            if is_correct:
                room_data["scores"][user] += fixed_points
                points_awarded[user] = fixed_points
            else:
                points_awarded[user] = 0
            continue

        if q_type == "text":
            user_norm = apply_alias(normalize_answer(value))
            correct_raw = question.get("correct")

            if isinstance(correct_raw, list):
                correct_norms = [apply_alias(normalize_answer(c)) for c in correct_raw]
                is_correct = is_fuzzy_match(user_norm, correct_norms)
            else:
                is_correct = is_fuzzy_match(user_norm, [apply_alias(normalize_answer(correct_raw))])

            if is_correct:
                room_data["scores"][user] += fixed_points
                points_awarded[user] = fixed_points
            else:
                points_awarded[user] = 0
            continue

        if q_type == "wordle":
            attempts = data_answer.get("attempts", []) if isinstance(data_answer, dict) else []
            solved = bool(data_answer.get("solved")) if isinstance(data_answer, dict) else False
            if solved:
                attempts_used = max(1, len(attempts))
                wordle_points = max(0, 1100 - (attempts_used * 100))
                room_data["scores"][user] += wordle_points
                points_awarded[user] = wordle_points
            else:
                points_awarded[user] = 0
            continue

        if selected_list is not None:
            selected_set = set(selected_list)
            correct_set = set(correct)

            if selected_set == correct_set:
                room_data["scores"][user] += fixed_points
                points_awarded[user] = fixed_points
            else:
                points_awarded[user] = 0

        elif selected in correct:
            room_data["scores"][user] += fixed_points
            points_awarded[user] = fixed_points
        else:
            points_awarded[user] = 0

    leaderboard = sorted(
        room_data["scores"].items(),
        key=lambda x: x[1],
        reverse=True
    )

    room_data.setdefault("question_history", []).append({
        "question_index": question_index,
        "question": display_question_text,
        "question_type": q_type,
        "points": fixed_points,
        "correct": question.get("correct"),
        "users_answers": users_answers,
        "points_awarded": points_awarded.copy(),
        "response_times_ms": {
            user: metrics.get("response_time_ms")
            for user, metrics in (room_data.get("current_question_metrics") or {}).items()
            if isinstance(metrics, dict)
        },
        "tab_switches": {
            user: int(metrics.get("tab_switches", 0) or 0)
            for user, metrics in (room_data.get("current_question_metrics") or {}).items()
            if isinstance(metrics, dict)
        },
        "offline_before_answer_ms": {
            user: int(float(metrics.get("offline_before_answer_ms", 0) or 0))
            for user, metrics in (room_data.get("current_question_metrics") or {}).items()
            if isinstance(metrics, dict)
        },
        "manual_suspicious": {
            user: bool(metrics.get("manual_suspicious"))
            for user, metrics in (room_data.get("current_question_metrics") or {}).items()
            if isinstance(metrics, dict) and metrics.get("manual_suspicious")
        },
        "manual_suspicious_note": {
            user: str(metrics.get("manual_suspicious_note") or "")
            for user, metrics in (room_data.get("current_question_metrics") or {}).items()
            if isinstance(metrics, dict) and (metrics.get("manual_suspicious") or metrics.get("manual_suspicious_note"))
        },
        "offline_ms": {
            user: int(
                (float(metrics.get("offline_total_ms", 0) or 0) +
                 (max(0.0, time.time() - float(metrics.get("offline_started_at"))) * 1000 if metrics.get("offline_started_at") else 0.0))
            )
            for user, metrics in (room_data.get("current_question_metrics") or {}).items()
            if isinstance(metrics, dict)
        },
        "leaderboard": leaderboard.copy(),
        "team_leaderboard": compute_team_leaderboard(room_data),
        "answered_count": len(users_answers),
        "max_attempts": max(1, int(question.get("max_attempts", 6))) if q_type == "wordle" else None,
    })

    reveal_payload = {
        "type": "reveal",
        "question_type": q_type,
        "correct": correct,
        "correct_value": question.get("correct"),
        "scores": room_data["scores"],
        "leaderboard": leaderboard,
        "question": display_question_text,
        "answers": question.get("answers"),
        "image": question.get("image"),
        "audio": question.get("audio"),
        "image_zoom": question.get("image_zoom"),
        "image_position": question.get("image_position"),
        "image_offset": question.get("image_offset"),
        "stats": stats,
        "users_answers": users_answers,
        "points": question.get("points", 1000),
        "points_awarded": points_awarded,
        "team_leaderboard": compute_team_leaderboard(room_data),
        "max_attempts": max(1, int(question.get("max_attempts", 6))) if q_type == "wordle" else None,
        "word_length": len(apply_alias(normalize_answer(question.get("correct")))) if q_type == "wordle" else None,
    }
    await broadcast(room, reveal_payload)
    room_data["current_payload"] = reveal_payload
    room_data["current_view"] = "reveal"
    log_event("reveal_sent", room, question_index=question_index, question_type=q_type, answers=len(users_answers))

    await asyncio.sleep(2)

    leaderboard_payload = {
        "type": "leaderboard",
        "leaderboard": leaderboard,
        "team_leaderboard": compute_team_leaderboard(room_data),
        "total_scores": room_data["scores"]
    }
    await broadcast(room, leaderboard_payload)
    room_data["current_payload"] = leaderboard_payload
    room_data["current_view"] = "leaderboard"
    log_event("leaderboard_sent", room, question_index=question_index, leaderboard_size=len(leaderboard))

async def reveal_after_delay(room, delay, question_index):

    remaining = delay
    last_tick = time.time()

    while remaining > 0.001:
        await asyncio.sleep(0.05)
        if room not in rooms:
            return
        room_data = rooms[room]
        # Если вопрос сменился — выходим
        if question_index != room_data["question_index"]:
            return
        # Если пауза — просто ждём, не обновляем last_tick
        if room_data.get("paused"):
            await asyncio.sleep(0.05)
            continue
        now = time.time()
        delta = now - last_tick
        last_tick = now
        remaining -= delta
        if remaining < 0:
            remaining = 0

    await send_reveal(room, question_index)

async def update_players(room):
    if room not in rooms:
        return
    room_data = rooms[room]
    if room_data["host"]:
        await safe_send_json(room_data, room_data["host"], {
            "type": "players_update",
            "players": [
                {
                    "username": p,
                    "connected": p not in room_data.get("disconnected", {}),
                    "late_joiner": bool(room_data.get("late_joiners", {}).get(p)),
                }
                for p in room_data["players"].keys()
                if p != "HOST"
            ],
            "team_mode": bool(room_data.get("team_mode")),
            "team_size": int(room_data.get("team_size", 2)),
            "teams": room_data.get("teams", []),
        }, "host")


async def notify_host_player_connection(room: str, username: str, status: str):
    room_data = rooms.get(room)
    if not room_data or not room_data.get("host"):
        return
    await safe_send_json(room_data, room_data["host"], {
        "type": "player_connection",
        "username": username,
        "status": status,
    }, "host")

from fastapi.responses import StreamingResponse
import io
import csv

@app.get("/export/{room}")
async def export_results(room: str, request: Request):
    if room not in rooms:
        return {"error": "Room not found"}

    room_data = rooms[room]
    sorted_scores = sorted(
        room_data["scores"].items(),
        key=lambda x: x[1],
        reverse=True
    )
    question_history = room_data.get("question_history", [])
    appeals = room_data.get("appeals", [])

    player_correct = {player: 0 for player, _ in sorted_scores}
    per_question_rows = []
    detailed_rows = []
    anti_cheat_rows = []

    for item in question_history:
        points_awarded = item.get("points_awarded", {}) or {}
        users_answers = item.get("users_answers", {}) or {}
        response_times_ms = item.get("response_times_ms", {}) or {}
        offline_before_answer_ms = item.get("offline_before_answer_ms", {}) or {}
        manual_suspicious = item.get("manual_suspicious", {}) or {}
        manual_suspicious_note = item.get("manual_suspicious_note", {}) or {}
        answered_count = max(0, int(item.get("answered_count", 0)))
        correct_count = sum(1 for delta in points_awarded.values() if isinstance(delta, (int, float)) and delta > 0)
        accuracy = round((correct_count / answered_count) * 100, 1) if answered_count else 0.0
        per_question_rows.append({
            "question_index": int(item.get("question_index", 0)) + 1,
            "question": item.get("question", ""),
            "answered": answered_count,
            "correct": correct_count,
            "accuracy": accuracy,
        })

        for player, delta in points_awarded.items():
            if player in player_correct and isinstance(delta, (int, float)) and delta > 0:
                player_correct[player] += 1

        players_for_question = sorted(set(users_answers.keys()) | set(points_awarded.keys()) | set(response_times_ms.keys()) | set(offline_before_answer_ms.keys()) | set(manual_suspicious.keys()) | set(manual_suspicious_note.keys()))
        for player in players_for_question:
            row = {
                "question_index": int(item.get("question_index", 0)) + 1,
                "question": item.get("question", ""),
                "question_type": item.get("question_type", ""),
                "player": player,
                "answer": stringify_export_value(users_answers.get(player)),
                "points_awarded": int(points_awarded.get(player, 0) or 0),
                "response_time_ms": response_times_ms.get(player),
                "offline_before_answer_seconds": round(float(offline_before_answer_ms.get(player, 0) or 0) / 1000, 2),
                "tab_switches": int((item.get("tab_switches", {}) or {}).get(player, 0) or 0),
                "manual_suspicious": bool(manual_suspicious.get(player)),
                "manual_suspicious_note": str(manual_suspicious_note.get(player, "") or ""),
            }
            detailed_rows.append(row)
            if row["offline_before_answer_seconds"] > 0 or row["tab_switches"] > 0 or row["manual_suspicious"]:
                anti_cheat_rows.append(row)

    export_format = (request.query_params.get("format") or "html").strip().lower()

    if export_format == "anti_cheat_csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Question #",
            "Question",
            "Question Type",
            "Player",
            "Answer",
            "Response Time (ms)",
            "Offline Before Answer (sec)",
            "Tab Switches",
            "Manual Suspicious",
            "Suspicious Note",
            "Points Awarded",
        ])
        for row in anti_cheat_rows:
            writer.writerow([
                row["question_index"],
                row["question"],
                row["question_type"],
                row["player"],
                row["answer"],
                row["response_time_ms"] if row["response_time_ms"] is not None else "",
                row["offline_before_answer_seconds"],
                row["tab_switches"],
                "yes" if row["manual_suspicious"] else "no",
                row["manual_suspicious_note"],
                row["points_awarded"],
            ])
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=anti_cheat_report_{room}.csv"
            }
        )

    if export_format == "anti_cheat_html":
        anti_cheat_rows_sorted = sorted(
            anti_cheat_rows,
            key=lambda row: (
                str(row.get("player", "")).lower(),
                int(row.get("question_index", 0) or 0),
            )
        )
        anti_cheat_player_names = sorted({
            str(row.get("player", "")).strip()
            for row in anti_cheat_rows_sorted
            if str(row.get("player", "")).strip()
        }, key=lambda value: value.lower())
        anti_cheat_player_names.extend(
            name for name in sorted({
                str(entry.get("username", "")).strip()
                for entry in appeals
                if str(entry.get("username", "")).strip() and str(entry.get("username", "")).strip() not in anti_cheat_player_names
            }, key=lambda value: value.lower())
        )
        anti_cheat_table = "".join(
            (
                f"<tr style=\"background:{'rgba(239,68,68,.14)' if row['manual_suspicious'] else 'transparent'};\">"
                f"<td data-player=\"{html.escape(str(row['player']))}\">{row['question_index']}</td>"
                f"<td>{html.escape(str(row['player']))}</td>"
                f"<td>{html.escape(str(row['question']))}</td>"
                f"<td>{html.escape(str(row['question_type']))}</td>"
                f"<td>{html.escape(str(row['answer']))}</td>"
                f"<td>{row['response_time_ms'] if row['response_time_ms'] is not None else '—'}</td>"
                f"<td>{row['offline_before_answer_seconds']}</td>"
                f"<td>{row['tab_switches']}</td>"
                f"<td>{'offline before answer' if row['offline_before_answer_seconds'] > 0 else ''}{' + ' if row['offline_before_answer_seconds'] > 0 and row['tab_switches'] > 0 else ''}{'tab switch' if row['tab_switches'] > 0 else ''}{' + ' if (row['offline_before_answer_seconds'] > 0 or row['tab_switches'] > 0) and row['manual_suspicious'] else ''}{'manual suspicious' if row['manual_suspicious'] else ''}{' — ' + html.escape(str(row['manual_suspicious_note'])) if row['manual_suspicious_note'] else ''}</td>"
                f"<td>{row['points_awarded']}</td>"
                f"</tr>"
            )
            for row in anti_cheat_rows_sorted
        ) or "<tr><td colspan='10'>Подозрительных событий не найдено</td></tr>"
        anti_cheat_appeal_table = "".join(
            (
                f"<tr data-player=\"{html.escape(str(entry.get('username', '')))}\">"
                f"<td>{int(entry.get('question_index', 0) or 0) + 1 if entry.get('question_index') is not None else '—'}</td>"
                f"<td>{html.escape(str(entry.get('username', '')))}</td>"
                f"<td>{html.escape(str(entry.get('question', '')))}</td>"
                f"<td>{html.escape(str(entry.get('answer', '')) or '—')}</td>"
                f"<td>{html.escape(str(entry.get('reason', '')) or '—')}</td>"
                f"<td>{'Одобрена' if entry.get('resolution') == 'approved' else 'Отклонена' if entry.get('resolution') == 'rejected' else 'Новая'}</td>"
                f"</tr>"
            )
            for entry in sorted(
                [entry for entry in appeals if entry.get("username")],
                key=lambda entry: (
                    str(entry.get("username", "")).lower(),
                    int(entry.get("question_index", 0) or 0),
                )
            )
        ) or "<tr><td colspan='6'>Жалоб по игрокам пока нет</td></tr>"
        report_html = f"""
        <!doctype html>
        <html lang="ru">
        <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Anti-Cheat Report {room}</title>
        <style>
        body{{margin:0;font-family:DM Sans,Arial,sans-serif;background:radial-gradient(circle at top left,#3f0d12,#0f172a 52%,#111827);color:#fff;padding:28px;}}
        .wrap{{max-width:1220px;margin:0 auto;}}
        .hero{{display:flex;justify-content:space-between;gap:16px;align-items:end;flex-wrap:wrap;margin-bottom:22px;}}
        .hero h1{{margin:0;font-size:38px;}}
        .sub{{color:#fecaca;font-weight:700}}
        .card{{background:rgba(255,255,255,.06);border:1px solid rgba(248,113,113,.18);border-radius:24px;padding:20px;box-shadow:0 24px 70px rgba(0,0,0,.38);}}
        table{{width:100%;border-collapse:collapse;font-size:14px;}}
        th,td{{padding:12px 10px;text-align:left;border-bottom:1px solid rgba(255,255,255,.08);vertical-align:top;}}
        th{{color:#fecaca;font-size:12px;text-transform:uppercase;letter-spacing:.08em;}}
        .actions{{margin-bottom:18px;display:flex;gap:12px;flex-wrap:wrap;}}
        .toolbar{{margin-bottom:16px;display:flex;gap:10px;flex-wrap:wrap;}}
        .toolbar input{{min-width:260px;padding:12px 14px;border-radius:14px;border:1px solid rgba(255,255,255,.16);background:rgba(255,255,255,.08);color:#fff;font:inherit;}}
        .btn{{display:inline-block;padding:12px 16px;border-radius:14px;background:#ef4444;color:#fff;text-decoration:none;font-weight:800;border:0;cursor:pointer;}}
        .btn-secondary{{background:rgba(148,163,184,.22);border:1px solid rgba(148,163,184,.35);}}
        .btn-ghost{{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.12);}}
        </style>
        </head>
        <body>
        <div class="wrap">
            <div class="hero">
                <div>
                    <div class="sub">Anti-Cheat Report</div>
                    <h1>Комната {room}</h1>
                    <div class="sub">Подозрительных записей: {len(anti_cheat_rows)}</div>
                </div>
                <div class="actions">
                    <a class="btn" href="/export/{room}?format=anti_cheat_csv">Скачать Anti-Cheat CSV</a>
                    <button class="btn btn-secondary" type="button" onclick="window.print()">Печать / PDF</button>
                </div>
            </div>
            <div class="card">
                <div class="toolbar">
                    <button class="btn btn-ghost" type="button" onclick="sortTable('antiCheatTable', 1, false)">Сортировать по нику</button>
                    <button class="btn btn-ghost" type="button" onclick="sortTable('antiCheatTable', 0, true)">Сортировать по вопросу</button>
                    <input id="playerFilterInput" list="playerFilterOptions" type="text" placeholder="Фильтр по нику игрока">
                    <datalist id="playerFilterOptions">
                        {"".join(f'<option value="{html.escape(name)}"></option>' for name in anti_cheat_player_names)}
                    </datalist>
                </div>
                <table>
                    <thead><tr><th>#</th><th>Игрок</th><th>Вопрос</th><th>Тип</th><th>Ответ</th><th>Ответ, мс</th><th>Offline до ответа, сек</th><th>Tab switches</th><th>Сигнал</th><th>Очки</th></tr></thead>
                    <tbody id="antiCheatTable">{anti_cheat_table}</tbody>
                </table>
            </div>
            <div class="card" style="margin-top:20px;">
                <div class="toolbar">
                    <div class="sub">Все жалобы на выбранного игрока</div>
                </div>
                <table>
                    <thead><tr><th>#</th><th>Игрок</th><th>Вопрос</th><th>Ответ</th><th>Причина жалобы</th><th>Статус</th></tr></thead>
                    <tbody id="appealPlayerTable">{anti_cheat_appeal_table}</tbody>
                </table>
            </div>
        </div>
        <script>
        function sortTable(tableId, columnIndex, numeric){{
            const tbody = document.getElementById(tableId);
            if(!tbody) return;
            const rows = Array.from(tbody.querySelectorAll("tr"));
            rows.sort((a, b)=>{{
                const aText = (a.children[columnIndex]?.textContent || "").trim();
                const bText = (b.children[columnIndex]?.textContent || "").trim();
                if(numeric){{
                    return Number(aText || 0) - Number(bText || 0);
                }}
                return aText.localeCompare(bText, "ru", {{ sensitivity: "base" }});
            }});
            rows.forEach((row)=>tbody.appendChild(row));
        }}
        function filterByPlayer(){{
            const value = (document.getElementById("playerFilterInput")?.value || "").trim().toLowerCase();
            const antiRows = Array.from(document.querySelectorAll("#antiCheatTable tr"));
            antiRows.forEach((row)=>{{
                const player = (row.children[1]?.textContent || "").trim().toLowerCase();
                row.style.display = !value || player.includes(value) ? "" : "none";
            }});
            const appealRows = Array.from(document.querySelectorAll("#appealPlayerTable tr"));
            appealRows.forEach((row)=>{{
                const player = (row.children[1]?.textContent || "").trim().toLowerCase();
                row.style.display = !value || player.includes(value) ? "" : "none";
            }});
        }}
        document.getElementById("playerFilterInput")?.addEventListener("input", filterByPlayer);
        </script>
        </body>
        </html>
        """
        return HTMLResponse(report_html)

    if sorted_scores and export_format != "csv":
        total_questions = len(question_history)
        cards = []
        for place, (player, score) in enumerate(sorted_scores, start=1):
            appeals_for_player = [
                entry for entry in appeals
                if entry.get("username") == player and "delta" in entry
            ]
            appeal_total = sum(int(entry.get("delta", 0)) for entry in appeals_for_player)
            accuracy = round((player_correct.get(player, 0) / total_questions) * 100, 1) if total_questions else 0.0
            cards.append(f"""
                <tr>
                    <td>{place}</td>
                    <td>{html.escape(str(player))}</td>
                    <td>{score}</td>
                    <td>{player_correct.get(player, 0)}</td>
                    <td>{len(appeals_for_player)} ({appeal_total:+d})</td>
                    <td>{accuracy}%</td>
                </tr>
            """)

        question_table = "".join(
            f"<tr><td>{entry['question_index']}</td><td>{html.escape(str(entry['question']))}</td><td>{entry['answered']}</td><td>{entry['correct']}</td><td>{entry['accuracy']}%</td></tr>"
            for entry in per_question_rows
        ) or "<tr><td colspan='5'>История вопросов пока пуста</td></tr>"

        detailed_table = "".join(
            f"<tr><td>{row['question_index']}</td><td>{html.escape(str(row['player']))}</td><td>{html.escape(str(row['question']))}</td><td>{html.escape(str(row['question_type']))}</td><td>{html.escape(str(row['answer']))}</td><td>{row['points_awarded']}</td><td>{row['response_time_ms'] if row['response_time_ms'] is not None else '—'}</td><td>{row['offline_before_answer_seconds']}</td></tr>"
            for row in sorted(detailed_rows, key=lambda row: (str(row.get("player", "")).lower(), int(row.get("question_index", 0) or 0)))
        ) or "<tr><td colspan='8'>Подробной статистики пока нет</td></tr>"

        appeal_table = "".join(
            f"<tr><td>{idx}</td><td>{html.escape(str(entry.get('username','')))}</td><td>{html.escape(str(entry.get('reason','')))}</td><td>{int(entry.get('delta',0)):+d}</td></tr>"
            for idx, entry in enumerate([a for a in appeals if 'delta' in a], start=1)
        ) or "<tr><td colspan='4'>Апелляций с изменением очков не было</td></tr>"

        report_html = f"""
        <!doctype html>
        <html lang="ru">
        <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Отчёт комнаты {room}</title>
        <style>
        body{{margin:0;font-family:DM Sans,Arial,sans-serif;background:linear-gradient(160deg,#0f172a,#1e293b);color:#fff;padding:32px;}}
        .wrap{{max-width:1180px;margin:0 auto;}}
        .hero{{display:flex;justify-content:space-between;gap:16px;align-items:end;flex-wrap:wrap;margin-bottom:24px;}}
        .hero h1{{margin:0;font-size:40px;}}
        .sub{{color:#cbd5e1;font-weight:700;}}
        .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px;margin-bottom:24px;}}
        .card{{background:rgba(255,255,255,0.07);border:1px solid rgba(255,255,255,0.08);border-radius:24px;padding:20px;box-shadow:0 20px 60px rgba(0,0,0,.35);}}
        table{{width:100%;border-collapse:collapse;font-size:15px;}}
        th,td{{padding:12px 10px;text-align:left;border-bottom:1px solid rgba(255,255,255,0.08);vertical-align:top;}}
        th{{color:#cbd5e1;font-size:13px;text-transform:uppercase;letter-spacing:.08em;}}
        .actions{{margin-bottom:18px;display:flex;gap:12px;flex-wrap:wrap;}}
        .btn{{display:inline-block;padding:12px 16px;border-radius:14px;background:#7c3aed;color:#fff;text-decoration:none;font-weight:800;border:0;cursor:pointer;}}
        .btn-secondary{{background:rgba(148,163,184,.22);border:1px solid rgba(148,163,184,.35);}}
        .btn-ghost{{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.12);}}
        </style>
        </head>
        <body>
        <div class="wrap">
            <div class="hero">
                <div>
                    <div class="sub">Итоговый отчёт</div>
                    <h1>Комната {room}</h1>
                    <div class="sub">Игроков: {len(sorted_scores)} • Вопросов: {len(question_history)} • Апелляций: {len([a for a in appeals if 'delta' in a])}</div>
                </div>
                <div class="actions">
                    <a class="btn" href="/export/{room}?format=csv">Скачать CSV</a>
                    <a class="btn" href="/export/{room}?format=anti_cheat_html">Anti-Cheat Документ</a>
                    <button class="btn btn-secondary" type="button" onclick="exitReport()">Выйти из отчёта</button>
                </div>
            </div>
            <div class="grid">
                <div class="card">
                    <h2>Итоговый рейтинг</h2>
                    <table>
                        <thead><tr><th>Место</th><th>Игрок</th><th>Очки</th><th>Правильные</th><th>Апелляции</th><th>Точность</th></tr></thead>
                        <tbody>{''.join(cards)}</tbody>
                    </table>
                </div>
                <div class="card">
                    <h2>Попадание по вопросам</h2>
                    <table>
                        <thead><tr><th>#</th><th>Вопрос</th><th>Ответили</th><th>Верно</th><th>%</th></tr></thead>
                        <tbody>{question_table}</tbody>
                    </table>
                </div>
            </div>
            <div class="card">
                <h2>Апелляции с изменением очков</h2>
                <table>
                    <thead><tr><th>#</th><th>Игрок</th><th>Причина</th><th>Изменение</th></tr></thead>
                    <tbody>{appeal_table}</tbody>
                </table>
            </div>
            <div class="card" style="margin-top:24px;">
                <h2>Подробно по ответам</h2>
                <div class="actions">
                    <button class="btn btn-ghost" type="button" onclick="sortTable('detailedTable', 1, false)">Сортировать по нику</button>
                    <button class="btn btn-ghost" type="button" onclick="sortTable('detailedTable', 0, true)">Сортировать по вопросу</button>
                </div>
                <table>
                    <thead><tr><th>#</th><th>Игрок</th><th>Вопрос</th><th>Тип</th><th>Ответ</th><th>Очки</th><th>Ответ, мс</th><th>Offline перед ответом, сек</th></tr></thead>
                    <tbody id="detailedTable">{detailed_table}</tbody>
                </table>
            </div>
        </div>
        <script>
        function exitReport(){{
            if(window.history.length > 1){{
                window.history.back();
                return;
            }}
            try{{
                window.close();
            }}catch(e){{}}
            window.location.href = "/";
        }}
        function sortTable(tableId, columnIndex, numeric){{
            const tbody = document.getElementById(tableId);
            if(!tbody) return;
            const rows = Array.from(tbody.querySelectorAll("tr"));
            rows.sort((a, b)=>{{
                const aText = (a.children[columnIndex]?.textContent || "").trim();
                const bText = (b.children[columnIndex]?.textContent || "").trim();
                if(numeric){{
                    return Number(aText || 0) - Number(bText || 0);
                }}
                return aText.localeCompare(bText, "ru", {{ sensitivity: "base" }});
            }});
            rows.forEach((row)=>tbody.appendChild(row));
        }}
        </script>
        </body>
        </html>
        """
        return HTMLResponse(report_html)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Place",
        "Player",
        "Score",
        "Question #",
        "Question",
        "Question Type",
        "Answer",
        "Points Awarded",
        "Response Time (ms)",
        "Offline Before Answer (sec)",
    ])

    place_map = {player: index for index, (player, _) in enumerate(sorted_scores, start=1)}
    score_map = {player: score for player, score in sorted_scores}
    if detailed_rows:
        for row in detailed_rows:
            writer.writerow([
                place_map.get(row["player"], ""),
                row["player"],
                score_map.get(row["player"], 0),
                row["question_index"],
                row["question"],
                row["question_type"],
                row["answer"],
                row["points_awarded"],
                row["response_time_ms"] if row["response_time_ms"] is not None else "",
                row["offline_before_answer_seconds"],
            ])
    else:
        for i, (player, score) in enumerate(sorted_scores, start=1):
            writer.writerow([i, player, score, "", "", "", "", "", "", ""])

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=results.csv"
        }
    )

app.mount("/static", StaticFiles(directory="static"), name="static")
