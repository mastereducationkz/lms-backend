"""
Модуль для автоматической проверки заданий различных типов
Поддерживает все типы заданий из ТЗ
"""

from typing import Dict, List, Any, Union
import re
import difflib


def check_assignment_answers(
    assignment_type: str,
    student_answers: Dict[str, Any],
    correct_answers: Dict[str, Any],
    max_score: int
) -> int:
    """
    Автоматическая проверка ответов на задания
    
    Args:
        assignment_type: Тип задания
        student_answers: Ответы студента
        correct_answers: Правильные ответы
        max_score: Максимальный балл
    
    Returns:
        int: Полученный балл (0 - max_score)
    """
    
    checkers = {
        "single_choice": check_single_choice,
        "multiple_choice": check_multiple_choice,
        "picture_choice": check_picture_choice,
        "fill_in_blanks": check_fill_in_blanks,
        "matching": check_matching,
        "matching_text": check_matching_text,
        "free_text": check_free_text,
        "file_upload": check_file_upload,
        "multi_task": check_multi_task
    }
    
    if assignment_type not in checkers:
        raise ValueError(f"Unsupported assignment type: {assignment_type}")
    
    # Вызываем соответствующую функцию проверки
    score_percentage = checkers[assignment_type](student_answers, correct_answers)
    
    # Конвертируем процент в балл
    return int((score_percentage / 100) * max_score)


def check_single_choice(student_answers: Dict, correct_answers: Dict) -> float:
    """
    Проверка single choice (один правильный ответ)
    
    Expected format:
    student_answers: {"selected_option": 2}
    correct_answers: {"correct_answer": 1}
    """
    student_choice = student_answers.get("selected_option")
    correct_choice = correct_answers.get("correct_answer")
    
    if student_choice is None or correct_choice is None:
        return 0.0
    
    return 100.0 if student_choice == correct_choice else 0.0


def check_multiple_choice(student_answers: Dict, correct_answers: Dict) -> float:
    """
    Проверка multiple choice (несколько правильных ответов)
    
    Expected format:
    student_answers: {"selected_options": [0, 2, 3]}
    correct_answers: {"correct_answers": [1, 2]}
    """
    student_choices = set(student_answers.get("selected_options", []))
    correct_choices = set(correct_answers.get("correct_answers", []))
    
    if not correct_choices:
        return 0.0
    
    # Частичное засчитывание очков
    correct_count = len(student_choices.intersection(correct_choices))
    wrong_count = len(student_choices.difference(correct_choices))
    missed_count = len(correct_choices.difference(student_choices))
    
    # Формула: (правильные - неправильные) / общее количество правильных
    score = max(0, correct_count - wrong_count) / len(correct_choices)
    return min(100.0, score * 100)


def check_picture_choice(student_answers: Dict, correct_answers: Dict) -> float:
    """
    Проверка picture choice (выбор из изображений)
    Аналогично single choice, но с изображениями
    """
    return check_single_choice(student_answers, correct_answers)


def check_fill_in_blanks(student_answers: Dict, correct_answers: Dict) -> float:
    """
    Проверка fill in the blanks (заполнение пропусков)
    
    Expected format:
    student_answers: {"answers": ["ответ1", "ответ2", "ответ3"]}
    correct_answers: {"correct_answers": ["правильный1", "правильный2", "правильный3"]}
    """
    student_blanks = student_answers.get("answers", [])
    correct_blanks = correct_answers.get("correct_answers", [])
    
    if not correct_blanks:
        return 0.0
    
    correct_count = 0
    total_blanks = min(len(student_blanks), len(correct_blanks))
    
    for i in range(total_blanks):
        if normalize_text(student_blanks[i]) == normalize_text(correct_blanks[i]):
            correct_count += 1
    
    # Учитываем незаполненные пропуски
    if len(student_blanks) < len(correct_blanks):
        total_blanks = len(correct_blanks)
    
    return (correct_count / len(correct_blanks)) * 100 if correct_blanks else 0.0


def check_matching(student_answers: Dict, correct_answers: Dict) -> float:
    """
    Проверка matching (сопоставление пар)
    
    Expected format:
    student_answers: {"matches": {"0": "1", "1": "0", "2": "2"}}  # left_index: right_index
    correct_answers: {"correct_matches": {"0": "2", "1": "1", "2": "0"}}
    """
    student_matches = student_answers.get("matches", {})
    correct_matches = correct_answers.get("correct_matches", {})
    
    if not correct_matches:
        return 0.0
    
    correct_count = 0
    
    for left_item, correct_right in correct_matches.items():
        student_right = student_matches.get(str(left_item))
        if student_right == correct_right:
            correct_count += 1
    
    return (correct_count / len(correct_matches)) * 100


def check_matching_text(student_answers: Dict, correct_answers: Dict) -> float:
    """
    Проверка matching text (сопоставление текстовых элементов)
    
    Expected format:
    student_answers: {"matches": {"термин1": "определение1", "термин2": "определение2"}}
    correct_answers: {"correct_matches": {"термин1": "определение1", "термин2": "определение2"}}
    """
    student_matches = student_answers.get("matches", {})
    correct_matches = correct_answers.get("correct_matches", {})
    
    if not correct_matches:
        return 0.0
    
    correct_count = 0
    
    for term, correct_definition in correct_matches.items():
        student_definition = student_matches.get(term)
        if student_definition and normalize_text(student_definition) == normalize_text(correct_definition):
            correct_count += 1
    
    return (correct_count / len(correct_matches)) * 100


def check_free_text(student_answers: Dict, correct_answers: Dict) -> float:
    """
    Проверка free text (свободный ответ)
    Использует ключевые слова и частичное совпадение
    
    Expected format:
    student_answers: {"text": "Ответ студента на вопрос"}
    correct_answers: {"keywords": ["ключевое1", "ключевое2"], "sample_answer": "Примерный ответ"}
    """
    student_text = student_answers.get("text", "").strip()
    keywords = correct_answers.get("keywords", [])
    sample_answer = correct_answers.get("sample_answer", "")
    
    if not student_text:
        return 0.0
    
    score = 0.0
    
    # Проверка по ключевым словам (50% от оценки)
    if keywords:
        normalized_text = normalize_text(student_text)
        keyword_score = 0
        
        for keyword in keywords:
            if normalize_text(keyword) in normalized_text:
                keyword_score += 1
        
        score += (keyword_score / len(keywords)) * 50
    
    # Проверка схожести с примерным ответом (50% от оценки)
    if sample_answer:
        similarity = calculate_text_similarity(student_text, sample_answer)
        score += similarity * 50
    
    return min(100.0, score)


def check_file_upload(student_answers: Dict, correct_answers: Dict) -> float:
    """
    Проверка file upload (загрузка файла)
    Автоматически засчитывается, если файл загружен
    
    Expected format:
    student_answers: {"file_uploaded": True, "file_url": "path/to/file"}
    correct_answers: {"requires_file": True}
    """
    file_uploaded = student_answers.get("file_uploaded", False)
    file_url = student_answers.get("file_url")
    
    # Простая проверка - засчитываем, если файл загружен
    if file_uploaded and file_url:
        return 100.0
    
    return 0.0


def check_multi_task(student_answers: Dict, correct_answers: Dict) -> float:
    """
    Проверка multi_task (многокомпонентное задание)
    Пока возвращаем 0, так как требуется ручная проверка или сложная логика
    """
    return 0.0


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def normalize_text(text: str) -> str:
    """Нормализация текста для сравнения"""
    if not isinstance(text, str):
        return str(text).lower().strip()
    
    # Приводим к нижнему регистру и убираем лишние пробелы
    normalized = re.sub(r'\s+', ' ', text.lower().strip())
    
    # Убираем знаки препинания
    normalized = re.sub(r'[^\w\s]', '', normalized)
    
    return normalized


def calculate_text_similarity(text1: str, text2: str) -> float:
    """Вычисление схожести двух текстов (0.0 - 1.0)"""
    if not text1 or not text2:
        return 0.0
    
    # Нормализуем тексты
    norm_text1 = normalize_text(text1)
    norm_text2 = normalize_text(text2)
    
    # Используем SequenceMatcher для вычисления схожести
    similarity = difflib.SequenceMatcher(None, norm_text1, norm_text2).ratio()
    
    return similarity


def validate_answer_format(assignment_type: str, answers: Dict[str, Any]) -> bool:
    """Валидация формата ответов студента"""
    
    required_fields = {
        "single_choice": ["selected_option"],
        "multiple_choice": ["selected_options"],
        "picture_choice": ["selected_option"],
        "fill_in_blanks": ["answers"],
        "matching": ["matches"],
        "matching_text": ["matches"],
        "free_text": ["text"],
        "file_upload": ["file_uploaded"],
        "audio": []
    }
    
    if assignment_type not in required_fields:
        return False
    
    for field in required_fields[assignment_type]:
        if field not in answers:
            return False
    
    # Дополнительные проверки типов
    if assignment_type == "single_choice" or assignment_type == "picture_choice":
        return isinstance(answers["selected_option"], int)
    
    elif assignment_type == "multiple_choice":
        return isinstance(answers["selected_options"], list)
    
    elif assignment_type == "fill_in_blanks":
        return isinstance(answers["answers"], list)
    
    elif assignment_type in ["matching", "matching_text"]:
        return isinstance(answers["matches"], dict)
    
    elif assignment_type == "free_text":
        return isinstance(answers["text"], str)
    
    elif assignment_type == "file_upload":
        return isinstance(answers["file_uploaded"], bool)
    
    return True


# =============================================================================
# SCORING STRATEGIES
# =============================================================================

class ScoringStrategy:
    """Базовый класс для стратегий оценивания"""
    
    @staticmethod
    def calculate_partial_score(correct: int, total: int, wrong: int = 0) -> float:
        """Вычисление частичного балла"""
        return max(0, (correct - wrong) / total) * 100


class StrictScoring(ScoringStrategy):
    """Строгое оценивание - либо все правильно, либо 0"""
    
    @staticmethod
    def calculate_score(correct: int, total: int, wrong: int = 0) -> float:
        return 100.0 if correct == total and wrong == 0 else 0.0


class PartialScoring(ScoringStrategy):
    """Частичное оценивание с вычетом за неправильные ответы"""
    
    @staticmethod
    def calculate_score(correct: int, total: int, wrong: int = 0) -> float:
        return max(0, (correct - wrong * 0.5) / total) * 100


class LenientScoring(ScoringStrategy):
    """Мягкое оценивание - только за правильные ответы"""
    
    @staticmethod
    def calculate_score(correct: int, total: int, wrong: int = 0) -> float:
        return (correct / total) * 100


def get_scoring_strategy(strategy_name: str = "partial") -> ScoringStrategy:
    """Получить стратегию оценивания"""
    strategies = {
        "strict": StrictScoring(),
        "partial": PartialScoring(),
        "lenient": LenientScoring()
    }
    
    return strategies.get(strategy_name, PartialScoring())
