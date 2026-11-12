"""SQLite: дедупликация, история цен, очередь уведомлений, источники, капчи."""
import time

from avito_watch.models import Listing, SourceStatus
from avito_watch.utils import now


def make_listing(item_id="1000000001", price=50000, title="iPhone 15", description=None):
    return Listing(item_id=item_id, url=f"https://www.avito.ru/x_{item_id}", title=title,
                   price=price, price_text=f"{price} ₽", image_url="https://00.img.avito.st/image/1/a.jpg",
                   description=description, published_at=now(), source_id=1)


def test_new_then_known(db):
    listing = make_listing()
    assert db.upsert_listing(listing) == "new"
    assert db.upsert_listing(listing) == "known"
    assert db.stats()["listings"] == 1


def test_seed_marks_as_notified(db):
    db.upsert_listing(make_listing(), mark_seen_only=True)
    assert db.pending_notifications() == []
    assert db.stats()["notified"] == 1


def test_price_history_and_drop(db):
    db.upsert_listing(make_listing(price=50000))
    assert db.upsert_listing(make_listing(price=45000)) == "price_drop"
    assert db.upsert_listing(make_listing(price=47000)) == "price_up"
    history = db.price_history("1000000001")
    assert [price for _, price in history] == [47000, 45000, 50000]


def test_pending_and_mark_notified(db):
    db.upsert_listing(make_listing("1000000001"))
    db.upsert_listing(make_listing("1000000002"))
    assert len(db.pending_notifications()) == 2
    db.mark_notified("1000000001")
    pending = db.pending_notifications()
    assert [l.item_id for l in pending] == ["1000000002"]
    db.mark_notified("1000000002", error="telegram down")
    assert len(db.pending_notifications()) == 1     # при ошибке остаётся в очереди


def test_known_ids_batch(db):
    for i in range(5):
        db.upsert_listing(make_listing(f"100000000{i}"))
    known = db.known_ids([f"100000000{i}" for i in range(8)])
    assert len(known) == 5


def test_enrichment_updates_description(db):
    db.upsert_listing(make_listing())
    enriched = make_listing(description="Полное описание объявления")
    db.upsert_listing(enriched)
    stored = db.get_listing("1000000001")
    assert stored.description == "Полное описание объявления"
    assert stored.price == 50000


def test_sources_crud_and_due(db):
    source = db.add_source("https://www.avito.ru/moskva?q=iphone", name="iPhone", interval_sec=120)
    assert source.id > 0
    assert db.add_source("https://www.avito.ru/moskva?q=iphone").id == source.id  # без дублей
    db.update_source(source.id, next_check_at=time.time() - 1)
    assert [s.id for s in db.due_sources()] == [source.id]
    db.update_source(source.id, status=SourceStatus.PAUSED.value)
    assert db.due_sources() == []
    db.update_source(source.id, status=SourceStatus.ACTIVE.value, interval_sec=60)
    assert db.get_source(source.id).interval_sec == 60
    assert db.remove_source(source.id) is True
    assert db.get_source(source.id) is None


def test_source_filters_roundtrip(db):
    source = db.add_source("https://www.avito.ru/x", keywords_exclude=["запчасти", "копия"],
                           min_price=1000, max_price=90000)
    loaded = db.get_source(source.id)
    assert loaded.keywords_exclude == ["запчасти", "копия"]
    assert (loaded.min_price, loaded.max_price) == (1000, 90000)


def test_captcha_events(db):
    event_id = db.log_captcha(1, "geetest", "https://www.avito.ru/x", method="slider-cv", attempts=2)
    assert db.last_captcha_at(1) is not None
    db.resolve_captcha(event_id, "manual", True, "решено человеком")
    stats = db.stats()
    assert stats["captcha_total"] == 1
    assert stats["captcha_solved_manual"] == 1
    assert stats["captcha_solved_auto"] == 0


def test_meta_and_export(db):
    db.set_meta("chat_id", "100500")
    assert db.get_meta("chat_id") == "100500"
    db.upsert_listing(make_listing())
    rows = db.export_listings()
    assert rows and rows[0]["item_id"] == "1000000001"


def test_counters(db):
    source = db.add_source("https://www.avito.ru/y")
    db.bump_source_counters(source.id, found=3, notified=2)
    db.bump_source_counters(source.id, found=1, notified=1)
    loaded = db.get_source(source.id)
    assert (loaded.found_total, loaded.notified_total) == (4, 3)