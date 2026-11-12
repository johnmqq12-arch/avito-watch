"""Управление парсером из Telegram: команды, кнопки, доступ, ответы на капчу."""
import pytest

from avito_watch.bot.commands import BotController
from avito_watch.captcha.manual import ManualCenter
from avito_watch.models import CaptchaVariant, SourceStatus
from avito_watch.pipeline import Pipeline
from avito_watch.watcher import Watcher

URL = "https://www.avito.ru/moskva?q=iphone"


@pytest.fixture
def manual():
    return ManualCenter()


@pytest.fixture
def bot(cfg, db, fake_engine, fake_telegram, manual):
    pipeline = Pipeline(cfg, db, fake_engine, fake_telegram)
    watcher = Watcher(cfg, db, fake_engine, fake_telegram, pipeline, manual=manual)
    return BotController(cfg, db, fake_telegram, watcher, manual)


def message(text: str, chat_id: str = "100500", user_id: str = "100500", **extra) -> dict:
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "from": {"id": user_id},
                                        "text": text, **extra}}


def callback(data: str, chat_id: str = "100500", user_id: str = "100500") -> dict:
    return {"update_id": 2, "callback_query": {"id": "cb1", "data": data,
                                              "from": {"id": user_id},
                                              "message": {"chat": {"id": chat_id}}}}


async def test_help_and_id(bot, fake_telegram):
    await bot._handle(message("/help"))
    assert "/add" in fake_telegram.messages[-1]
    await bot._handle(message("/id"))
    assert "100500" in fake_telegram.messages[-1]


async def test_stranger_cannot_control_bot(bot, fake_telegram, db):
    await bot._handle(message("/add " + URL, chat_id="999", user_id="999"))
    assert db.list_sources() == []
    assert fake_telegram.messages == []


async def test_start_binds_chat_when_not_configured(bot, cfg, db, fake_telegram):
    cfg.telegram.chat_id = ""
    cfg.telegram.admin_ids = []
    await bot._handle(message("/start", chat_id="777", user_id="777"))
    assert cfg.telegram.chat_id == "777"
    assert db.get_meta("chat_id") == "777"
    assert cfg.telegram.admin_ids == ["777"]


async def test_add_list_interval_remove(bot, db, fake_telegram):
    await bot._handle(message(f"/add {URL} iPhone Москва"))
    source = db.list_sources()[0]
    assert source.label == "iPhone Москва"
    assert "Первый проход" in fake_telegram.messages[-1]

    await bot._handle(message("/list"))
    assert "iPhone Москва" in fake_telegram.messages[-1]

    await bot._handle(message(f"/interval {source.id} 600"))
    assert db.get_source(source.id).interval_sec == 600

    await bot._handle(message(f"/remove {source.id}"))
    assert db.list_sources() == []


async def test_add_rejects_non_avito(bot, db, fake_telegram):
    await bot._handle(message("/add https://example.com/iphone"))
    assert db.list_sources() == []
    assert "не ссылка Авито" in fake_telegram.messages[-1]


async def test_interval_warns_about_ban_risk(bot, db, fake_telegram):
    await bot._handle(message(f"/add {URL}"))
    source = db.list_sources()[0]
    await bot._handle(message(f"/interval {source.id} 20"))
    assert any("слишком часто" in m for m in fake_telegram.messages)
    assert db.get_source(source.id).interval_sec == 20      # ставим, но предупредив


async def test_bad_arguments_are_explained(bot, fake_telegram):
    for command in ("/add", "/remove x", "/interval 1", "/check abc"):
        await bot._handle(message(command))
        assert "Формат:" in fake_telegram.messages[-1]
    await bot._handle(message("/чушь"))
    assert "Не знаю команду" in fake_telegram.messages[-1]


async def test_pause_and_resume_single_source(bot, db, fake_telegram):
    await bot._handle(message(f"/add {URL}"))
    source = db.list_sources()[0]
    await bot._handle(message(f"/pause {source.id}"))
    assert db.get_source(source.id).status == SourceStatus.PAUSED.value
    await bot._handle(message(f"/resume {source.id}"))
    assert db.get_source(source.id).status == SourceStatus.ACTIVE.value


async def test_global_pause_and_resume(bot, db, fake_telegram):
    await bot._handle(message(f"/add {URL}"))
    source = db.list_sources()[0]
    await bot._handle(message("/pause"))
    assert bot.watcher.paused_globally is True
    bot.watcher.pause_source(source.id)
    await bot._handle(message("/resume"))
    assert bot.watcher.paused_globally is False
    assert db.get_source(source.id).status == SourceStatus.ACTIVE.value


async def test_check_now_reports_result(bot, db, fake_telegram):
    await bot._handle(message(f"/add {URL} iPhone"))
    source = db.list_sources()[0]
    await bot._handle(message(f"/check {source.id}"))
    assert any("Состояние: content" in m for m in fake_telegram.messages)
    assert bot.watcher.cycles == 1


async def test_answer_without_captcha(bot, fake_telegram):
    await bot._handle(message("/answer ab12"))
    assert "нет капчи" in fake_telegram.messages[-1]


async def test_answer_reaches_pending_captcha(bot, manual, fake_telegram):
    pending = manual.register(1, 5, CaptchaVariant.IMAGE, "iPhone")
    await bot._handle(message("/answer ab12cd"))
    assert pending.answers.get_nowait() == "ab12cd"
    assert "Пробую ответ" in fake_telegram.messages[-1]


async def test_plain_reply_to_captcha_message_is_an_answer(bot, manual, fake_telegram):
    pending = manual.register(1, 5, CaptchaVariant.IMAGE, "iPhone")
    await bot._handle(message("ab12cd", reply_to_message={"message_id": 10}))
    assert pending.answers.get_nowait() == "ab12cd"


async def test_solved_button_and_command(bot, manual, fake_telegram):
    pending = manual.register(1, 5, CaptchaVariant.GEETEST, "iPhone")
    await bot._handle(message("/solved"))
    assert pending.done.is_set()
    await bot._handle(callback("cap:done:1"))
    assert fake_telegram.callbacks[-1][0] == "cb1"


async def test_skip_button(bot, manual, fake_telegram):
    pending = manual.register(1, 5, CaptchaVariant.GEETEST, "iPhone")
    await bot._handle(callback("cap:skip:1"))
    assert pending.skip.is_set()
    assert "Пропускаю" in fake_telegram.callbacks[-1][1]


async def test_source_buttons(bot, db, fake_telegram):
    await bot._handle(message(f"/add {URL}"))
    source = db.list_sources()[0]
    await bot._handle(callback(f"src:pause:{source.id}"))
    assert db.get_source(source.id).status == SourceStatus.PAUSED.value
    await bot._handle(callback(f"src:resume:{source.id}"))
    assert db.get_source(source.id).status == SourceStatus.ACTIVE.value


async def test_callback_from_stranger_is_rejected(bot, db, fake_telegram):
    await bot._handle(message(f"/add {URL}"))
    source = db.list_sources()[0]
    await bot._handle(callback(f"src:pause:{source.id}", chat_id="999", user_id="999"))
    assert db.get_source(source.id).status == SourceStatus.ACTIVE.value
    assert "Нет доступа" in fake_telegram.callbacks[-1][1]


async def test_stats_and_status(bot, db, fake_telegram):
    await bot._handle(message(f"/add {URL}"))
    await bot._handle(message("/stats"))
    assert "Объявлений в базе" in fake_telegram.messages[-1]
    await bot._handle(message("/status"))
    assert "Движок: fake" in fake_telegram.messages[-1]


async def test_export_sends_csv(bot, db, fake_telegram, fake_engine):
    await bot._handle(message("/export"))
    assert "База пуста" in fake_telegram.messages[-1]

    await bot._handle(message(f"/add {URL} iPhone"))
    source = db.list_sources()[0]
    await bot.watcher.pipeline.check_source(source)          # фиксируем объявления
    await bot._handle(message("/export"))
    method, payload, files = fake_telegram.api_calls[-1]
    assert method == "sendDocument"
    assert files["document"][0].endswith(".csv")
    assert b"item_id" in files["document"][1]


async def test_command_error_is_reported_not_crashed(bot, fake_telegram, monkeypatch):
    async def boom(chat_id, args):
        raise RuntimeError("сломалось")

    monkeypatch.setattr(bot, "_cmd_help", boom)
    await bot._handle(message("/help"))
    assert "сломалось" in fake_telegram.messages[-1]
