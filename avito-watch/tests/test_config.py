"""Конфигурация: YAML, переменные окружения, валидация, .env."""
import textwrap

from avito_watch.config import Config, load_dotenv


def test_defaults():
    cfg = Config()
    assert cfg.watch.default_interval_sec == 300
    assert cfg.browser.engine == "browser"
    assert cfg.captcha.auto_solve is True


def test_yaml_loading(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent("""
        telegram:
          description_limit: 300
          send_photos: false
        watch:
          default_interval_sec: 120
          pages_per_source: 3
        captcha:
          slider_max_tries: 5
          ocr_backend: none
        sources:
          - url: "https://www.avito.ru/moskva?q=iphone"
            name: iPhone
            interval_sec: 90
    """), encoding="utf-8")
    cfg = Config.load(path)
    assert cfg.telegram.description_limit == 300
    assert cfg.telegram.send_photos is False
    assert cfg.watch.default_interval_sec == 120
    assert cfg.watch.pages_per_source == 3
    assert cfg.captcha.slider_max_tries == 5
    assert cfg.sources[0]["interval_sec"] == 90


def test_env_overrides_yaml(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("watch:\n  default_interval_sec: 120\n", encoding="utf-8")
    monkeypatch.setenv("AW_INTERVAL", "600")
    monkeypatch.setenv("AW_TELEGRAM_TOKEN", "999:ABC")
    monkeypatch.setenv("AW_CHAT_ID", "42")
    monkeypatch.setenv("AW_HEADLESS", "0")
    monkeypatch.setenv("AW_ADMIN_IDS", "42, 43")
    cfg = Config.load(path)
    assert cfg.watch.default_interval_sec == 600
    assert cfg.telegram.bot_token == "999:ABC"
    assert cfg.telegram.chat_id == "42"
    assert cfg.browser.headless is False
    assert cfg.telegram.admin_ids == ["42", "43"]


def test_validation_reports_missing_secrets(tmp_path):
    cfg = Config.load(tmp_path / "missing.yaml")
    problems = cfg.validate()
    assert any("AW_TELEGRAM_TOKEN" in p for p in problems)
    assert any("AW_CHAT_ID" in p for p in problems)


def test_validation_warns_about_aggressive_interval(tmp_path):
    cfg = Config.load(tmp_path / "missing.yaml")
    cfg.telegram.bot_token = "1:ABC"
    cfg.telegram.chat_id = "1"
    cfg.watch.default_interval_sec = 20
    assert any("капчу" in p for p in cfg.validate())


def test_secrets_are_masked(tmp_path):
    cfg = Config.load(tmp_path / "missing.yaml")
    cfg.telegram.bot_token = "123:SECRET"
    cfg.browser.proxy = "http://user:pass@host:1234"
    data = cfg.to_dict()
    assert data["telegram"]["bot_token"] == "***"
    assert data["browser"]["proxy"] == "***"


def test_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# комментарий\nAW_TEST_VALUE=hello\nexport AW_TEST_QUOTED=\"world\"\n", encoding="utf-8")
    monkeypatch.delenv("AW_TEST_VALUE", raising=False)
    monkeypatch.delenv("AW_TEST_QUOTED", raising=False)
    load_dotenv(env)
    import os
    assert os.environ["AW_TEST_VALUE"] == "hello"
    assert os.environ["AW_TEST_QUOTED"] == "world"


def test_dotenv_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AW_TEST_VALUE=from_file\n", encoding="utf-8")
    monkeypatch.setenv("AW_TEST_VALUE", "from_env")
    load_dotenv(env)
    import os
    assert os.environ["AW_TEST_VALUE"] == "from_env"