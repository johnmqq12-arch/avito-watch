"""Состояния файрвола и разбор выдачи/объявления."""
from avito_watch.captcha.detect import detect_variant, extract_captcha_id, extract_inline_image
from avito_watch.models import CaptchaVariant, PageState
from avito_watch.parser.item import parse_item
from avito_watch.parser.listing import next_page_url, parse_listing
from avito_watch.parser.state import detect_state, has_firewall_form, page_title
from tests.conftest import load_html, make_card, make_listing_page


def test_states():
    assert detect_state(load_html("listing_state")) is PageState.CONTENT
    assert detect_state(load_html("listing_dom")) is PageState.CONTENT
    assert detect_state(load_html("item"), is_item_page=True) is PageState.CONTENT
    assert detect_state(load_html("firewall_captcha")) is PageState.CAPTCHA
    assert detect_state(load_html("firewall_image")) is PageState.CAPTCHA
    assert detect_state(load_html("firewall_blocked")) is PageState.BLOCKED
    assert detect_state(load_html("firewall_pow")) is PageState.CHALLENGE
    assert detect_state(load_html("empty_search")) is PageState.EMPTY


def test_form_is_element_not_substring():
    # скрипт упоминает .js-firewall-form, но формы нет → это не капча
    html = "<html><head><title>Доступ ограничен</title></head><body>" \
           "<script>document.querySelector('.js-firewall-form');</script></body></html>"
    assert has_firewall_form(html) is False
    assert detect_state(html) is PageState.CHALLENGE


def test_title_extraction():
    assert "Доступ ограничен" in page_title(load_html("firewall_captcha"))


def test_listing_from_state_and_dom():
    listings = parse_listing(load_html("listing_state"), source_id=1)
    ids = [l.item_id for l in listings]
    assert ids == ["7654321098", "7654321099", "7654321100", "7654321101"]
    # карусель «Подобрали для вас» и «Похожие объявления» не результаты поиска
    assert "9999999901" not in ids and "9999999902" not in ids


def test_discount_price_trap():
    listing = parse_listing(load_html("listing_state"))[0]
    # priceDetailed верхнего уровня = 69989 (цена ДО скидки), показанная = 62990
    assert listing.price == 62990
    assert listing.old_price == 69989
    assert listing.discount_pct == 10
    assert listing.price_source == "state"


def test_listing_fields():
    listing = parse_listing(load_html("listing_state"))[0]
    assert listing.title == "iPhone 15 Pro 256 ГБ"
    assert listing.url.startswith("https://www.avito.ru/moskva/telefony/")
    assert listing.image_url.endswith("iphone1.jpg")
    assert listing.published_at is not None
    assert listing.location and "Москва" in listing.location
    assert listing.seller_name == "ТехноМаркет"


def test_dom_only_listing():
    listings = parse_listing(load_html("listing_dom"))
    assert len(listings) == 3
    assert all(l.price_source == "dom" for l in listings)
    assert listings[0].price == 62000
    assert listings[0].published_at is not None
    assert listings[0].image_url.endswith("trek1.jpg")


def test_free_and_missing_price():
    listings = {l.item_id: l for l in parse_listing(load_html("listing_state"))}
    assert listings["7654321100"].price == 0            # «Бесплатно»
    assert listings["7654321100"].image_url.endswith("box.jpg")   # фото добрано из DOM


def test_empty_search_is_not_error():
    assert parse_listing(load_html("empty_search")) == []


def test_pagination_url():
    assert next_page_url("https://www.avito.ru/moskva?q=iphone", 2) == \
        "https://www.avito.ru/moskva?q=iphone&p=2"
    assert next_page_url("https://www.avito.ru/moskva?q=iphone&p=2", 3) == \
        "https://www.avito.ru/moskva?q=iphone&p=3"


def test_item_page():
    item = parse_item(load_html("item"), url="https://www.avito.ru/moskva/telefony/x_7654321098")
    assert item is not None
    assert item.item_id == "7654321098"
    assert item.price == 62990                     # formattedPrice, а не item.price=69989
    assert "полный комплект" in (item.description or "")
    assert len(item.images) == 2
    assert item.published_at is not None
    assert item.location and "Авиамоторная" in item.location
    assert item.params and "Apple" in item.params


def test_captcha_detection():
    html = load_html("firewall_captcha")
    assert detect_variant(html) is CaptchaVariant.GEETEST
    assert extract_captcha_id(html) == "e1b6a1c2d3e4f5061728394a5b6c7d8e"
    image_html = load_html("firewall_image")
    assert detect_variant(image_html) is CaptchaVariant.IMAGE
    assert extract_inline_image(image_html) is not None


def test_hidden_captcha_blocks_ignored():
    html = load_html("firewall_image")   # geetest скрыт, картинка видна
    assert detect_variant(html) is CaptchaVariant.IMAGE


def test_dedup_within_page():
    card = make_card("1234567890", "Повторяющееся объявление", 1000)
    page = make_listing_page([card, card])
    assert len(parse_listing(page)) == 1