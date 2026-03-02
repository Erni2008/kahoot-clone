from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
import random
import string
import time
import asyncio
from typing import Dict
from difflib import SequenceMatcher

import re
import os
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


app = FastAPI()

from fastapi import Request
from fastapi.responses import Response

@app.middleware("http")
async def add_ngrok_header(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["ngrok-skip-browser-warning"] = "true"
    return response

rooms: Dict[str, dict] = {}


def build_room_quiz(quiz_name: str):
    quiz = list(QUIZZES.get(quiz_name, []))
    if len(quiz) <= 1:
        return quiz

    first_question = quiz[0]
    remaining_questions = quiz[1:]
    random.shuffle(remaining_questions)
    return [first_question, *remaining_questions]

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
}


]
        

    
}

# Supported question types:
# mcq        - multiple choice (default)
# numeric    - numeric input
# text       - short text answer
# poll       - no correct answer (voting)
# fastest    - first correct wins bonus

def generate_pin():
    while True:
        pin = "".join(random.choices(string.digits, k=6))
        if pin not in rooms:
            return pin


async def close_room_and_notify(room: str, message: str = "Тест завершен"):
    if room not in rooms:
        return False

    room_data = rooms[room]

    for ws in list(room_data.get("players", {}).values()):
        try:
            await ws.send_json({
                "type": "test_aborted",
                "message": message
            })
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass

    if room_data.get("host"):
        try:
            await room_data["host"].send_json({
                "type": "return_to_lobby",
                "message": "Тест завершён. Игроки удалены."
            })
        except Exception:
            pass

    del rooms[room]
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

@app.get("/create-room/{quiz}")
async def create_room(quiz: str, request: Request):
    # ===== РАСФОРМИРОВАНИЕ СУЩЕСТВУЮЩИХ КОМНАТ =====
    for existing_pin in list(rooms.keys()):
        room_data = rooms[existing_pin]

        # уведомляем всех игроков
        for ws in room_data.get("players", {}).values():
            try:
                await ws.send_json({
                    "type": "room_closed",
                    "message": "Комната расформирована ведущим"
                })
            except:
                pass

        # уведомляем хоста (если есть)
        if room_data.get("host"):
            try:
                await room_data["host"].send_json({
                    "type": "room_closed",
                    "message": "Комната расформирована"
                })
            except:
                pass

        # даём фронту время обработать сообщение
        await asyncio.sleep(0.15)

        # закрываем все соединения игроков
        for ws in list(room_data.get("players", {}).values()):
            try:
                await ws.close()
            except:
                pass

        # закрываем соединение хоста
        if room_data.get("host"):
            try:
                await room_data["host"].close()
            except:
                pass

        # удаляем комнату
        del rooms[existing_pin]

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
        "current_payload": None,
        "current_view": "waiting",
    }

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
    

    if role == "host":
        room_data["host"] = websocket
        await update_players(room)
    else:
        # === Player connect / reconnect handling ===
        # Allow reconnect with the same username (common mobile background/screen-off case).
        # New players are only allowed while waiting.

        existing_ws = room_data.get("players", {}).get(username)
        existing_score = room_data.get("scores", {}).get(username)
        is_known_player = existing_score is not None
        was_disconnected = username in room_data.get("disconnected", {})

        # If the game already started and this username is not known -> block join
        if room_data.get("status") != "waiting" and not is_known_player:
            await websocket.send_json({
                "type": "error",
                "message": "Game already started"
            })
            await websocket.close()
            return

        # If there is an existing socket for this username, replace it (reconnect)
        if existing_ws is not None:
            try:
                await existing_ws.close()
            except Exception:
                pass

        room_data.setdefault("players", {})[username] = websocket

        # Preserve score on reconnect, otherwise initialize
        if existing_score is None:
            room_data.setdefault("scores", {})[username] = 0

        # Clear disconnected marker on successful reconnect
        room_data.setdefault("disconnected", {}).pop(username, None)

        if room_data.get("status") == "waiting":
            await websocket.send_json({"type": "waiting"})
        elif is_known_player:
            current_payload = room_data.get("current_payload")
            if isinstance(current_payload, dict):
                payload_to_send = dict(current_payload)
                if room_data.get("current_view") == "question":
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

        if was_disconnected:
            await notify_host_player_connection(room, username, "reconnected")
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
                    room_data.setdefault("disconnected", {}).pop(username, None)
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

                room_data.setdefault("appeals", []).append(appeal_item)

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
                    "total_scores": room_data["scores"],
                })
                continue

            # ANSWER (player OR host)
            if msg_type == "answer" and (role == "player" or role == "host"):

                answer_user = username if role == "player" else "HOST"

                # ensure HOST exists in scores
                if role == "host" and answer_user not in room_data["scores"]:
                    room_data["scores"][answer_user] = 0

                if answer_user not in room_data["answers"]:

                    current_time = time.time()
                    elapsed = current_time - room_data["question_start"]
                    remaining = max(0, room_data["question_duration"] - elapsed)

                    room_data["answers"][answer_user] = {
                        "value": data.get("value"),
                        "selected": data.get("selected"),
                        "selected_list": data.get("selected_list"),
                        "remaining": remaining
                    }

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
                room_data["status"] = "active"
                room_data["paused"] = False
                room_data["remaining_time"] = None
                room_data["current_payload"] = None
                room_data["current_view"] = "question"
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
                else:
                    room_data["paused"] = False

                    room_data["question_start"] = time.time()
                    room_data["question_duration"] = room_data["remaining_time"]

                    await broadcast(room, {
                        "type": "resume",
                        "time": room_data["remaining_time"]
                    })

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

                    await update_players(room)

            # NEXT
            if role == "host" and data["type"] == "next":
                if room_data["status"] != "active":
                    continue

                room_data["paused"] = False
                room_data["remaining_time"] = None

                room_data["question_index"] += 1
                room_data["current_payload"] = None
                room_data["current_view"] = "question"
                await send_question(room)

            if role == "host" and data["type"] == "force_reveal":
                if room_data["status"] != "active":
                    continue
                await send_reveal(room, room_data["question_index"])

            # END GAME (manual by host)
            if role == "host" and data["type"] == "end_game":
                if room not in rooms:
                    continue

                room_data = rooms[room]

                # Notify and kick ALL players
                for username_player, ws_player in list(room_data["players"].items()):
                    try:
                        await ws_player.send_json({
                            "type": "test_aborted",
                            "message": "Тест завершен"
                        })
                    except:
                        pass
                    try:
                        await ws_player.close()
                    except:
                        pass

                # Clear only players and game state
                room_data["players"] = {}
                room_data["scores"] = {}
                room_data["answers"] = {}
                room_data["question_index"] = 0
                room_data["status"] = "waiting"
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

                continue

    except WebSocketDisconnect:
        # Очистка при отключении
        if room in rooms:
            room_data = rooms[room]

            if role == "player" and room_data.get("players", {}).get(username) is websocket:
                # Mark disconnected, keep score/state for a short grace window.
                room_data.setdefault("disconnected", {})[username] = time.time()
                await notify_host_player_connection(room, username, "disconnected")
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
                    await update_players(pin)

                asyncio.create_task(_cleanup_if_not_back(room, username))

            if role == "host":
                # Если ведущий отключился — завершить тест для всех игроков
                await broadcast(room, {
                    "type": "test_aborted",
                    "message": "Тест завершен"
                })

                for ws in room_data.get("players", {}).values():
                    try:
                        await ws.close()
                    except:
                        pass

                del rooms[room]

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
            "streaks": room_data.get("streaks", {})
        }
        await broadcast(room, game_over_payload)
        room_data["current_payload"] = game_over_payload
        room_data["current_view"] = "game_over"

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
    room_data["revealed"] = False
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
        "max_select": max_select
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
        "max_select": max_select
    }
    room_data["current_view"] = "question"

    current_index = room_data["question_index"]
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
        if q_type == "numeric" or q_type == "text":
            users_answers[user] = data_answer.get("value")
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
        "leaderboard": leaderboard.copy(),
        "answered_count": len(users_answers),
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
        "points_awarded": points_awarded
    }
    await broadcast(room, reveal_payload)
    room_data["current_payload"] = reveal_payload
    room_data["current_view"] = "reveal"

    await asyncio.sleep(2)

    leaderboard_payload = {
        "type": "leaderboard",
        "leaderboard": leaderboard,
        "total_scores": room_data["scores"]
    }
    await broadcast(room, leaderboard_payload)
    room_data["current_payload"] = leaderboard_payload
    room_data["current_view"] = "leaderboard"

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
    room_data = rooms[room]
    if room_data["host"]:
        await room_data["host"].send_json({
            "type": "players_update",
            "players": [
                p for p in room_data["players"].keys()
                if p != "HOST" and p not in room_data.get("disconnected", {})
            ]
        })

async def broadcast(room, message):
    room_data = rooms[room]

    if room_data["host"]:
        await room_data["host"].send_json(message)

    for ws in list(room_data["players"].values()):
        try:
            await ws.send_json(message)
        except:
            pass


async def notify_host_player_connection(room: str, username: str, status: str):
    room_data = rooms.get(room)
    if not room_data or not room_data.get("host"):
        return
    try:
        await room_data["host"].send_json({
            "type": "player_connection",
            "username": username,
            "status": status,
        })
    except Exception:
        pass

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

    for item in question_history:
        points_awarded = item.get("points_awarded", {}) or {}
        answered_count = max(0, int(item.get("answered_count", 0)))
        correct_count = sum(1 for delta in points_awarded.values() if isinstance(delta, (int, float)) and delta > 0)
        accuracy = round((correct_count / answered_count) * 100, 1) if answered_count else 0.0
        per_question_rows.append({
            "question": item.get("question", ""),
            "answered": answered_count,
            "correct": correct_count,
            "accuracy": accuracy,
        })

        for player, delta in points_awarded.items():
            if player in player_correct and isinstance(delta, (int, float)) and delta > 0:
                player_correct[player] += 1

    export_format = (request.query_params.get("format") or "html").strip().lower()

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
            f"<tr><td>{idx}</td><td>{html.escape(str(entry['question']))}</td><td>{entry['answered']}</td><td>{entry['correct']}</td><td>{entry['accuracy']}%</td></tr>"
            for idx, entry in enumerate(per_question_rows, start=1)
        ) or "<tr><td colspan='5'>История вопросов пока пуста</td></tr>"

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
        .btn{{display:inline-block;padding:12px 16px;border-radius:14px;background:#7c3aed;color:#fff;text-decoration:none;font-weight:800;}}
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
        </div>
        </body>
        </html>
        """
        return HTMLResponse(report_html)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Place", "Player", "Score"])

    for i, (player, score) in enumerate(sorted_scores, start=1):
        writer.writerow([i, player, score])

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=results.csv"
        }
    )

app.mount("/static", StaticFiles(directory="static"), name="static")
