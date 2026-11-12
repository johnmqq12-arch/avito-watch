.PHONY: help install browsers run once doctor test warmup add docker-build docker-up docker-down docker-logs

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## создать venv и поставить зависимости
	python3 -m venv .venv
	.venv/bin/pip install -U pip
	.venv/bin/pip install -r requirements.txt -r requirements-ocr.txt -r requirements-dev.txt
	@[ -f .env ] || cp .env.example .env
	@[ -f config.yaml ] || cp config.example.yaml config.yaml

browsers:  ## скачать Chromium для Playwright
	.venv/bin/playwright install chromium

run:  ## запустить мониторинг
	.venv/bin/python -m avito_watch run

once:  ## один проход и выход
	.venv/bin/python -m avito_watch once

doctor:  ## диагностика
	.venv/bin/python -m avito_watch doctor

test:  ## тесты (без сети и браузера)
	.venv/bin/python -m pytest -q

warmup:  ## пройти капчу вручную и сохранить профиль
	.venv/bin/python -m avito_watch warmup

docker-build:  ## собрать образ
	docker compose build

docker-up:  ## запустить в Docker
	docker compose up -d

docker-down:  ## остановить
	docker compose down

docker-logs:  ## логи
	docker compose logs -f --tail=100
