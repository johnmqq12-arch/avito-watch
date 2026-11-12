"""Разбор страницы выдачи (поиск/категория).

Авито отдаёт один и тот же URL в двух видах:
  1) только DOM-разметка с микроразметкой schema.org;
  2) DOM + встроенный JSON <script data-mfe-state> (больше полей: дата публикации,
     старая цена, тип продавца).
Читаем оба источника: JSON приоритетнее, DOM закрывает дыры.
"""
from __future__ import annotations

import logging
from typing import Any

from bs4 import BeautifulSoup

from ..models import Listing
from ..utils import (absolute_avito_url, clean_text, item_id_from_url, parse_price,
                     parse_published, strip_tracking)
from .helpers import (extract_images, extract_mfe_state, extract_price_from_state,
                      extract_published_raw, find_loader_value, first_scalar)

log = logging.getLogger(__name__)

SIMILAR_HEADING = "Похожие объявления"


def parse_listing(html: str, *, source_id: int | None = None, page_url: str = "",
                  base_time=None) -> list[Listing]:
    state_items = _state_items(html)
    soup = BeautifulSoup(html or "", "lxml")
    dom_cards = _dom_cards(soup, html)

    listings: dict[str, Listing] = {}
    order: list[str] = []

    for raw in state_items:
        listing = _from_state(raw, source_id=source_id, base_time=base_time)
        if not listing:
            continue
        if listing.item_id not in listings:
            order.append(listing.item_id)
        listings[listing.item_id] = listing

    for item_id, dom in dom_cards.items():
        if item_id in listings:
            _merge(listings[item_id], dom)
        else:
            dom.source_id = source_id
            listings[item_id] = dom
            order.append(item_id)

    result = [listings[i] for i in order]
    log.debug("Выдача %s: state=%d dom=%d итог=%d", page_url, len(state_items), len(dom_cards), len(result))
    return result


# ---------------------------------------------------------------- JSON state
def _state_items(html: str) -> list[dict[str, Any]]:
    state = extract_mfe_state(html)
    if not state:
        return []
    catalog = find_loader_value(state, "catalog")
    if not isinstance(catalog, dict):
        return []
    items = catalog.get("items")
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


def _from_state(raw: dict[str, Any], *, source_id: int | None, base_time=None) -> Listing | None:
    item_id = first_scalar(raw, ("id", "itemId", "sku", "avitoId"))
    if item_id is None:
        return None
    item_id = str(item_id)
    path = first_scalar(raw, ("urlPath", "url", "itemUrl", "link"))
    url = absolute_avito_url(str(path)) if path else f"https://www.avito.ru/items/{item_id}"
    price, price_text, old_price, discount = extract_price_from_state(raw)
    images = extract_images(raw)
    published_raw = extract_published_raw(raw)
    location = first_scalar(raw, ("location", "locationName", "address", "geo"))
    if isinstance(location, dict):
        location = first_scalar(location, ("name", "formatted", "title", "address"))
    seller = raw.get("seller") if isinstance(raw.get("seller"), dict) else {}
    return Listing(
        item_id=item_id,
        url=strip_tracking(url),
        title=clean_text(str(first_scalar(raw, ("title", "name")) or "")),
        price=price,
        price_text=price_text,
        old_price=old_price,
        discount_pct=discount,
        image_url=images[0] if images else None,
        images=images,
        description=clean_text(str(raw["description"])) if isinstance(raw.get("description"), str) else None,
        published_at=parse_published(published_raw, base=base_time),
        published_raw=str(published_raw) if published_raw is not None else None,
        location=clean_text(str(location)) if location else None,
        seller_name=clean_text(str(seller.get("name"))) if seller.get("name") else None,
        seller_type=str(seller.get("type") or raw.get("sellerType") or "") or None,
        source_id=source_id,
        price_source="state",
        raw=raw,
    )


# ---------------------------------------------------------------- DOM
def _dom_cards(soup: BeautifulSoup, html: str) -> dict[str, Listing]:
    """Обход в документном порядке: карусель рекомендаций пропускаем,
    всё после заголовка «Похожие объявления…» отрезаем."""
    out: dict[str, Listing] = {}
    headings = ("h1", "h2", "h3", "h4")
    for element in soup.descendants:
        name = getattr(element, "name", None)
        if name is None:
            continue
        if name in headings:
            if SIMILAR_HEADING in element.get_text(" ", strip=True):
                break
        else:
            marker = (element.get("data-marker") or "").lower()
            if "similar" in marker or "related" in marker:
                break
            direct = element.find(string=True, recursive=False)
            if direct and SIMILAR_HEADING in direct:
                break
        if element.get("data-marker") != "item":
            continue
        item_id = str(element.get("data-item-id") or "").strip()
        if not item_id.isdigit():
            continue
        if element.find_parent(attrs={"data-marker": "itemsCarousel"}) is not None:
            continue
        listing = _from_dom(element, item_id)
        if listing:
            out.setdefault(item_id, listing)
    return out


def _text(node) -> str:
    return clean_text(node.get_text(" ", strip=True)) if node else ""


def _from_dom(card, item_id: str) -> Listing | None:
    title_node = card.select_one('[data-marker="item-title"]') or card.select_one('[itemprop="name"]')
    title = _text(title_node) or clean_text(card.get("title") or "")

    link = card.select_one('a[itemprop="url"]') or card.select_one('a[href]')
    url = absolute_avito_url(link.get("href") if link else None)
    if not url:
        url = f"https://www.avito.ru/items/{item_id}"

    price_node = card.select_one('[itemprop="price"]')
    price_text = ""
    price = None
    if price_node is not None:
        raw_price = price_node.get("content") or price_node.get("value") or _text(price_node)
        price = parse_price(raw_price)
        price_text = clean_text(str(_text(price_node) or raw_price or ""))
    if price is None:
        node = card.select_one('[data-marker="item-price"]')
        if node is not None:
            price_text = _text(node)
            price = parse_price(price_text)

    image_url = _dom_image(card)
    date_node = card.select_one('[data-marker="item-date"]') or card.select_one('[data-marker="item-date/text"]')
    published_raw = _text(date_node) or None
    params = _text(card.select_one('[data-marker="item-specific-params"]')) or None
    location = _text(card.select_one('[data-marker="item-location"]')) or None
    description = _text(card.select_one('[data-marker="item-description"]')) or None

    return Listing(
        item_id=item_id,
        url=strip_tracking(url),
        title=title,
        price=price,
        price_text=price_text,
        image_url=image_url,
        images=[image_url] if image_url else [],
        description=description,
        published_at=parse_published(published_raw),
        published_raw=published_raw,
        location=location,
        params=params,
        price_source="dom",
    )


def _dom_image(card) -> str | None:
    for selector in ('img[itemprop="image"]', '[data-marker="item-photo"] img', "img"):
        for img in card.select(selector):
            for attr in ("src", "data-src", "data-original"):
                url = absolute_avito_url(img.get(attr))
                if url and not url.startswith("data:") and ".svg" not in url:
                    return url
            srcset = img.get("srcset")
            if srcset:
                parts = [p.strip().split(" ")[0] for p in srcset.split(",") if p.strip()]
                for part in reversed(parts):
                    url = absolute_avito_url(part)
                    if url and not url.startswith("data:"):
                        return url
    return None


def _merge(target: Listing, dom: Listing) -> None:
    """DOM закрывает то, чего не оказалось в JSON (и наоборот — JSON уже приоритетен)."""
    for field in ("title", "price", "price_text", "image_url", "location", "params",
                  "description", "published_raw"):
        if not getattr(target, field) and getattr(dom, field):
            setattr(target, field, getattr(dom, field))
    if target.published_at is None and dom.published_at is not None:
        target.published_at = dom.published_at
    if not target.images and dom.images:
        target.images = dom.images
    if target.url.startswith("https://www.avito.ru/items/") and dom.url:
        target.url = dom.url
    if not target.item_id:
        target.item_id = dom.item_id
    if target.price_source == "state" and target.price is None and dom.price is not None:
        target.price, target.price_source = dom.price, "dom"


def next_page_url(url: str, page: int) -> str:
    """?p=N — разрешённая пагинация Авито."""
    base = url.split("#")[0]
    if "p=" in base:
        import re
        return re.sub(r"([?&])p=\d+", rf"\g<1>p={page}", base)
    return base + ("&" if "?" in base else "?") + f"p={page}"


def looks_like_item_url(url: str) -> bool:
    return item_id_from_url(url) is not None