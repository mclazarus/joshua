.PHONY: test lint fmt dev-up dev-logs dev-down dev-health dev-reset

UID := $(shell id -u)
GID := $(shell id -g)
export UID GID

test:
	uv run pytest -q

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

# Local sandbox run in Docker (reads .env, keeps state in ./data)
dev-up:
	@test -f .env || (echo "copy .env.example to .env and fill it in first" && exit 1)
	mkdir -p data
	docker compose -f compose.dev.yaml up -d --build
	@echo "logs: make dev-logs"

dev-logs:
	docker compose -f compose.dev.yaml logs -f --tail 200

dev-down:
	docker compose -f compose.dev.yaml down

dev-health:
	docker exec joshua-dev joshua healthcheck

# Wipes the sandbox DB (registrations, keys, reminder history).
dev-reset: dev-down
	rm -rf data
