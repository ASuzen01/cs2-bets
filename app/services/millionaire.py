"""Экономика и вопросы викторины «Кто хочет стать миллионером?».

Ставка списывается при старте. Выплаты — валовые (включая возврат ставки),
а вопросы и проверка ответов остаются на сервере.
"""

from __future__ import annotations

import gzip
import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Final

STAKES: Final[tuple[int, ...]] = (10, 50, 100, 250, 500, 1000)
BASE_STAKE: Final = 100
# Сумма возврата при верном ответе на соответствующий вопрос, для ставки 100.
BASE_PAYOUTS: Final[tuple[int, ...]] = (
    100, 100, 100, 110, 120, 135, 150, 170, 190, 215, 245, 280, 320, 370, 430,
)
CHECKPOINT_LEVELS: Final[tuple[int, ...]] = (5, 10)
TOTAL_QUESTIONS: Final = 15


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    band: str
    prompt: str
    options: tuple[str, str, str, str]
    answer: int
    stage: int | None = None


_LEGACY_QUESTIONS: Final[tuple[Question, ...]] = (
    Question("e01", "easy", "Как называется столица Португалии?", ("Лиссабон", "Порту", "Брага", "Коимбра"), 0),
    Question("e02", "easy", "Какой газ обозначают формулой CO₂?", ("Кислород", "Углекислый газ", "Водород", "Азот"), 1),
    Question("e03", "easy", "Сколько сторон у треугольника?", ("Две", "Три", "Четыре", "Пять"), 1),
    Question("e04", "easy", "Какую планету часто называют Красной планетой?", ("Венеру", "Марс", "Юпитер", "Меркурий"), 1),
    Question("e05", "easy", "Сколько дней в обычной неделе?", ("Пять", "Шесть", "Семь", "Восемь"), 2),
    Question("e06", "easy", "Какой орган перекачивает кровь по телу?", ("Лёгкие", "Печень", "Сердце", "Почки"), 2),
    Question("e07", "easy", "Сколько сантиметров в одном метре?", ("10", "100", "1 000", "60"), 1),
    Question("e08", "easy", "Как называется естественный спутник Земли?", ("Фобос", "Луна", "Титан", "Европа"), 1),
    Question("e09", "easy", "Сколько будет 5 × 6?", ("11", "25", "30", "36"), 2),
    Question("e10", "easy", "Чем является Солнце?", ("Планетой", "Звездой", "Кометой", "Спутником"), 1),
    Question("m01", "medium", "На каком языке преимущественно говорят в Бразилии?", ("Испанском", "Португальском", "Французском", "Итальянском"), 1),
    Question("m02", "medium", "Какой газ составляет наибольшую долю земной атмосферы?", ("Кислород", "Углекислый газ", "Азот", "Аргон"), 2),
    Question("m03", "medium", "Какой химический элемент обозначается символом Fe?", ("Фтор", "Железо", "Фосфор", "Франций"), 1),
    Question("m04", "medium", "Кто написал картину «Звёздная ночь»?", ("Клод Моне", "Винсент ван Гог", "Пабло Пикассо", "Сальвадор Дали"), 1),
    Question("m05", "medium", "Какой океан является крупнейшим по площади?", ("Атлантический", "Индийский", "Северный Ледовитый", "Тихий"), 3),
    Question("m06", "medium", "Какое число обозначает римская цифра L?", ("10", "40", "50", "100"), 2),
    Question("m07", "medium", "Какой город является столицей Канады?", ("Торонто", "Ванкувер", "Оттава", "Монреаль"), 2),
    Question("m08", "medium", "Как расшифровывается первая буква D в аббревиатуре DNA?", ("Двойная", "Дезоксирибо-", "Динамическая", "Донорская"), 1),
    Question("m09", "medium", "Какой инструмент обычно имеет клавиши, молоточки и струны?", ("Флейта", "Арфа", "Фортепиано", "Труба"), 2),
    Question("m10", "medium", "Чему равно 4 в кубе?", ("12", "16", "48", "64"), 3),
    Question("h01", "hard", "Каким символом обозначают вольфрам?", ("V", "W", "Wo", "Vl"), 1),
    Question("h02", "hard", "Как называется столица Бутана?", ("Катманду", "Тхимпху", "Дакка", "Коломбо"), 1),
    Question("h03", "hard", "Какое десятичное число записано в двоичной системе как 1010?", ("8", "10", "12", "14"), 1),
    Question("h04", "hard", "Чему равно 0! (факториал нуля)?", ("0", "1", "Не определено", "−1"), 1),
    Question("h05", "hard", "У какой планеты звёздные сутки длиннее её года?", ("Марса", "Венеры", "Земли", "Юпитера"), 1),
    Question("h06", "hard", "Кто стала первой женщиной — лауреатом Нобелевской премии?", ("Ада Лавлейс", "Мария Кюри", "Лизе Мейтнер", "Дороти Ходжкин"), 1),
    Question("h07", "hard", "Кто автор романа «Сто лет одиночества»?", ("Хорхе Луис Борхес", "Габриэль Гарсиа Маркес", "Марио Варгас Льоса", "Пауло Коэльо"), 1),
    Question("h08", "hard", "Как называется греческая буква Ω?", ("Омикрон", "Омега", "Сигма", "Дельта"), 1),
    Question("h09", "hard", "Какой химический элемент обозначается символом Sb?", ("Селен", "Сурьма", "Серебро", "Самарий"), 1),
    Question("h10", "hard", "В каком году Плутон был переведён МАС в категорию карликовых планет?", ("1999", "2003", "2006", "2010"), 2),
)

_RELEASE_BANK: Final = (
    Path(__file__).resolve().parents[2]
    / "data" / "millionaire" / "releases" / "millionaire-questions-v0.1.2-beta.json.gz"
)


def _load_release_questions() -> tuple[Question, ...]:
    with gzip.open(_RELEASE_BANK, "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    questions = tuple(
        Question(
            id=str(row["id"]),
            band=str(row["difficulty"]),
            prompt=str(row["text"]),
            options=tuple(str(option) for option in row["options"]),
            answer=int(row["answer"]),
            stage=int(row["stage"]),
        )
        for row in payload["questions"]
    )
    if not questions:
        raise RuntimeError("Millionaire release question bank is empty")
    if len({question.id for question in questions}) != len(questions):
        raise RuntimeError("Millionaire release contains duplicate question IDs")
    missing_stages = [stage for stage in range(1, TOTAL_QUESTIONS + 1)
                      if not any(question.stage == stage for question in questions)]
    if missing_stages:
        raise RuntimeError(f"Millionaire release has no questions for stages {missing_stages}")
    return questions


QUESTIONS: Final[tuple[Question, ...]] = _load_release_questions()
_BY_ID: Final = {question.id: question for question in (*_LEGACY_QUESTIONS, *QUESTIONS)}
_BY_STAGE: Final = {
    stage: tuple(question.id for question in QUESTIONS if question.stage == stage)
    for stage in range(1, TOTAL_QUESTIONS + 1)
}


def payout(stake: int, level: int) -> int:
    """Полный возврат по уровню; дробные монеты округляются вверх от половины."""
    amount = int(stake)
    question = int(level)
    if amount not in STAKES:
        raise ValueError("Выбери одну из доступных ставок")
    if not 1 <= question <= TOTAL_QUESTIONS:
        raise ValueError("Номер вопроса должен быть от 1 до 15")
    return (BASE_PAYOUTS[question - 1] * amount + BASE_STAKE // 2) // BASE_STAKE


def checkpoint_payout(stake: int, correct_count: int) -> int:
    """Сумма, гарантированная при ошибке после уже пройденных уровней."""
    correct = int(correct_count)
    if correct >= 10:
        return payout(stake, 10)
    if correct >= 5:
        return payout(stake, 5)
    return 0


def pick_question_ids() -> list[str]:
    """Выбирает по одному вопросу для каждой из 15 номерных ступеней."""
    rng = secrets.SystemRandom()
    return [rng.choice(_BY_STAGE[stage]) for stage in range(1, TOTAL_QUESTIONS + 1)]


def question(question_id: str) -> Question:
    try:
        return _BY_ID[str(question_id)]
    except KeyError:
        raise ValueError("Вопрос не найден") from None


def public_question(question_id: str, level: int) -> dict[str, object]:
    item = question(question_id)
    return {"level": int(level), "text": item.prompt, "options": list(item.options)}


__all__ = [
    "BASE_PAYOUTS",
    "CHECKPOINT_LEVELS",
    "STAKES",
    "TOTAL_QUESTIONS",
    "checkpoint_payout",
    "payout",
    "pick_question_ids",
    "public_question",
    "question",
]
