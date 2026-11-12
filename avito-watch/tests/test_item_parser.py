"""Страница объявления: разбор и из JSON-состояния, и из «голого» DOM."""
from avito_watch.parser.item import parse_item
from avito_watch.utils import MSK

ITEM_URL = "https://www.avito.ru/moskva/telefony/iphone_15_pro_7654321098?context=abc"

DOM_ITEM = """<!DOCTYPE html><html lang="ru"><head>
<title>iPhone 15 Pro 256 ГБ — Авито</title>
<meta property="og:url" content="https://www.avito.ru/moskva/telefony/iphone_15_pro_7654321098">
<meta property="og:image" content="https://00.img.avito.st/image/1/cover.jpg">
</head><body>
<div data-marker="item-view/title-info"><h1>iPhone 15 Pro 256 ГБ</h1></div>
<span data-marker="item-view/item-price" content="62990">62 990 ₽</span>
<div data-marker="item-view/item-description"><p>Продаю iPhone 15 Pro.</p><p>Полный комплект.</p></div>
<div data-marker="item-view/item-date">сегодня в 12:30</div>
<span data-marker="item-view/item-address">Москва, Центральный</span>
<div data-marker="image-frame/image"><img src="https://00.img.avito.st/image/1/a.jpg"></div>
<div data-marker="image-frame/image"><img data-src="https://00.img.avito.st/image/1/b.jpg"></div>
</body></html>"""


def test_dom_only_item_is_parsed():
    item = parse_item(DOM_ITEM, url=ITEM_URL)
    assert item is not None
    assert item.item_id == "7654321098"
    assert item.title == "iPhone 15 Pro 256 ГБ"
    assert item.price == 62990
    assert item.price_source == "dom"
    assert "Полный комплект" in item.description
    assert item.location == "Москва, Центральный"
    assert item.published_at is not None
    assert item.image_url == "https://00.img.avito.st/image/1/a.jpg"
    assert len(item.images) == 2
    assert "?" not in item.url                      # трекинг вырезан


def test_item_id_from_og_url_when_no_url_given():
    item = parse_item(DOM_ITEM)
    assert item is not None and item.item_id == "7654321098"


def test_item_id_from_data_attribute():
    html = '<html><body><div data-item-id="1234567890"></div><h1>Товар</h1></body></html>'
    item = parse_item(html)
    assert item is not None and item.item_id == "1234567890"
    assert item.title == "Товар"


def test_no_id_no_item():
    assert parse_item("<html><body><h1>Просто страница</h1></body></html>") is None
    assert parse_item("") is None


def test_state_wins_over_dom():
    """Цена покупателя берётся из состояния, DOM её не перетирает."""
    from tests.conftest import load_html
    item = parse_item(load_html("item"), url="https://www.avito.ru/moskva/x_7654321098")
    assert item is not None
    assert item.price_source in ("item", "state")
    assert item.price and item.price > 0
    assert item.description
    assert item.images


def test_published_today_is_in_msk():
    from datetime import datetime
    item = parse_item(DOM_ITEM, url=ITEM_URL)
    now = datetime.now(MSK)
    assert item.published_at.date() == now.date()
    assert item.published_at.hour == 12 and item.published_at.minute == 30
