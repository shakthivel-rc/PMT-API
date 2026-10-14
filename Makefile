# Base Project — common tasks.
#
# `make` on its own prints this list. The one you want on a fresh clone is `make setup`.

APP_DIR ?= $(shell if [ -d ../pmt-app ]; then echo ../pmt-app; elif [ -d ../PMT-APP ]; then echo ../PMT-APP; else echo ../pmt-app; fi)
COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help setup setup-native setup-docker doctor up down restart logs ps shell db-shell \
        migrate revision seed dev test test-api test-app lint lint-list typecheck audit \
        openapi secrets hooks \
        verify verify-api verify-app build clean reset

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  New here? \033[36mmake setup\033[0m brings the whole stack up (Docker, falling back to native)."

# --- getting started --------------------------------------------------------------------
#
# On Windows there is no make: run `setup.cmd`, or `powershell -ExecutionPolicy Bypass
# -File scripts\bootstrap.ps1`. It installs what Windows is missing and then runs the same
# bootstrap.sh under Git Bash, so there is one implementation of the setup logic.

setup: ## Clone the SPA, write .env, and start everything (Docker, falling back to native)
	@./scripts/bootstrap.sh

setup-docker: ## Setup, refusing to fall back to a native install
	@./scripts/bootstrap.sh --docker

setup-native: ## Setup without Docker — venv + npm + MySQL, installing what is missing
	@./scripts/bootstrap.sh --native

doctor: ## Report what setup would do and what is missing, changing nothing
	@./scripts/bootstrap.sh --check

# --- Docker -----------------------------------------------------------------------------

up: ## Start the stack in the background
	$(COMPOSE) up -d

down: ## Stop the stack, keeping the database
	$(COMPOSE) down

restart: ## Rebuild and restart after a code change
	$(COMPOSE) up -d --build

logs: ## Follow logs from every service
	$(COMPOSE) logs -f

ps: ## Show what is running
	$(COMPOSE) ps

shell: ## A shell inside the API container
	$(COMPOSE) exec api bash

db-shell: ## A MySQL prompt on the app database
	@$(COMPOSE) exec db sh -c 'exec mysql -u"$$MYSQL_USER" -p"$$MYSQL_PASSWORD" "$$MYSQL_DATABASE"'

# --- database ---------------------------------------------------------------------------

migrate: ## Apply migrations (the container does this on start; this is for a running stack)
	$(COMPOSE) exec api alembic upgrade head

# A one-off container rather than `exec` into the running one: the image's /app is
# root-owned and read-only to its uid-10001 user and the service mounts nothing, so a
# revision written there fails or dies with the container. This one runs as you, with
# alembic/versions bind-mounted, so the file lands in your checkout; it still diffs
# against the stack's database (which has to be at head — `make up` gets it there).
revision: ## Autogenerate a migration into alembic/versions — READ IT before applying (see CLAUDE.md §17)
	@test -n "$(m)" || { echo 'usage: make revision m="add_x_table"'; exit 1; }
	$(COMPOSE) run --rm -u "$$(id -u):$$(id -g)" -v "$(CURDIR)/alembic/versions:/app/alembic/versions" \
		--entrypoint alembic api revision --autogenerate -m "$(m)"

seed: ## Seed the superadmin and default roles
	$(COMPOSE) exec api python -m seeders.user_seeder

# --- development (native) ---------------------------------------------------------------

dev: ## Run both dev servers with hot reload, on the ports in .env (Ctrl-C stops both)
	@./scripts/dev.sh

# --- tests and gates --------------------------------------------------------------------
#
# `verify` is the gate. It covers both halves (API and SPA). CI runs these same targets for
# the API so the two cannot disagree — a CI that diverges from the local command is a CI
# whose failures nobody can reproduce. The SPA has no workflow yet (docs/hardening-audit.md
# F-07), so `verify-app` is a local gate only.

# The venv's interpreter when there is one, called by path so no target depends on an
# activated shell. Every Python target goes through it: sourcing `.venv/bin/activate` in a
# recipe aborts /bin/sh outright when the file is missing (dash treats `.` as a special
# builtin), and an `activate && pytest || pytest` fallback re-runs a red suite.
PY := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

test: test-api test-app ## Run both test suites

test-api: ## Backend suite (in-memory SQLite; no infrastructure needed)
	@$(PY) -m pytest

test-app: ## Frontend suite
	@cd $(APP_DIR) && npm test

lint: ## Ruff, gated by the ledger in ruff-suppressions.json (a NEW finding fails)
	@$(PY) -m ruff check --no-cache --statistics . || true
	@$(PY) scripts/lint_ledger.py

lint-list: ## The findings themselves, file by file — the work list behind `make lint`
	@$(PY) -m ruff check --no-cache .

typecheck: ## mypy over the modules listed in [tool.mypy] — those must stay green
	@$(PY) -m mypy

audit: ## Known CVEs in both pinned dependency sets. GATES — a new advisory fails.
# Flipped from reporting to gating on 2026-08-27, once the backlog reached zero. It had
# been `|| true` since the hardening pass, with a note saying it would start gating "the
# day the current backlog is zero" — that day was the day somebody finally ran it and
# found 22 advisories across 5 packages, seven of them in PyJWT, which authenticates every
# request in this service. An audit that never fails is an audit nobody runs.
#
# Anything that genuinely cannot be upgraded is waived, with a reason and a date, in
# docs/security-exceptions.md, and `--ignore-vuln` is passed for it below. A waiver is a
# reviewed line in a file; `|| true` is a waiver for everything, forever, that nobody reads.
	@$(PY) scripts/audit_deps.py

verify-api: lint typecheck ## The backend gate: lint + types + tests + coverage ratchet
	@$(PY) -m pytest --cov --cov-report=term-missing

verify-app: ## The frontend gate: typecheck + lint + coverage ratchet
	@cd $(APP_DIR) && npm run verify

verify: verify-api verify-app ## Everything that must pass before calling work done

openapi: ## Regenerate docs/openapi-digest.txt — run this in the SAME commit as an API change
	@$(PY) scripts/openapi_digest.py --write

secrets: ## Scan every tracked file for credential-shaped content
	@$(PY) scripts/check_staged_secrets.py --all

hooks: ## Install the pre-commit hooks (ruff, secret scan, no stray .env)
	@$(PY) -m pre_commit install 2>/dev/null \
		|| { echo "pre-commit not installed: pip install -r requirements-dev.txt"; exit 1; }

# --- housekeeping -------------------------------------------------------------------------

build: ## Rebuild the images without starting anything
	$(COMPOSE) build

clean: ## Stop the stack and remove build artefacts (keeps your data)
	-$(COMPOSE) down
	rm -rf $(APP_DIR)/dist $(APP_DIR)/coverage .pytest_cache htmlcov .coverage
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

reset: ## DESTRUCTIVE — also deletes the database volume and all data in it
	@printf 'This deletes the database volume and all data in it. Type "yes": ' \
		&& read ans && [ "$$ans" = "yes" ] || { echo "aborted"; exit 1; }
	$(COMPOSE) down -v
