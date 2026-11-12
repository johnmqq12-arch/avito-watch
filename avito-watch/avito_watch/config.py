"""Конфигурация: config.yaml + переменные окружения (.env).

Приоритет: значения по умолчанию < config.yaml < переменные окружения.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

ENV_PREFIX = "AW_"


def _env(name: str, default: Any = None) -> Any:
    return os.environ.get(name, default)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "да")


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value).split(",") if p.strip()]


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""                       # куда слать объявления (личный чат/канал/группа)
    admin_ids: list[str] = field(default_factory=list)  # кто может управлять ботом; пусто = chat_id
    enabled: bool = True
    send_photos: bool = True
    description_limit: int = 700            # символов описания в подписи к фото
    full_description: bool = False          # слать полное описание отдельным сообщением
    notify_price_drop: bool = True
    price_drop_min_pct: int = 5
    commands_enabled: bool = True
    silent_hours: str = ""                  # '23:00-08:00' — без звука (не без сообщений)
    request_timeout: float = 40.0


@dataclass
class BrowserConfig:
    engine: str = "browser"                 # browser | http (http — экспериментально)
    headless: bool = True
    user_data_dir: str = "data/profile"     # персистентный профиль: cookies файрвола живут здесь
    proxy: str = ""                         # http://user:pass@host:port — липкая сессия, RU IP
    locale: str = "ru-RU"
    timezone: str = "Europe/Moscow"
    viewport_width: int = 1440
    viewport_height: int = 900
    user_agent: str = ""                    # пусто = родной UA браузера (обычно лучше)
    nav_timeout_sec: float = 45.0
    pow_wait_sec: float = 30.0              # сколько ждём proof-of-work, не перезагружая страницу
    block_resources: bool = True            # не грузить картинки/шрифты/медиа на выдаче
    humanize: bool = True                   # движения мыши, скролл, паузы
    page_delay_sec: float = 3.0             # пауза между страницами одного источника
    restart_after_pages: int = 120          # профилактический перезапуск браузера
    cdp_endpoint: str = ""                  # подключение к внешнему браузеру по CDP (опционально)
    chrome_channel: str = "chromium"        # chromium | chrome (если установлен настоящий Chrome)
    extra_args: list[str] = field(default_factory=list)


@dataclass
class CaptchaConfig:
    auto_solve: bool = True
    max_auto_attempts: int = 2
    notify: bool = True                     # всегда сообщать в Telegram о факте капчи
    notify_cooldown_sec: int = 300          # не спамить чаще, чем раз в N секунд на источник
    manual_wait_sec: int = 1800             # сколько ждём ручного решения, прежде чем уйти в паузу
    poll_interval_sec: float = 5.0
    pause_source_on_captcha: bool = True
    # --- локальный ИИ для капчи-картинки (#inner-captcha) ---
    ocr_backend: str = "auto"               # auto | ddddocr | onnx | none
    onnx_model_path: str = "models/captcha.onnx"
    onnx_charset: str = "0123456789abcdefghijklmnopqrstuvwxyz"
    ocr_variants: int = 3                   # сколько вариантов предобработки пробовать
    save_samples: bool = True               # копить dataset для дообучения своей модели
    # --- слайдер GeeTest v4: локальное CV + человеческая траектория ---
    slider_enabled: bool = True
    slider_max_tries: int = 3
    slider_gap_method: str = "auto"         # auto | template | edge
    # --- ручное решение ---
    manual_url: str = ""                    # ссылка на noVNC, которую бот пришлёт в кнопке
    manual_hint: str = ""                   # свой текст инструкции
    # --- тайминги взаимодействия со страницей, сек ---
    submit_wait_sec: float = 3.0            # ждём вердикт файрвола после отправки ответа
    slider_settle_sec: float = 3.5          # ждём вердикт GeeTest после рывка слайдера
    reload_wait_sec: float = 2.5            # ждём загрузку страницы после reload
    widget_timeout_sec: float = 12.0        # сколько ждём появления виджета капчи
    click_wait_sec: float = 2.0             # пауза после клика «Продолжить» / обновления пазла


@dataclass
class WatchConfig:
    default_interval_sec: int = 300
    min_interval_sec: int = 60              # защита от самобана: ниже — предупреждение
    interval_jitter: float = 0.2
    pages_per_source: int = 1
    max_concurrent_pages: int = 1           # Авито считает запросы по IP: >1 почти всегда хуже
    seed_existing: bool = True              # первый проход — только фиксация, без спама в Telegram
    enrich_items: bool = True               # заходить на страницу объявления за описанием/датой
    enrich_max_per_cycle: int = 10
    enrich_delay_sec: float = 4.0
    max_new_per_cycle: int = 25             # предохранитель от лавины уведомлений
    error_backoff_sec: int = 120
    error_backoff_max_sec: int = 1800
    blocked_backoff_sec: int = 900          # «подождите немного» — долгая пауза, нужен новый IP
    notify_backlog: bool = True             # дослать то, что не успели отправить до перезапуска
    respect_robots: bool = False            # true = отказываться от URL с ?f=/?s= (robots.txt Авито)
    watchdog_sec: int = 60


@dataclass
class StorageConfig:
    db_path: str = "data/avito.db"
    captcha_dir: str = "data/captcha"
    keep_raw_json: bool = True
    vacuum_every_hours: int = 24


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "data/avito-watch.log"
    max_bytes: int = 5_000_000
    backups: int = 3
    json: bool = False


@dataclass
class Config:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    captcha: CaptchaConfig = field(default_factory=CaptchaConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    sources: list[dict[str, Any]] = field(default_factory=list)
    config_path: str = "config.yaml"

    # ---------- загрузка ----------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        cfg = cls()
        path = Path(path or _env("AW_CONFIG", "config.yaml"))
        cfg.config_path = str(path)
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise ValueError(f"{path}: ожидался YAML-словарь")
            cfg._apply_dict(data)
        cfg._apply_env()
        cfg._normalize()
        return cfg

    def _apply_dict(self, data: dict[str, Any]) -> None:
        for f in fields(self):
            if f.name not in data:
                continue
            value = data[f.name]
            current = getattr(self, f.name)
            if is_dataclass(current) and isinstance(value, dict):
                _fill_dataclass(current, value)
            else:
                setattr(self, f.name, value)

    def _apply_env(self) -> None:
        t, b, c, w, s, lg = (self.telegram, self.browser, self.captcha,
                             self.watch, self.storage, self.logging)
        # секреты — только из окружения/.env, в YAML их лучше не хранить
        t.bot_token = _env("AW_TELEGRAM_TOKEN", _env("TELEGRAM_BOT_TOKEN", t.bot_token)) or ""
        t.chat_id = str(_env("AW_CHAT_ID", _env("TELEGRAM_CHAT_ID", t.chat_id)) or "")
        t.admin_ids = _as_list(_env("AW_ADMIN_IDS", t.admin_ids)) or t.admin_ids
        t.enabled = _as_bool(_env("AW_TELEGRAM_ENABLED", t.enabled))
        b.headless = _as_bool(_env("AW_HEADLESS", b.headless))
        b.proxy = _env("AW_PROXY", b.proxy) or ""
        b.engine = _env("AW_ENGINE", b.engine) or b.engine
        b.cdp_endpoint = _env("AW_CDP_ENDPOINT", b.cdp_endpoint) or ""
        b.user_data_dir = _env("AW_PROFILE_DIR", b.user_data_dir) or b.user_data_dir
        c.auto_solve = _as_bool(_env("AW_CAPTCHA_AUTO", c.auto_solve))
        c.ocr_backend = _env("AW_OCR_BACKEND", c.ocr_backend) or c.ocr_backend
        c.manual_url = _env("AW_MANUAL_URL", c.manual_url) or ""
        c.manual_wait_sec = int(_env("AW_CAPTCHA_MANUAL_WAIT", c.manual_wait_sec))
        w.default_interval_sec = int(_env("AW_INTERVAL", w.default_interval_sec))
        w.pages_per_source = int(_env("AW_PAGES", w.pages_per_source))
        w.enrich_items = _as_bool(_env("AW_ENRICH", w.enrich_items))
        w.seed_existing = _as_bool(_env("AW_SEED_EXISTING", w.seed_existing))
        s.db_path = _env("AW_DB_PATH", s.db_path) or s.db_path
        lg.level = _env("AW_LOG_LEVEL", lg.level) or lg.level

    def _normalize(self) -> None:
        t = self.telegram
        if not t.admin_ids and t.chat_id:
            t.admin_ids = [t.chat_id]
        t.admin_ids = [str(x) for x in t.admin_ids]
        self.watch.default_interval_sec = max(15, int(self.watch.default_interval_sec))
        self.watch.pages_per_source = max(1, min(20, int(self.watch.pages_per_source)))
        self.watch.max_concurrent_pages = max(1, int(self.watch.max_concurrent_pages))
        self.captcha.max_auto_attempts = max(0, int(self.captcha.max_auto_attempts))
        for p in (self.storage.db_path, self.logging.file):
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(self.storage.captcha_dir).mkdir(parents=True, exist_ok=True)
        Path(self.browser.user_data_dir).mkdir(parents=True, exist_ok=True)

    # ---------- проверки ----------
    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.telegram.enabled:
            if not self.telegram.bot_token:
                problems.append("Не задан AW_TELEGRAM_TOKEN (токен бота от @BotFather)")
            elif ":" not in self.telegram.bot_token:
                problems.append("AW_TELEGRAM_TOKEN выглядит некорректно (ожидается '123456:ABC...')")
            if not self.telegram.chat_id:
                problems.append("Не задан AW_CHAT_ID (напишите боту /start — он покажет ваш chat_id)")
        if self.watch.default_interval_sec < self.watch.min_interval_sec:
            problems.append(
                f"Интервал {self.watch.default_interval_sec}с меньше рекомендованного "
                f"{self.watch.min_interval_sec}с — Авито начнёт показывать капчу чаще"
            )
        if self.browser.engine == "http":
            problems.append("engine=http: файрвол Авито (proof-of-work) требует JS; "
                            "режим годится только для прогрева сессии браузером")
        return problems

    def to_dict(self) -> dict[str, Any]:
        def conv(obj: Any) -> Any:
            if is_dataclass(obj):
                return {f.name: conv(getattr(obj, f.name)) for f in fields(obj)}
            if isinstance(obj, list):
                return [conv(v) for v in obj]
            return obj
        data = conv(self)
        if data.get("telegram", {}).get("bot_token"):
            data["telegram"]["bot_token"] = "***"
        if data.get("browser", {}).get("proxy"):
            data["browser"]["proxy"] = "***"
        return data


def _fill_dataclass(obj: Any, data: dict[str, Any]) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            continue
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _fill_dataclass(current, value)
        elif known[key].type in ("list[str]", list) or isinstance(current, list):
            setattr(obj, key, _as_list(value))
        elif isinstance(current, bool):
            setattr(obj, key, _as_bool(value))
        elif isinstance(current, int) and not isinstance(current, bool):
            setattr(obj, key, int(value))
        elif isinstance(current, float):
            setattr(obj, key, float(value))
        else:
            setattr(obj, key, value)


def load_dotenv(path: str | Path = ".env") -> None:
    """Мини-загрузчик .env без внешних зависимостей (не перетирает уже заданные переменные)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)