"""Конвейер: фиксация существующих, дедупликация, фильтры, обогащение, капча."""
import pytest

from avito_watch.captcha.manual import ManualCenter
from avito_watch.captcha.solver import CaptchaSolver
from avito_watch.models import PageState
from avito_watch.pipeline import Pipeline
from conftest import FakeEngine, load_html, make_card, make_listing_page

URL = "https://www.avito.ru/moskva?q=iphone"


@pytest.fixture
def source(db):
    return db.add_source(URL, name="iPhone", interval_sec=120)


async def test_first_cycle_only_seeds(cfg, db, fake_engine, fake_telegram, source):
    pipeline = Pipeline(cfg, db, fake_engine, fake_telegram)
    report = await pipeline.check_source(source)
    assert report.state is PageState.CONTENT
    assert report.seeded == 4
    assert report.notified == 0
    assert fake_telegram.listings == []
    assert db.get_meta(f"seeded:{source.id}") == "1"
    assert db.get_source(source.id).seed_existing is False


async def test_second_cycle_no_duplicates(cfg, db, fake_engine, fake_telegram, source):
    pipeline = Pipeline(cfg, db, fake_engine, fake_telegram)
    await pipeline.check_source(source)
    report = await pipeline.check_source(source)
    assert report.new == 0
    assert report.notified == 0
    assert fake_telegram.listings == []


async def test_new_listing_is_sent_once(cfg, db, fake_engine, fake_telegram, source):
    pipeline = Pipeline(cfg, db, fake_engine, fake_telegram)
    await pipeline.check_source(source)

    html = load_html("listing_state").replace(
        "</div>\n<div data-marker=\"itemsCarousel\">",
        make_card("7654321999", "iPhone 15 Pro Max 512", 110000) + "</div>\n<div data-marker=\"itemsCarousel\">")
    fake_engine.default = html
    report = await pipeline.check_source(source)
    assert report.new == 1
    assert report.notified == 1
    assert len(fake_telegram.listings) == 1
    assert fake_telegram.listings[0][0].item_id == "7654321999"

    # третий цикл: то же объявление повторно не приходит
    again = await pipeline.check_source(source)
    assert again.new == 0 and len(fake_telegram.listings) == 1


async def test_price_drop_notification(cfg, db, fake_engine, fake_telegram, source):
    pipeline = Pipeline(cfg, db, fake_engine, fake_telegram)
    await pipeline.check_source(source)
    fake_engine.default = load_html("listing_state").replace('"value": 62990', '"value": 50000')
    report = await pipeline.check_source(source)
    assert report.price_drops == 1
    assert any(kind == "price_drop" for _, kind in fake_telegram.listings)


async def test_small_price_change_ignored(cfg, db, fake_engine, fake_telegram, source):
    cfg.telegram.price_drop_min_pct = 20
    pipeline = Pipeline(cfg, db, fake_engine, fake_telegram)
    await pipeline.check_source(source)
    fake_engine.default = load_html("listing_state").replace('"value": 62990', '"value": 62000')
    report = await pipeline.check_source(source)
    assert report.price_drops == 0


async def test_keyword_and_price_filters(cfg, db, fake_engine, fake_telegram):
    source = db.add_source(URL, name="фильтры", keywords_exclude=["коробка"], max_price=60000)
    pipeline = Pipeline(cfg, db, fake_engine, fake_telegram)
    report = await pipeline.check_source(source)
    # из 4 объявлений: «коробка» отсеяна словом, iPhone 15 Plus (55500) проходит,
    # iPhone 15 Pro (62990) отсеян ценой
    assert report.seeded == 2
    assert report.filtered == 2


async def test_enrichment_fills_description(cfg, db, fake_telegram, source):
    cfg.watch.enrich_items = True
    cfg.watch.enrich_delay_sec = 0
    engine = FakeEngine(default=make_listing_page([make_card("7654321777", "Новое объявление", 42000)]))
    pipeline = Pipeline(cfg, db, engine, fake_telegram)
    await pipeline.check_source(source)          # фиксация

    engine.default = make_listing_page([make_card("7654321777", "Новое объявление", 42000),
                                        make_card("7654321888", "Совсем новое", 39000)])
    engine.set("https://www.avito.ru/moskva/tovar_7654321888", load_html("item"))
    report = await pipeline.check_source(source)
    assert report.new == 1 and report.enriched == 1
    stored = db.get_listing("7654321888")
    assert stored.description and "полный комплект" in stored.description


async def test_captcha_stops_cycle_and_notifies(cfg, db, fake_telegram, source):
    cfg.captcha.auto_solve = False
    cfg.captcha.manual_wait_sec = 0.3
    engine = FakeEngine(default=load_html("firewall_captcha"))
    manual = ManualCenter()
    solver = CaptchaSolver(cfg, db, fake_telegram, manual, engine=engine)
    pipeline = Pipeline(cfg, db, engine, fake_telegram, solver)
    report = await pipeline.check_source(source)
    assert report.state is PageState.CAPTCHA
    assert engine.captcha_calls == 1
    assert db.stats()["captcha_total"] == 1
    assert any("капчу" in m.lower() for m in fake_telegram.messages)
    assert fake_telegram.photos, "скриншот капчи должен уйти в Telegram"


async def test_blocked_state_reported(cfg, db, fake_telegram, source):
    engine = FakeEngine(default=load_html("firewall_blocked"))
    pipeline = Pipeline(cfg, db, engine, fake_telegram)
    report = await pipeline.check_source(source)
    assert report.state is PageState.BLOCKED
    assert report.found == 0


async def test_multiple_pages(cfg, db, fake_telegram):
    source = db.add_source(URL, name="две страницы", pages=2, seed_existing=False)
    engine = FakeEngine(default=make_listing_page([make_card("7000000010", "Страница 1", 1000)]))
    engine.set(URL + "&p=2", make_listing_page([make_card("7000000011", "Страница 2", 2000)]))
    pipeline = Pipeline(cfg, db, engine, fake_telegram)
    report = await pipeline.check_source(source)
    assert report.pages_read == 2
    assert report.new == 2


async def test_backlog_flush(cfg, db, fake_engine, fake_telegram, source):
    pipeline = Pipeline(cfg, db, fake_engine, fake_telegram)
    await pipeline.check_source(source)
    db._conn.execute("UPDATE listings SET notified_at=NULL WHERE item_id='7654321098'")
    sent = await pipeline.flush_backlog()
    assert sent == 1