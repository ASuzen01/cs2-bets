.PHONY: install run miniapp test smoke integration check

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

check: test smoke integration
