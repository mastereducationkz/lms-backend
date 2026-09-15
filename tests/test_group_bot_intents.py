"""Which answer a group-chat message asks for — the phrasings students actually use.

The rules are run without a model: everything they decide must be right on its own, and what they
cannot place must say so (``source == "default"``) so the model is asked. Real production
messages from 2026-09-12…15 are in here verbatim.
"""
import json

import pytest

from src.services import group_bot_intents as intents
from src.services.group_bot_intents import classify

ROUTED = [
    # the regular weekly timetable
    ("какое расписание?", "schedule", None),
    ("расписание", "schedule", None),
    ("скиньте расписание", "schedule", None),
    ("расписание пж", "schedule", None),
    ("какое у нас расписание", "schedule", None),
    ("здравствуйте, это постоянное расписание?", "schedule", None),
    ("в какие дни у нас уроки?", "schedule", None),
    ("по каким дням занятия", "schedule", None),
    ("какой график занятий?", "schedule", None),
    ("Расписание уроков какое?", "schedule", None),
    ("рассписание", "schedule", None),
    ("раписание скиньте", "schedule", None),
    ("расписание поменялось?", "schedule", None),
    ("у нас постоянное время уроков?", "schedule", None),
    ("какие дни недели уроки", "schedule", None),
    ("schedule", "schedule", None),
    ("what is our schedule?", "schedule", None),
    ("what days are the lessons?", "schedule", None),
    ("кесте қандай?", "schedule", None),
    ("сабақ кестесі", "schedule", None),
    ("қай күндері сабақ бар?", "schedule", None),
    # upcoming dated lessons
    ("какие ближайшие уроки?", "lessons", None),
    ("ближайшие уроки", "lessons", None),
    ("следующие занятия когда", "lessons", None),
    ("ближайшее расписание", "lessons", None),
    ("upcoming lessons", "lessons", None),
    ("next lessons", "lessons", None),
    ("келесі сабақтар қашан?", "lessons", None),
    # a day
    ("есть урок сегодня?", "lessons", "today"),
    ("во сколько сегодня урок?", "lessons", "today"),
    ("сегодня будет урок?", "lessons", "today"),
    ("сегодня verbal math или вводный урок?", "lessons", "today"),
    ("урок сегодня отменили?", "lessons", "today"),
    ("какие пары сегодня", "lessons", "today"),
    ("во сколько сегодня?", "lessons", "today"),
    ("ссылка на сегодняшний урок", "lessons", "today"),
    ("сегодняшний урок во сколько", "lessons", "today"),
    ("is there a lesson today?", "lessons", "today"),
    ("бүгін сабақ бар ма?", "lessons", "today"),
    ("завтра урок есть?", "lessons", "tomorrow"),
    ("во сколько завтра занятие", "lessons", "tomorrow"),
    ("tomorrow's lesson time?", "lessons", "tomorrow"),
    ("ертең сабақ бар ма?", "lessons", "tomorrow"),
    # the weekend
    ("уроки есть на выходных?", "lessons", "weekend"),
    ("на выходных будут занятия?", "lessons", "weekend"),
    ("any lessons this weekend?", "lessons", "weekend"),
    ("демалыс күндері сабақ бар ма?", "lessons", "weekend"),
    ("расписание на выходные", "lessons", "weekend"),
    # a week
    ("уроки на этой неделе", "lessons", "this_week"),
    ("расписание на эту неделю", "lessons", "this_week"),
    ("какие занятия на этой неделе?", "lessons", "this_week"),
    ("lessons this week", "lessons", "this_week"),
    ("осы аптада сабақ бар ма?", "lessons", "this_week"),
    ("скинь пожалуйста расписание на следующую неделю", "lessons", "next_week"),
    ("уроки на следующей неделе будут?", "lessons", "next_week"),
    ("next week schedule", "lessons", "next_week"),
    ("келесі аптада сабақ қашан?", "lessons", "next_week"),
    ("расписание на неделю", "lessons", "week"),
    # the next lesson and its link
    ("когда следующий урок?", "next", None),
    ("когда наш следующий урок", "next", None),
    ("когда урок?", "next", None),
    ("во сколько урок?", "next", None),
    ("урок будет?", "next", None),
    ("ссылка", "next", None),
    ("ссылка на урок", "next", None),
    ("скиньте ссылку на урок", "next", None),
    ("дайте ссылку на мит", "next", None),
    ("линк на урок", "next", None),
    ("где ссылка на meet?", "next", None),
    ("зум ссылка", "next", None),
    ("google meet link", "next", None),
    ("в котором часу урок", "next", None),
    ("урок во сколько начинается", "next", None),
    ("следующее занятие когда", "next", None),
    ("ближайший урок", "next", None),
    ("когда следующий?", "next", None),
    ("next lesson?", "next", None),
    ("when is the next class?", "next", None),
    ("келесі сабақ қашан?", "next", None),
    ("сабақ қашан?", "next", None),
    ("сілтеме бар ма?", "next", None),
    # homework
    ("какое дз?", "homework", None),
    ("какое домашнее задание?", "homework", None),
    ("дз есть?", "homework", None),
    ("что задали?", "homework", None),
    ("до когда дедлайн?", "homework", None),
    ("когда дедлайн по эссе?", "homework", None),
    ("какое дз на следующий урок", "homework", None),
    ("д/з", "homework", None),
    ("домашка какая", "homework", None),
    ("что по домашке", "homework", None),
    ("homework?", "homework", None),
    ("what's the homework", "homework", None),
    ("үй тапсырмасы қандай?", "homework", None),
    # recordings
    ("где запись урока?", "recording", None),
    ("скиньте запись", "recording", None),
    ("есть запись вчерашнего урока?", "recording", None),
    ("ссылка на запись", "recording", None),
    ("видео урока будет?", "recording", None),
    ("спасибо, скинь запись урока", "recording", None),
    ("recording of the last lesson", "recording", None),
    ("сабақтың жазбасы бар ма?", "recording", None),
    # weekly mock
    ("когда викли мок?", "weekly", None),
    ("когда следующий weekly mock?", "weekly", None),
    ("когда будет следующий викли мок тест", "weekly", None),
    ("мок тест на этой неделе будет?", "weekly", None),
    ("пробник когда", "weekly", None),
    ("mock test when", "weekly", None),
    ("weekly test link", "weekly", None),
    # what the bot does
    ("что ты умеешь", "help", None),
    ("что умеешь?", "help", None),
    ("привет", "help", None),
    ("здравствуйте!", "help", None),
    ("команды", "help", None),
    ("hello", "help", None),
    ("help", "help", None),
    ("сәлем", "help", None),
    ("ты тут?", "help", None),
    # personal — never answered in the room
    ("какой у меня балл?", "personal", None),
    ("сколько у меня уроков на балансе", "personal", None),
    ("я сдал домашку?", "personal", None),
    ("когда мне оплатить обучение", "personal", None),
    ("what is my balance?", "personal", None),
    ("what is my grade?", "personal", None),
    ("мой балл за мок", "personal", None),
    ("сколько у меня пропусков", "personal", None),
    ("моя оценка за дз", "personal", None),
    ("я оплатила?", "personal", None),
    ("проверили мое дз?", "personal", None),
    ("что у меня с оплатой", "personal", None),
    ("мои результаты теста", "personal", None),
    ("менің бағам қандай?", "personal", None),
    # acknowledgements — silence
    ("спасибо", "courtesy", None),
    ("спасибо!", "courtesy", None),
    ("спасибо большое", "courtesy", None),
    ("понял", "courtesy", None),
    ("понятно, спасибо", "courtesy", None),
    ("ок", "courtesy", None),
    ("👍", "courtesy", None),
    ("+", "courtesy", None),
    ("рахмет", "courtesy", None),
    ("Пасыба", "courtesy", None),
    ("отдуши брат", "courtesy", None),
    ("сесе , понял", "courtesy", None),
    ("базар жок", "courtesy", None),
    ("thanks!", "courtesy", None),
    ("жақсы", "courtesy", None),
    # a person is needed
    ("а можно перенести урок на другой день?", "curator", None),
    ("можете отменить урок?", "curator", None),
    ("у меня не открывается ссылка", "curator", None),
    ("не могу зайти на урок", "curator", None),
    ("я не смогу прийти на урок завтра", "curator", None),
    ("meet не работает", "curator", None),
    ("link doesn't work", "curator", None),
]


@pytest.mark.parametrize("text,name,span", ROUTED)
def test_the_rules_route_it(text, name, span):
    got = classify(text, use_model=False)
    assert (got.name, got.span) == (name, span), got
    assert got.source == "rules"


@pytest.mark.parametrize("text", [
    "можете мне скинуть дз?", "мне нужна ссылка на урок", "какой у нас урок сегодня?",
    "когда дедлайн?", "где запись?", "мой урок во сколько?",
])
def test_group_questions_are_not_mistaken_for_personal_ones(text):
    assert intents.is_personal(text) is False


@pytest.mark.parametrize("text,default", [
    ("когда у нас уроки?", "schedule"),
    ("занятия во сколько?", "schedule"),
    ("уроки есть?", "lessons"),
    ("урок", "next"),
])
def test_what_the_rules_cannot_place_is_left_to_the_model(text, default, monkeypatch):
    monkeypatch.setattr(intents, "ask_model", lambda question, context="": None)
    got = classify(text)
    assert (got.name, got.source) == (default, "default")

    monkeypatch.setattr(intents, "ask_model", lambda question, context="": ("lessons", "gpt-4.1-nano"))
    assert classify(text) == intents.Intent("lessons", None, "gpt-4.1-nano")


@pytest.mark.parametrize("text", [
    # verbatim from the production ledger, 2026-09-12…14: said to the room, not asked of the bot
    "на этот гугл мит заходим",
    "заходим по ссылке",
    "@add10tay @wingene09 заходим по этой ссылке",
    "Добрый день Ребята, урок будет в 17:00, можете не заходить по этой ссылке",
    "For the next lesson, please use the Google Meet link from this message",
    "hey, guys, please join. only 1 person present",
    "Попросите у бота, отметив его, либо можете напрямую найти на LMS Тут в объявлении все объясняется 🙌",
])
def test_a_statement_to_the_room_is_not_a_request(text, monkeypatch):
    monkeypatch.setattr(intents, "ask_model", lambda *a, **k: pytest.fail("a statement needs no model"))
    assert classify(text).name == "none"


@pytest.mark.parametrize("text", ["а почему небо синее?", "кто ведёт у нас?"])
def test_an_unknown_question_without_a_model_goes_to_a_person(text):
    assert classify(text, use_model=False).name == "curator"


def test_a_statement_without_a_model_is_nothing():
    assert classify("ну ладно тогда", use_model=False).name == "none"


def test_a_command_is_never_second_guessed(monkeypatch):
    monkeypatch.setattr(intents, "ask_model", lambda *a, **k: pytest.fail("a command needs no model"))
    for command in intents.COMMANDS:
        assert classify("что угодно", command=command) == intents.Intent(command, None, "command")


@pytest.mark.parametrize("text,reply_to,expected", [
    ("А на этой неделе есть?", "📚 <b>G</b>\n🧪 Weekly mock:\n• IELTS Weekly Test", ("weekly", None)),
    ("а во сколько?", "📚 <b>G</b>\n⏭ Следующий урок: Завтра, 16 сентября, 20:30–21:30", ("next", None)),
    ("а завтра?", "📚 <b>G</b>\n📅 Уроки сегодня (время Алматы):\n1. Сегодня…", ("lessons", "tomorrow")),
    ("а до какого числа?", "📚 <b>G</b>\n📝 Домашние задания:\n• Essay", ("homework", None)),
    ("а ссылка?", "Приглашение на урок\nIELTS July 8, Урок 12", ("next", None)),
])
def test_a_follow_up_takes_its_topic_from_the_bot_message_it_replies_to(text, reply_to, expected, monkeypatch):
    monkeypatch.setattr(intents, "ask_model", lambda *a, **k: pytest.fail("the context was enough"))
    got = classify(text, reply_to_text=reply_to)
    assert (got.name, got.span) == expected


# ── the model call ─────────────────────────────────────────────────────────────────────────

class _FakeClient:
    calls: list = []
    replies: list = []

    def __init__(self, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        _FakeClient.calls.append(json)
        return _FakeClient.replies.pop(0)


class _Response:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = body if isinstance(body, str) else __import__("json").dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _label(text):
    return _Response(200, {"choices": [{"message": {"content": text}}]})


@pytest.fixture
def fake_model(monkeypatch):
    import httpx

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    monkeypatch.setattr(intents, "_dead_models", set())
    monkeypatch.setattr(intents, "_cache", intents.OrderedDict())
    _FakeClient.calls, _FakeClient.replies = [], []
    return _FakeClient


def test_the_model_is_handed_only_the_question_and_asked_for_one_label(fake_model):
    fake_model.replies = [_label("this_week")]
    assert intents.ask_model("когда у нас уроки", "📅 ближайшие уроки") == ("this_week", intents.INTENT_MODELS[0])
    payload = fake_model.calls[0]
    assert payload["max_tokens"] <= 4 and payload["temperature"] == 0
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert payload["messages"][1]["content"] == (
        "Message: когда у нас уроки\nIt replies to the bot's message: 📅 ближайшие уроки")
    assert len(json.dumps(payload, ensure_ascii=False)) < 1400, "a label costs a few hundred tokens, not a prompt"


def test_an_unavailable_model_falls_back_to_the_next_and_is_not_tried_again(fake_model):
    fake_model.replies = [_Response(404, "The model `gpt-4.1-nano` does not exist"), _label("schedule"),
                          _label("lessons")]
    assert intents.ask_model("когда уроки") == ("schedule", "gpt-4o-mini")
    assert intents.ask_model("когда занятия") == ("lessons", "gpt-4o-mini")
    assert [call["model"] for call in fake_model.calls] == ["gpt-4.1-nano", "gpt-4o-mini", "gpt-4o-mini"]


def test_a_label_outside_the_list_is_no_answer(fake_model):
    fake_model.replies = [_label("Sure! The label is schedule")]
    assert intents.ask_model("когда уроки") is None


def test_the_same_question_is_labelled_once(fake_model):
    fake_model.replies = [_label("next")]
    assert intents.ask_model("урок")[0] == "next"
    assert intents.ask_model("урок") == ("next", "cache")
    assert len(fake_model.calls) == 1


def test_no_key_no_call(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert intents.ask_model("когда уроки") is None
