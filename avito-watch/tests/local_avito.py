"""Локальный «мини-Авито» для интеграционных тестов на настоящем Chromium.

Никаких запросов в интернет: поднимается HTTP-сервер на 127.0.0.1, который
повторяет поведение файрвола Авито:
  * /pow      — страница proof-of-work: JS сам ставит cookie и перезагружается
                (движок обязан ЖДАТЬ, а не перезагружать страницу руками);
  * /captcha  — страница с капчей-картинкой: ответ уходит fetch'ем, сервер
                ставит cookie и после reload отдаёт выдачу;
  * /blocked  — «подождите немного и обновите страницу» (нужен другой IP);
  * /listing  — выдача с карточками (состав меняется между проходами);
  * /moskva/tovar_<id> — страница объявления (для обогащения).

Сервер считает запросы, чтобы тест мог проверить «сколько раз ходили».
"""
from __future__ import annotations

import base64
import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).parent / "fixtures"

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


def find_font() -> str | None:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def make_captcha_png(text: str, *, noise: bool = True, size=(180, 64)) -> bytes:
    """Капча-картинка в стиле Авито: буквы+цифры, наклонные линии шума."""
    from PIL import Image, ImageDraw, ImageFont

    font_path = find_font()
    if font_path is None:                       # без шрифта тест пропускается
        raise RuntimeError("нет TTF-шрифта для генерации капчи")
    img = Image.new("RGB", size, (250, 250, 250))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, 38)
    step = (size[0] - 20) // max(1, len(text))
    for i, ch in enumerate(text):
        draw.text((10 + i * step, 8 + (i % 2) * 5), ch, font=font, fill=(25 + i * 8, 40, 95))
    if noise:
        for x in range(-20, size[0], 9):
            draw.line([(x, 0), (x + 24, size[1])], fill=(170, 175, 200), width=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def card_html(item_id: str, title: str, price: int, base_url: str) -> str:
    """Карточка выдачи. Ссылка абсолютная и локальная — чтобы обогащение не ушло в интернет."""
    return f"""<div data-marker="item" data-item-id="{item_id}">
      <a itemprop="url" data-marker="item-title" href="{base_url}/moskva/tovar_{item_id}">
        <h3 itemprop="name">{title}</h3></a>
      <img itemprop="image" src="https://00.img.avito.st/image/1/{item_id}.jpg">
      <div data-marker="item-price"><span itemprop="price" content="{price}">{price} ₽</span></div>
      <span data-marker="item-location">Москва</span>
      <span data-marker="item-date">10 минут назад</span>
    </div>"""


def listing_html(cards: list[str]) -> str:
    return ('<!DOCTYPE html><html lang="ru"><head><title>Авито — объявления</title></head><body>'
            f'<div data-marker="catalog">{"".join(cards)}</div></body></html>')


_POW_HTML = """<!DOCTYPE html><html lang="ru"><head><title>Доступ ограничен</title></head><body>
<h1>Доступ ограничен</h1><p>Проверяем ваш браузер…</p>
<script>
  // имитация proof-of-work: считаем «задачу» и только потом перезагружаемся
  setTimeout(() => {
    fetch('/verify').then(() => {
      document.cookie = 'fw_pow=1; path=/';
      setTimeout(() => location.reload(), 300);
    });
  }, 1200);
</script>
</body></html>"""

_CAPTCHA_TMPL = """<!DOCTYPE html><html lang="ru"><head><title>Доступ ограничен</title></head><body>
<form class="js-firewall-form" method="post">
  <input type="hidden" name="captcha-response" value="">
  <div id="geetest_captcha" style="display: none"></div>
  <div id="inner-captcha">
    <img alt="captcha" src="data:image/png;base64,{image}">
    <input id="form-input" name="answer" autocomplete="off">
  </div>
  <p id="err"></p>
  <button type="submit">Продолжить</button>
</form>
<script>
  document.querySelector('form.js-firewall-form').addEventListener('submit', function (e) {{
    e.preventDefault();
    var value = document.querySelector('#form-input').value;
    fetch('/check?answer=' + encodeURIComponent(value))
      .then(r => r.json())
      .then(function (j) {{
        if (j.ok) {{
          document.cookie = 'fw_ok=1; path=/';
          location.reload();
        }} else {{
          document.querySelector('#err').textContent = 'Неверный ответ';
        }}
      }});
  }});
</script>
</body></html>"""

_ITEM_TMPL = """<!DOCTYPE html><html lang="ru"><head><title>{title} — Авито</title></head><body>
<div data-marker="item-view/title-info"><h1>{title}</h1></div>
<span data-marker="item-view/item-price" content="{price}">{price} ₽</span>
<div data-marker="item-view/item-description"><p>Продаю {title}. Состояние отличное, полный комплект.</p></div>
<div data-marker="item-view/item-date">сегодня в 12:30</div>
<span data-marker="item-view/item-address">Москва, Центральный</span>
<img data-marker="image-frame/image" src="https://00.img.avito.st/image/1/{item_id}_big.jpg">
</body></html>"""


class LocalAvito:
    """Управление состоянием «мини-Авито» из теста."""

    def __init__(self) -> None:
        self.items: dict[str, tuple[str, int]] = {}
        self.captcha_answer: str = "ab12cd"
        self.captcha_noise: bool = True
        self.hits: dict[str, int] = {}
        self.answers_tried: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> str:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):        # тишина в логах pytest
                pass

            def _count(self, key: str) -> None:
                server.hits[key] = server.hits.get(key, 0) + 1

            def _send(self, body: str, status: int = 200, content_type: str = "text/html; charset=utf-8"):
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _cookie(self, name: str) -> str:
                raw = self.headers.get("Cookie") or ""
                for part in raw.split(";"):
                    key, _, value = part.strip().partition("=")
                    if key == name:
                        return value
                return ""

            def do_GET(self):                  # noqa: N802 (имя из stdlib)
                parsed = urlparse(self.path)
                path, query = parsed.path, parse_qs(parsed.query)
                self._count(path)

                if path == "/verify":
                    return self._send('{"ok":true}', content_type="application/json")

                if path == "/check":
                    answer = (query.get("answer") or [""])[0]
                    server.answers_tried.append(answer)
                    ok = answer.strip().lower() == server.captcha_answer.lower()
                    return self._send('{"ok":%s}' % ("true" if ok else "false"),
                                      content_type="application/json")

                if path == "/blocked":
                    return self._send((FIXTURES / "firewall_blocked.html").read_text(encoding="utf-8"))

                if path == "/empty":
                    return self._send((FIXTURES / "empty_search.html").read_text(encoding="utf-8"))

                if path.startswith("/moskva/tovar_"):
                    item_id = path.rsplit("_", 1)[-1]
                    title, price = server.items.get(item_id, ("Объявление", 1000))
                    return self._send(_ITEM_TMPL.format(title=title, price=price, item_id=item_id))

                if path == "/pow" and self._cookie("fw_pow") != "1":
                    return self._send(_POW_HTML)

                if path == "/captcha" and self._cookie("fw_ok") != "1":
                    image = make_captcha_png(server.captcha_answer, noise=server.captcha_noise)
                    return self._send(_CAPTCHA_TMPL.format(
                        image=base64.b64encode(image).decode("ascii")))

                cards = [card_html(i, t, p, server.base_url) for i, (t, p) in server.items.items()]
                return self._send(listing_html(cards))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ---------------------------------------------------------------- helpers
    @property
    def base_url(self) -> str:
        assert self._server is not None, "сервер не запущен"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def url(self, path: str) -> str:
        return self.base_url + path

    def set_items(self, items: dict[str, tuple[str, int]]) -> None:
        self.items = dict(items)

    def add_item(self, item_id: str, title: str, price: int) -> None:
        self.items[item_id] = (title, price)
