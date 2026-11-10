.PHONY: up down check test chaos load

up:
	docker compose up --build

down:
	docker compose down

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src

test:
	uv run pytest -q

chaos:
	uv run python scripts/chaos.py --jobs 10000 --workers 8

load:
	uv run python scripts/chaos.py --mode load --rps 100 --seconds 60 --workers 8
