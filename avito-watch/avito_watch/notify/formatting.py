"""Форматирование сообщений: картинка, цена, дата, описание, ссылка."""
from __future__ import annotations

import html

from ..config import TelegramConfig
from ..models import Listing
from ..utils import clean_text, format_price, human_date, truncate


def esc(value: str | None) -> str:
    return html.escape(value or "", quote=False)


def build_caption(listing: Listing, source_label: str, cfg: TelegramConfig) -> str:
    lines: list[str] = []
    title = esc(truncate(clean_text(listing.title) or "Без названия", 160))
    lines.append(f"🆕 <b>{title}</b>")

    price_line = f"💰 <b>{esc(format_price(listing.price, listing.currency))}</b>"
    if listing.price is None and listing.price_text:
        price_line = f"💰 <b>{esc(truncate(listing.price_text, 40))}</b>"
    if listing.old_price and listing.price and listing.old_price > listing.price:
        pct = listing.discount_pct or round((1 - listing.price / listing.old_price) * 100)
        price_line += f"  <s>{esc(format_price(listing.old_price, listing.currency))}</s> (−{pct}%)"
    lines.append(price_line)

    if listing.location:
        lines.append(f"📍 {esc(truncate(listing.location, 90))}")
    lines.append(f"🕒 {esc(human_date(listing.published_at))}")
    if listing.params:
        lines.append(f"⚙️ {esc(truncate(listing.params, 110))}")
    if listing.seller_name:
        seller = listing.seller_name
        if listing.seller_type:
            seller += f" · {listing.seller_type}"
        lines.append(f"👤 {esc(truncate(seller, 70))}")
    if source_label:
        lines.append(f"🔎 {esc(truncate(source_label, 60))}")

    description = clean_text(listing.description or "")
    if description:
        budget = 1024 - len("\n".join(lines)) - 30
        limit = max(80, min(cfg.description_limit, budget))
        lines.append("")
        lines.append(f"📝 {esc(truncate(description, limit))}")
    else:
        lines.append("")
        lines.append("📝 описание на странице объявления")

    lines.append(f"🔗 {esc(listing.url)}")
    caption = "\n".join(lines)
    if len(caption) > 1024:  # страховка: подпись к фото ограничена Telegram
        caption = caption[:1020] + "…"
    return caption


def build_price_drop_caption(listing: Listing, old_price: int | None, source_label: str) -> str:
    lines = [f"📉 <b>Цена снизилась</b>",
             f"{esc(truncate(clean_text(listing.title), 150))}"]
    if old_price is not None and listing.price is not None:
        pct = round((1 - listing.price / old_price) * 100) if old_price else 0
        lines.append(f"💰 <s>{esc(format_price(old_price, listing.currency))}</s> → "
                     f"<b>{esc(format_price(listing.price, listing.currency))}</b> (−{pct}%)")
    else:
        lines.append(f"💰 <b>{esc(format_price(listing.price, listing.currency))}</b>")
    if listing.location:
        lines.append(f"📍 {esc(truncate(listing.location, 80))}")
    if source_label:
        lines.append(f"🔎 {esc(truncate(source_label, 60))}")
    lines.append(f"🔗 {esc(listing.url)}")
    return "\n".join(lines)[:1024]


def captcha_message(*, source_label: str, variant: str, auto_tried: bool, auto_detail: str,
                    manual_url: str, image_captcha: bool) -> str:
    lines = [
        "🧩 <b>Авито показал капчу</b>",
        f"🔎 Источник: {esc(source_label)}",
        f"Тип: <code>{esc(variant)}</code>",
    ]
    if auto_tried:
        lines.append(f"🤖 Автопопытка (локально): не прошла — {esc(truncate(auto_detail, 120))}")
    else:
        lines.append("🤖 Автопопытка отключена в конфиге")
    lines.append("")
    if image_captcha:
        lines.append("Пришлите текст с картинки командой <code>/answer ТЕКСТ</code> "
                     "(или ответьте на это сообщение словом) — я подставлю его сам.")
    if manual_url:
        lines.append("Или откройте браузер кнопкой ниже и пройдите капчу руками — "
                     "я подхвачу и продолжу автоматически.")
    else:
        lines.append("Локально: запустите с <code>AW_HEADLESS=0</code> и пройдите капчу в окне браузера. "
                     "В Docker: поднимите профиль <code>manual</code> и откройте noVNC (порт 6080).")
    lines.append("После решения нажмите «✅ Я прошёл капчу».")
    return "\n".join(lines)[:1024]


def captcha_keyboard(*, event_id: int, source_id: int | None, manual_url: str) -> dict:
    rows: list[list[dict]] = []
    if manual_url:
        rows.append([{"text": "🖥 Открыть браузер (noVNC)", "url": manual_url}])
    rows.append([{"text": "✅ Я прошёл капчу", "callback_data": f"cap:done:{event_id}"}])
    if source_id is not None:
        rows.append([
            {"text": "⏸ Пауза источника", "callback_data": f"src:pause:{source_id}"},
            {"text": "⏭ Пропустить цикл", "callback_data": f"cap:skip:{event_id}"},
        ])
    return {"inline_keyboard": rows}