"""Генератор тестовых HTML-фикстур.

Структура повторяет то, что реально отдаёт Авито: JSON-состояние выдачи
(data-mfe-state), микроразметка schema.org, hydration-данные страницы объявления
и три состояния файрвола. Запуск: python scripts/make_fixtures.py
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
FIXTURES.mkdir(parents=True, exist_ok=True)

IMG = "https://00.img.avito.st/image/1/{name}.jpg"


def card(item_id: str, title: str, price: int | None, path: str, image: str,
         date: str = "2 часа назад", location: str = "Москва") -> str:
    price_html = (f'<span itemprop="price" content="{price}">{price:,} ₽</span>'.replace(",", " ")
                  if price is not None else '<span itemprop="price">Цена не указана</span>')
    return f"""
<div data-marker="item" data-item-id="{item_id}" class="iva-item-root">
  <a itemprop="url" data-marker="item-title" href="{path}"><h3 itemprop="name">{title}</h3></a>
  <div data-marker="item-photo"><img itemprop="image" src="{image}" alt="{title}"></div>
  <div data-marker="item-price">{price_html}</div>
  <span data-marker="item-location">{location}</span>
  <span data-marker="item-specific-params">256 ГБ, чёрный</span>
  <span data-marker="item-date">{date}</span>
</div>"""


STATE_ITEMS = [
    {
        "id": 7654321098,
        "title": "iPhone 15 Pro 256 ГБ",
        "urlPath": "/moskva/telefony/iphone_15_pro_256_gb_7654321098",
        # ловушка: цена ДО скидки
        "priceDetailed": {"value": 69989, "priceString": "69 989 ₽"},
        "location": "Москва, м. Авиамоторная",
        "seller": {"name": "ТехноМаркет", "type": "company"},
        "iva": {
            "PriceStep": [
                {"componentData": {"component": "price"},
                 "payload": {"priceDetailed": {"value": 62990, "priceString": "62 990 ₽"}}},
                {"componentData": {"component": "discount"},
                 "payload": {"old_string": "69 989 ₽", "percent": 10}},
            ],
            "DateInfoStep": [
                {"componentData": {"component": "date"}, "payload": {"text": "2 часа назад"}}
            ],
            "PhotoStep": [
                {"componentData": {"component": "photo"},
                 "payload": {"images": [{"1280x960": IMG.format(name="iphone1"),
                                         "432x324": IMG.format(name="iphone1_small")}]}}
            ],
        },
    },
    {
        "id": 7654321099,
        "title": "iPhone 15 128 ГБ, как новый",
        "urlPath": "/moskva/telefony/iphone_15_128_gb_7654321099",
        "priceDetailed": {"value": 48000, "priceString": "48 000 ₽"},
        "location": "Москва, м. Сокол",
        "publishedAt": 1762941600,
        "images": [{"1280x960": IMG.format(name="iphone2")}],
        "seller": {"type": "private"},
    },
    {
        "id": 7654321100,
        "title": "Отдам коробку от iPhone",
        "urlPath": "/moskva/telefony/korobka_ot_iphone_7654321100",
        "priceDetailed": {"value": 0, "priceString": "Бесплатно"},
        "location": "Москва",
        "iva": {
            "DateInfoStep": [
                {"componentData": {"component": "date"}, "payload": {"text": "вчера в 19:05"}}
            ]
        },
    },
]


def listing_state() -> str:
    state = {"loaderData": {"data": {"catalog": {"items": STATE_ITEMS}}}}
    cards = [
        card("7654321098", "iPhone 15 Pro 256 ГБ", 62990,
             "/moskva/telefony/iphone_15_pro_256_gb_7654321098", IMG.format(name="iphone1")),
        card("7654321099", "iPhone 15 128 ГБ, как новый", 48000,
             "/moskva/telefony/iphone_15_128_gb_7654321099", IMG.format(name="iphone2")),
        card("7654321100", "Отдам коробку от iPhone", None,
             "/moskva/telefony/korobka_ot_iphone_7654321100", IMG.format(name="box"), date="вчера в 19:05"),
        # карточка, которой нет в JSON — должна быть добрана из DOM
        card("7654321101", "iPhone 15 Plus 256 ГБ", 55500,
             "/moskva/telefony/iphone_15_plus_7654321101", IMG.format(name="iphone3"), date="15 минут назад"),
    ]
    carousel = f"""
<div data-marker="itemsCarousel">
  <h2>Подобрали для вас</h2>
  {card("9999999901", "Рекомендация: чехол", 990, "/moskva/chehol_9999999901", IMG.format(name="case"))}
</div>"""
    similar = f"""
<h2>Похожие объявления рядом и в других городах</h2>
{card("9999999902", "Похожее: iPhone 14", 39000, "/tula/iphone_14_9999999902", IMG.format(name="iphone4"))}
{card("9999999903", "Похожее: iPhone 13", 29000, "/tula/iphone_13_9999999903", IMG.format(name="iphone5"))}"""
    return f"""<!DOCTYPE html><html lang="ru"><head><title>iPhone — Авито</title></head><body>
<div data-marker="catalog">
{''.join(cards)}
{carousel}
</div>
{similar}
<script type="mime/invalid" data-mfe-state>{json.dumps(state, ensure_ascii=False)}</script>
</body></html>"""


def listing_dom() -> str:
    cards = [
        card("7000000001", "Велосипед Trek Marlin 7", 62000,
             "/sankt-peterburg/velosipedy/trek_marlin_7_7000000001", IMG.format(name="trek1"),
             date="сегодня в 09:15", location="Санкт-Петербург"),
        card("7000000002", "Trek X-Caliber 8", 95000,
             "/sankt-peterburg/velosipedy/trek_xcaliber_7000000002", IMG.format(name="trek2"),
             date="3 дня назад", location="Санкт-Петербург"),
        card("7000000003", "Рама Trek (запчасти)", 12000,
             "/sankt-peterburg/velosipedy/rama_trek_7000000003", IMG.format(name="trek3"),
             date="неделю назад", location="Санкт-Петербург"),
    ]
    carousel = f"""
<div data-marker="itemsCarousel">
  {card("9999999911", "Рекомендация: насос", 1500, "/spb/nasos_9999999911", IMG.format(name="pump"))}
</div>"""
    return f"""<!DOCTYPE html><html lang="ru"><head><title>Велосипеды Trek — Авито</title></head><body>
<div data-marker="search-core">{''.join(cards)}{carousel}</div>
</body></html>"""


def item_page() -> str:
    buyer_item = {
        "id": 7654321098,
        "title": "iPhone 15 Pro 256 ГБ",
        "description": "Продаю iPhone 15 Pro 256 ГБ.\nСостояние отличное, полный комплект.\nТорг уместен.",
        "price": 69989,
        "formattedPrice": {"value": "62 990 ₽"},
        "time": 1762941600,
        "address": "Москва, Авиамоторная улица, 10",
        "images": [{"1280x960": IMG.format(name="iphone1")},
                   {"1280x960": IMG.format(name="iphone1_back")}],
        "params": [{"name": "Бренд", "value": "Apple"}, {"name": "Память", "value": "256 ГБ"}],
        "seller": {"name": "ТехноМаркет", "type": "company"},
    }
    hydration = {"loaderData": {"item-page": {"buyerItem": buyer_item}}}
    literal = json.dumps(json.dumps(hydration, ensure_ascii=False), ensure_ascii=False)
    return f"""<!DOCTYPE html><html lang="ru"><head><title>iPhone 15 Pro 256 ГБ — Авито</title></head><body>
<h1 data-marker="item-view/title-info">iPhone 15 Pro 256 ГБ</h1>
<div data-marker="item-view/item-date">12 ноября в 14:00</div>
<div data-marker="item-view/item-description">Продаю iPhone 15 Pro 256 ГБ.</div>
<script>window.__staticRouterHydrationData = JSON.parse({literal});</script>
</body></html>"""


def firewall_captcha() -> str:
    return """<!DOCTYPE html><html lang="ru"><head><title>Доступ ограничен: проблема с IP</title></head><body>
<h1>Доступ ограничен</h1>
<p>С вашего IP-адреса поступает очень много запросов.</p>
<form class="form js-firewall-form" method="post">
  <input type="hidden" name="captcha-response" value="">
  <div id="geetest_captcha"><div class="geetest_widget">
     <canvas class="geetest_bg"></canvas><canvas class="geetest_slice_bg"></canvas>
     <div class="geetest_slider_button"></div></div></div>
  <div id="inner-captcha" style="display: none"><img src="data:image/png;base64,AAAA"><input id="form-input"></div>
  <div id="h-captcha" style="display: none" data-sitekey="10000000-ffff-ffff-ffff-000000000001"></div>
  <button type="submit">Продолжить</button>
</form>
<script>
  var captchaId = 'e1b6a1c2d3e4f5061728394a5b6c7d8e';
  document.querySelector('.js-firewall-form');
</script>
</body></html>"""


def firewall_image() -> str:
    # маленькая настоящая PNG-картинка (заглушка капчи) в data: URL
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAYAAACNMs+9AAAAFUlEQVR42mP8z8BQz0AEYBxVSF+FAP5FDvcfRYWgAAAAAElFTkSuQmCC")
    encoded = base64.b64encode(png).decode()
    return f"""<!DOCTYPE html><html lang="ru"><head><title>Доступ ограничен</title></head><body>
<form class="js-firewall-form" method="post">
  <input type="hidden" name="captcha-response" value="">
  <div id="geetest_captcha" style="display: none"></div>
  <div id="inner-captcha"><img src="data:image/png;base64,{encoded}"><input id="form-input" name="answer"></div>
  <button type="submit">Продолжить</button>
</form>
</body></html>"""


def firewall_blocked() -> str:
    return """<!DOCTYPE html><html lang="ru"><head><title>Доступ ограничен: проблема с IP</title></head><body>
<h1>Доступ ограничен</h1>
<p>Подождите немного и обновите страницу.</p>
</body></html>"""


def firewall_pow() -> str:
    return """<!DOCTYPE html><html lang="ru"><head><title>Доступ ограничен</title></head><body>
<h1>Доступ ограничен</h1>
<p>Проверяем ваш браузер…</p>
<script>fetch('/web/3/firewallPow/verify').then(() => location.reload());</script>
</body></html>"""


def empty_search() -> str:
    return """<!DOCTYPE html><html lang="ru"><head><title>Объявления — Авито</title></head><body>
<div data-marker="catalog"><h1>Ничего не найдено</h1>
<p>Попробуйте изменить параметры поиска</p></div>
</body></html>"""


def main() -> None:
    files = {
        "listing_state.html": listing_state(),
        "listing_dom.html": listing_dom(),
        "item.html": item_page(),
        "firewall_captcha.html": firewall_captcha(),
        "firewall_image.html": firewall_image(),
        "firewall_blocked.html": firewall_blocked(),
        "firewall_pow.html": firewall_pow(),
        "empty_search.html": empty_search(),
    }
    for name, content in files.items():
        (FIXTURES / name).write_text(content, encoding="utf-8")
        print(f"✓ {name} ({len(content)} байт)")


if __name__ == "__main__":
    main()