# Common course tasks.  `make help` lists them.
PY ?= python
VENV ?= .venv

.PHONY: help setup data data-advanced test test-day test-advanced lab smoke-live smoke-live-advanced docker-build docker-test docker-shell clean

help:
	@echo "setup         create .venv and install everything"
	@echo "data          regenerate the dataset (deterministic)"
	@echo "data-advanced regenerate the advanced course's dataset (deterministic)"
	@echo "test          run all tests (mock mode, no API key needed)"
	@echo "test-day D=2  run one day's labs and solutions"
	@echo "test-advanced run the advanced course's tests and labs only"
	@echo "lab F=day1_foundations/labs/01_first_call.py   run one lab"
	@echo "smoke-live    run a representative subset against the real API (needs ANTHROPIC_API_KEY)"
	@echo "smoke-live-advanced  same, for the advanced course"
	@echo "docker-build / docker-test / docker-shell"

setup:
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements.txt

data:
	$(PY) data/generate_data.py

data-advanced:
	$(PY) advanced/data/generate_data.py

test:
	LABKIT_MODE=mock $(PY) -m pytest -q

test-day:
	LABKIT_MODE=mock $(PY) -m pytest tests/test_labs.py -q -k "day$(D)"

test-advanced:
	LABKIT_MODE=mock $(PY) -m pytest tests/ -q -k "advanced"

lab:
	$(PY) $(F)

smoke-live:
	$(PY) scripts/smoke_live.py

smoke-live-advanced:
	$(PY) scripts/smoke_live.py --only advanced --cap 5

docker-build:
	docker build -t claude-agent-course .

docker-test: docker-build
	docker run --rm claude-agent-course

docker-shell: docker-build
	docker run --rm -it --env-file .env -v $(PWD):/course claude-agent-course bash

clean:
	rm -rf .runs .pytest_cache
