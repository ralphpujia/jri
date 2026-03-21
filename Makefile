PYTHON := python3
VENV := .venv
VENV_BIN := $(VENV)/bin
PIP := $(VENV_BIN)/pip
PYTEST := $(VENV_BIN)/pytest
UVICORN := $(VENV_BIN)/uvicorn

.PHONY: setup install run test clean

setup: $(PIP)
	$(PIP) install -r requirements.txt

install: setup

$(PIP):
	$(PYTHON) -m venv $(VENV)

run: $(UVICORN)
	$(UVICORN) app.main:app --reload --host 127.0.0.1 --port 8000

test: $(PYTEST)
	$(PYTEST)

clean:
	rm -rf $(VENV) .pytest_cache
