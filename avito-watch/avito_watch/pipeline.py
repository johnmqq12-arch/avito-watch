"""Конвейер одного цикла: загрузка выдачи → дедупликация → обогащение → Telegram."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .config import Config
from .db import Database
from .engine.base import Engine
from .models import Listing, PageState, Source
from .notify.telegram import Telegram
from .parser.listing import next_page_url, parse_listing
from .parser.item import parse_item
from .utils import asleep, clean_text, jitter, strip_tracking

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    source_id: int
    label: str
    state: PageState = PageState.UNKNOWN
    found: int = 0
    new: int = 0
    notified: int = 0
    price_drops: int = 0
    filtered: int = 0
    enriched: int = 0
    seeded: int = 0
    error: str | None = None
    pages_read: int = 0
    duration: float = 0.0
    details: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state in (PageState.CONTENT, PageState.EMPTY) and not self.error


class Pipeline:
    def __init__(self, cfg: Config, db: Database, engine: Engine, telegram: Telegram, solver=None):
        self.cfg = cfg
        self.db = db
        self.engine = engine
        self.telegram = telegram
        self.solver = solver

    # ------------------------------------------------------------------ main
    async def check_source(self, source: Source) -> CycleReport:
        started = time.time()
        report = CycleReport(source_id=source.id, label=source.label)
        collected: dict[str, Listing] = {}

        for page_num in range(1, max(1, source.pages) + 1):
            url = strip_tracking(source.url) if page_num == 1 else next_page_url(source.url, page_num)
            result = await self.engine.fetch(url, source_id=source.id, captcha_handler=self.solver)
            report.state = result.state
            report.pages_read += 1
            if result.error:
                report.error = result.error
            if not result.ok:
                report.details.append(f"стр.{page_num}: {result.state.value}")
                break

            listings = parse_listing(result.html, source_id=source.id, page_url=url)
            fresh = [l for l in listings if l.item_id not in collected]
            for listing in listings:
                collected.setdefault(listing.item_id, listing)
            report.found += len(listings)
            if not listings or (page_num > 1 and not fresh):
                break
            if page_num < source.pages:
                await asleep(jitter(self.cfg.browser.page_delay_sec))

        report.found = len(collected)
        if not report.ok:
            report.duration = time.time() - started
            return report

        candidates = [l for l in collected.values() if self._passes_filters(l, source)]
        report.filtered = len(collected) - len(candidates)

        # первый проход по источнику: только фиксируем, чтобы не прислать 50 старых объявлений
        seed_key = f"seeded:{source.id}"
        if source.seed_existing and self.db.get_meta(seed_key) != "1":
            for listing in candidates:
                self.db.upsert_listing(listing, mark_seen_only=True)
            self.db.set_meta(seed_key, "1")
            self.db.update_source(source.id, seed_existing=False)
            report.seeded = len(candidates)
            report.new = 0
            await self.telegram.send_text(
                f"📌 Источник <b>{source.label}</b> подключён.\n"
                f"Зафиксировал {len(candidates)} существующих объявлений — они не придут в чат.\n"
                f"Дальше присылаю только новые. Интервал: {source.interval_sec} с.")
            report.duration = time.time() - started
            return report

        new_listings: list[Listing] = []
        drops: list[tuple[Listing, int]] = []
        for listing in candidates:
            previous = self.db.get_listing(listing.item_id)
            old_price = previous.price if previous else None
            outcome = self.db.upsert_listing(listing)
            if outcome == "new":
                new_listings.append(listing)
            elif outcome == "price_drop" and self.cfg.telegram.notify_price_drop:
                if old_price and listing.price is not None:
                    pct = (1 - listing.price / old_price) * 100
                    if pct >= self.cfg.telegram.price_drop_min_pct:
                        drops.append((listing, old_price))
        report.new = len(new_listings)
        report.price_drops = len(drops)
        self.db.bump_source_counters(source.id, found=report.new)

        # обогащение: описание и точная дата живут на странице объявления
        limit = self.cfg.watch.enrich_max_per_cycle
        if self.cfg.watch.enrich_items and new_listings and limit > 0:
            for listing in new_listings[:limit]:
                await asleep(jitter(self.cfg.watch.enrich_delay_sec))
                enriched = await self._enrich(listing)
                if enriched:
                    report.enriched += 1

        # отправка
        send_limit = self.cfg.watch.max_new_per_cycle
        for listing in new_listings[:send_limit]:
            stored = self.db.get_listing(listing.item_id) or listing
            ok = await self.telegram.send_listing(stored, source_label=source.label, kind="new")
            if ok:
                self.db.mark_notified(listing.item_id)
                report.notified += 1
            else:
                self.db.mark_notified(listing.item_id, error="telegram send failed")
        if len(new_listings) > send_limit:
            report.details.append(f"отложено {len(new_listings) - send_limit} объявлений (лимит цикла)")

        for listing, old_price in drops[:send_limit]:
            stored = self.db.get_listing(listing.item_id) or listing
            await self.telegram.send_listing(stored, source_label=source.label,
                                             kind="price_drop", old_price=old_price)

        self.db.bump_source_counters(source.id, notified=report.notified)
        report.duration = time.time() - started
        log.info("Цикл %s: найдено=%d новых=%d отправлено=%d снижений_цены=%d (%.1fs)",
                 source.label, report.found, report.new, report.notified, report.price_drops,
                 report.duration)
        return report

    # ------------------------------------------------------------------ enrich
    async def _enrich(self, listing: Listing) -> bool:
        try:
            result = await self.engine.fetch(listing.url, is_item=True, source_id=listing.source_id,
                                             captcha_handler=self.solver)
        except Exception as exc:
            log.warning("Обогащение %s: %s", listing.url, exc)
            return False
        if not result.ok:
            log.info("Обогащение %s пропущено: %s", listing.url, result.state.value)
            return False
        item = parse_item(result.html, url=listing.url)
        if item is None:
            return False
        merged = Listing(
            item_id=listing.item_id, url=listing.url,
            title=item.title or listing.title,
            price=item.price if item.price is not None else listing.price,
            price_text=item.price_text or listing.price_text,
            currency=listing.currency,
            old_price=item.old_price if item.old_price is not None else listing.old_price,
            discount_pct=item.discount_pct if item.discount_pct is not None else listing.discount_pct,
            image_url=item.image_url or listing.image_url,
            images=item.images or listing.images,
            description=item.description or listing.description,
            published_at=item.published_at or listing.published_at,
            published_raw=item.published_raw or listing.published_raw,
            location=item.location or listing.location,
            seller_name=item.seller_name or listing.seller_name,
            seller_type=item.seller_type or listing.seller_type,
            params=item.params or listing.params,
            source_id=listing.source_id,
            price_source=item.price_source if item.price is not None else listing.price_source,
        )
        self.db.upsert_listing(merged)
        self.db.mark_enriched(listing.item_id)
        return True

    # ------------------------------------------------------------------ filters
    def _passes_filters(self, listing: Listing, source: Source) -> bool:
        if source.min_price is not None and listing.price is not None and listing.price < source.min_price:
            return False
        if source.max_price is not None and listing.price is not None and listing.price > source.max_price:
            return False
        haystack = f"{listing.title} {clean_text(listing.description or '')}".lower()
        if source.keywords_include and not any(k.lower() in haystack for k in source.keywords_include):
            return False
        if source.keywords_exclude and any(k.lower() in haystack for k in source.keywords_exclude):
            return False
        return True

    # ------------------------------------------------------------------ backlog
    async def flush_backlog(self) -> int:
        if not self.cfg.watch.notify_backlog:
            return 0
        pending = self.db.pending_notifications(limit=self.cfg.watch.max_new_per_cycle)
        sent = 0
        for listing in pending:
            source = self.db.get_source(listing.source_id) if listing.source_id else None
            ok = await self.telegram.send_listing(listing, source_label=source.label if source else "")
            if ok:
                self.db.mark_notified(listing.item_id)
                sent += 1
            else:
                break
        if sent:
            log.info("Дослал %d объявлений из очереди", sent)
        return sent