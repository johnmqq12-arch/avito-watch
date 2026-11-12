"""Общие помощники разбора: поиск по вложенному JSON, компоненты iva, картинки."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from ..utils import absolute_avito_url, clean_text, parse_price

_HYDRATION_MARK = "__staticRouterHydrationData"
_MFE_RE = re.compile(r"<script[^>]*data-mfe-state[^>]*>(.*?)</script>", re.S | re.I)


def deep_find(obj: Any, key: str, *, max_depth: int = 8) -> Any:
    """Первое значение по ключу в глубоко вложенной структуре."""
    stack: list[tuple[Any, int]] = [(obj, 0)]
    while stack:
        cur, depth = stack.pop()
        if depth > max_depth:
            continue
        if isinstance(cur, dict):
            if key in cur:
                return cur[key]
            stack.extend((v, depth + 1) for v in cur.values())
        elif isinstance(cur, list):
            stack.extend((v, depth + 1) for v in cur)
    return None


def deep_find_any(obj: Any, keys: Iterable[str], *, max_depth: int = 8) -> Any:
    for key in keys:
        value = deep_find(obj, key, max_depth=max_depth)
        if value not in (None, "", [], {}):
            return value
    return None


def first_scalar(obj: Any, keys: Iterable[str]) -> Any:
    for key in keys:
        if isinstance(obj, dict) and obj.get(key) not in (None, "", [], {}):
            return obj[key]
    return None


def iva_steps(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Плоский список шагов iva (Avito Visual Architecture) со всех секций."""
    iva = item.get("iva")
    if not isinstance(iva, dict):
        return []
    steps: list[dict[str, Any]] = []
    for value in iva.values():
        if isinstance(value, list):
            steps.extend(s for s in value if isinstance(s, dict))
        elif isinstance(value, dict):
            steps.append(value)
    return steps


def iva_payloads(item: dict[str, Any], component_names: Iterable[str]) -> list[dict[str, Any]]:
    names = {n.lower() for n in component_names}
    out = []
    for step in iva_steps(item):
        cd = step.get("componentData") or {}
        name = str(cd.get("component") or step.get("component") or "").lower()
        if name in names:
            payload = step.get("payload")
            if isinstance(payload, dict):
                out.append(payload)
            elif isinstance(payload, str):
                try:
                    out.append(json.loads(payload))
                except (ValueError, TypeError):
                    pass
    return out


def extract_price_from_state(item: dict[str, Any]) -> tuple[int | None, str, int | None, int | None]:
    """(показанная цена, текст, старая цена, скидка %).

    Ловушка Авито: priceDetailed.value верхнего уровня у товара со скидкой —
    это цена ДО скидки. Показанная цена лежит в компоненте price блока PriceStep.
    """
    price = old_price = discount = None
    text = ""
    for step in iva_steps(item):
        cd = step.get("componentData") or {}
        component = str(cd.get("component") or "").lower()
        payload = step.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if component == "price":
            detailed = payload.get("priceDetailed") or {}
            price = parse_price(detailed.get("value") or payload.get("value"))
            text = clean_text(str(detailed.get("priceString") or payload.get("value") or "")) or text
        elif component == "discount":
            old_price = parse_price(payload.get("old_string") or payload.get("oldPrice"))
            discount = payload.get("percent") if isinstance(payload.get("percent"), int) else None
    if price is None:
        for key in ("formattedPrice", "priceFormatted"):
            value = item.get(key)
            if isinstance(value, dict):
                price = parse_price(value.get("value"))
                text = text or clean_text(str(value.get("value") or ""))
        if price is None:
            price = parse_price(first_scalar(item, ("price", "priceValue")))
        detailed = item.get("priceDetailed")
        if isinstance(detailed, dict) and price is None:
            price = parse_price(detailed.get("value"))
            text = text or clean_text(str(detailed.get("priceString") or ""))
    if not text and price is not None:
        text = f"{price:,}".replace(",", " ") + " ₽"
    return price, text, old_price, discount


def _urls_from_image_obj(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        url = absolute_avito_url(value)
        if url and not url.startswith("data:"):
            out.append(url)
    elif isinstance(value, dict):
        # типичный вид {"1280x960": "https://...", "432x324": "..."}
        sized = [(k, v) for k, v in value.items() if isinstance(v, str)]
        def area(key: str) -> int:
            m = re.match(r"(\d+)x(\d+)", key)
            return int(m.group(1)) * int(m.group(2)) if m else 0
        sized.sort(key=lambda kv: area(kv[0]), reverse=True)
        for _, v in sized:
            url = absolute_avito_url(v)
            if url and not url.startswith("data:"):
                out.append(url)
        for key in ("url", "src", "image", "imageUrl", "original"):
            if key in value:
                out.extend(_urls_from_image_obj(value[key]))
        # остальные вложенные структуры (например {"images": [...]})
        for key, nested in value.items():
            if key in ("url", "src", "image", "imageUrl", "original"):
                continue
            if isinstance(nested, (dict, list)):
                out.extend(_urls_from_image_obj(nested))
    elif isinstance(value, list):
        for v in value:
            out.extend(_urls_from_image_obj(v))
    return out


def extract_images(item: dict[str, Any], limit: int = 10) -> list[str]:
    candidates: list[str] = []
    for key in ("images", "image", "gallery", "photos", "imageUrls", "imageUrl"):
        if key in item:
            candidates.extend(_urls_from_image_obj(item[key]))
    for payload in iva_payloads(item, ("photo", "gallery", "image", "images", "photogallery")):
        candidates.extend(_urls_from_image_obj(payload))
    seen: set[str] = set()
    out: list[str] = []
    for url in candidates:
        if not url or url in seen or "avito.st" not in url and "avito.ru" not in url:
            continue
        if any(bad in url for bad in ("no-image", "noimage", "placeholder", ".svg")):
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= limit:
            break
    return out


def extract_published_raw(item: dict[str, Any]) -> str | int | None:
    direct = first_scalar(item, ("publishedAt", "published", "publishedDate", "datePublished",
                                 "sortTimeStamp", "time", "createdAt", "formattedDate"))
    if isinstance(direct, dict):
        direct = first_scalar(direct, ("value", "text", "date", "timestamp"))
    if direct not in (None, ""):
        return direct
    for payload in iva_payloads(item, ("date", "dateinfo", "itemdate", "publisheddate", "datebadge")):
        value = first_scalar(payload, ("text", "value", "date", "timestamp", "title"))
        if value not in (None, ""):
            return value
    # иногда дата лежит в произвольном компоненте с говорящим ключом
    for step in iva_steps(item):
        payload = step.get("payload")
        if isinstance(payload, dict):
            for key, value in payload.items():
                if "date" in key.lower() or "publish" in key.lower():
                    if isinstance(value, (str, int, float)) and value:
                        return value
    return None


def extract_hydration_json(html: str) -> dict[str, Any] | None:
    """window.__staticRouterHydrationData = JSON.parse("…") — двойное декодирование."""
    if not html:
        return None
    pos = html.find(_HYDRATION_MARK)
    if pos < 0:
        return None
    q = html.find('JSON.parse("', pos)
    if q < 0:
        # вариант без JSON.parse: сразу объект
        brace = html.find("=", pos)
        if brace < 0:
            return None
        start = html.find("{", brace)
        if start < 0:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(html, start)
            return data if isinstance(data, dict) else None
        except ValueError:
            return None
    try:
        literal, _ = json.JSONDecoder().raw_decode(html, q + len("JSON.parse("))
        data = json.loads(literal)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def extract_mfe_state(html: str) -> dict[str, Any] | None:
    """<script type="mime/invalid" data-mfe-state>{…}</script>."""
    m = _MFE_RE.search(html or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def find_loader_value(data: dict[str, Any], key: str) -> Any:
    loader = data.get("loaderData")
    if isinstance(loader, dict):
        for value in loader.values():
            if isinstance(value, dict) and key in value:
                return value[key]
            if isinstance(value, dict):
                nested = value.get("data")
                if isinstance(nested, dict) and key in nested:
                    return nested[key]
    return deep_find(data, key)