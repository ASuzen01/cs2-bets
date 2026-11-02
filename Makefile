.PHONY: install run miniapp test smoke integration lint check

install:
	python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

run:
	.venv/bin/python bot.py

miniapp:
	.venv/bin/python miniapp.py

test:
	.venv/bin/python -m pytest -q

smoke:
	.venv/bin/python scripts/smoke_check.py

integration:
	.venv/bin/python scripts/integration_check.py

lint:
	.venv/bin/python -m ruff check app bot.py miniapp.py scripts tests
	.venv/bin/python -m ruff format --check app bot.py miniapp.py scripts tests || true

check: lint test smoke integration
