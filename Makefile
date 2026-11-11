.PHONY: help install test lint demo eval serve worker docker-build docker-up docker-down clean

PY ?= .venv/bin/python

help:
	@echo "install      create the venv and install locked dependencies"
	@echo "test         run the test suite"
	@echo "lint         byte-compile the package"
	@echo "demo         run the Lovesac demonstration deal"
	@echo "eval         score the latest memoranda against quality gates"
	@echo "serve        start the API on 127.0.0.1:8000"
	@echo "worker       start a standalone durable worker"
	@echo "docker-build build the production image"
	@echo "docker-up    run API and worker with docker compose"

install:
	uv venv --python 3.13 .venv
	UV_LINK_MODE=copy uv pip install --python $(PY) -r requirements.lock

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m compileall -q dossier tests

demo:
	$(PY) -m dossier.cli demo

eval:
	$(PY) -m dossier.cli eval

serve:
	$(PY) -m dossier.cli serve --host 127.0.0.1 --port 8000

worker:
	$(PY) -m dossier.cli worker

docker-build:
	docker build -t dossier:local .

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

clean:
	rm -rf __pycache__ dossier/__pycache__ dossier/pipeline/__pycache__ dossier/sources/__pycache__ .pytest_cache
