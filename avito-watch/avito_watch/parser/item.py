"""Разбор страницы объявления: описание, точная дата, все фото, актуальная цена.

Данные лежат в window.__staticRouterHydrationData = JSON.parse("…") → loaderData → buyerItem.
Цену, которую видит покупатель, берём из formattedPrice.value (item.price — до скидки).
"""
from __future__ import annotations

import logging
from typing import Any

from bs4 import BeautifulSoup

from ..models import Listing
from ..utils import clean_text, item_id_from_url, parse_price, parse_published, strip_tracking
from .helpers import (extract_hydration_json, extract_images, extract_price_from_state,
                      extract_published_raw, find_loader_value, first_scalar)

log = logging.getLogger(__name__)


def parse_item(html: str, *, url: str = "", base_time=None) -> Listing | None:
    data = extract_hydration_json(html)
    buyer = find_loader_value(data, "buyerItem") if data else None
    if isinstance(buyer, dict) and "item" in buyer and isinstance(buyer["item"], dict):
        buyer = buyer["item"]

    soup = BeautifulSoup(html or "", "lxml")
    item_id = None
    if isinstance(buyer, dict):
        item_id = first_scalar(buyer, ("id", "itemId"))
    if item_id is None:
        item_id = item_id_from_url(url) or _dom_item_id(soup)
    if item_id is None:
        return None
    item_id = str(item_id)

    listing = Listing(item_id=item_id, url=strip_tracking(url) if url else f"https://www.avito.ru/items/{item_id}")
    if isinstance(buyer, dict):
        _fill_from_state(listing, buyer, base_time=base_time)
    _fill_from_dom(listing, soup)
    return listing


def _fill_from_state(listing: Listing, item: dict[str, Any], *, base_time=None) -> None:
    listing.title = clean_text(str(first_scalar(item, ("title", "name")) or listing.title))
    description = first_scalar(item, ("description", "fullDescription", "itemDescription"))
    if isinstance(description, str):
        listing.description = clean_text(description)
    price, price_text, old_price, discount = extract_price_from_state(item)
    if price is not None:
        listing.price, listing.price_source = price, "item"
    if price_text:
        listing.price_text = price_text
    listing.old_price = old_price if old_price is not None else listing.old_price
    listing.discount_pct = discount if discount is not None else listing.discount_pct
    images = extract_images(item, limit=15)
    if images:
        listing.images = images
        listing.image_url = images[0]
    published_raw = extract_published_raw(item)
    if published_raw is not None:
        listing.published_raw = str(published_raw)
        listing.published_at = parse_published(published_raw, base=base_time) or listing.published_at
    location = first_scalar(item, ("address", "location", "locationName"))
    if isinstance(location, dict):
        location = first_scalar(location, ("formatted", "name", "title"))
    if location:
        listing.location = clean_text(str(location))
    seller = item.get("seller") if isinstance(item.get("seller"), dict) else {}
    if seller.get("name"):
        listing.seller_name = clean_text(str(seller["name"]))
    if seller.get("type"):
        listing.seller_type = str(seller["type"])
    params = item.get("params")
    if isinstance(params, list):
        parts = []
        for p in params:
            if isinstance(p, dict):
                name, value = p.get("name") or p.get("title"), p.get("value") or p.get("text")
                if name and value:
                    parts.append(f"{name}: {value}")
        if parts:
            listing.params = "; ".join(parts)
    listing.raw = item


def _dom_item_id(soup: BeautifulSoup) -> str | None:
    node = soup.select_one("[data-item-id]")
    if node and str(node.get("data-item-id", "")).isdigit():
        return str(node["data-item-id"])
    meta = soup.select_one('meta[property="og:url"]')
    if meta:
        return item_id_from_url(meta.get("content", ""))
    return None


def _fill_from_dom(listing: Listing, soup: BeautifulSoup) -> None:
    if not listing.title:
        node = (soup.select_one('[data-marker="item-view/title-info"]')
                or soup.select_one("h1") or soup.select_one('meta[property="og:title"]'))
        if node is not None:
            listing.title = clean_text(node.get("content") if node.name == "meta" else node.get_text(" ", strip=True))
    if not listing.description:
        node = (soup.select_one('[data-marker="item-view/item-description"]')
                or soup.select_one('[itemprop="description"]'))
        if node is not None:
            listing.description = clean_text(node.get_text("\n", strip=True))
    if listing.price is None:
        node = (soup.select_one('[data-marker="item-view/item-price"]')
                or soup.select_one('[data-marker="item-price"]')
                or soup.select_one('[itemprop="price"]')
                or soup.select_one('meta[property="product:price:amount"]'))
        if node is not None:
            raw = node.get("content") or node.get("value") or node.get_text(" ", strip=True)
            listing.price = parse_price(raw)
            listing.price_text = listing.price_text or clean_text(str(raw))
            listing.price_source = "dom"
    if listing.published_at is None:
        node = soup.select_one('[data-marker="item-view/item-date"]')
        if node is not None:
            raw = clean_text(node.get_text(" ", strip=True))
            listing.published_raw = listing.published_raw or raw
            listing.published_at = parse_published(raw)
    if not listing.images:
        images: list[str] = []
        for img in soup.select('[data-marker*="image"] img, [itemprop="image"], gallery img'):
            src = img.get("src") or img.get("data-src")
            if src and src.startswith("http") and ".svg" not in src:
                images.append(src)
        if not images:
            meta = soup.select_one('meta[property="og:image"]')
            if meta and meta.get("content"):
                images.append(meta["content"])
        if images:
            listing.images = list(dict.fromkeys(images))
            listing.image_url = listing.images[0]
    if not listing.location:
        node = soup.select_one('[data-marker="item-view/item-address"], [itemprop="address"]')
        if node is not None:
            listing.location = clean_text(node.get_text(" ", strip=True))