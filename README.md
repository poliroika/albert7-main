# Umbrella

Umbrella — это **workspace-first control-plane** вокруг мультиагентного фреймворка **GMAS**: политика границ репозитория, реестр и рантайм workspace’ов, retrieval, артефакты запусков и операторский Web UI. Поверх этого крутится **Ouroboros** — менеджер долгих циклов улучшения: он не подменяет прикладной код в `workspaces/`, а ведёт итерации, память и (при необходимости) вторичный контур self-improvement.

Идея в одном предложении: **Ouroboros управляет эволюцией, GMAS исполняет графы, workspace — место, где лежит решение задачи.**

Большой техотчёт по частям — **[docs/technical-report/README.md](docs/technical-report/README.md)** (удобно открывать как раздел в GitHub/GitLab).

## Из чего состоит репозиторий

| Каталог | Роль |
|--------|------|
| `gmas/` | Движок графов, раннеры, инструменты. По политике проекта считается **read-only** для автопатчей. |
| `workspaces/` | Прикладные системы: seed-шаблоны и рабочие копии под задачи (`workspace.toml`, `TASK_MAIN.md`, тесты). |
| `umbrella/` | Control-plane: политика, registry, runtime, verification, web bridge, интеграция с Ouroboros. |
| `ouroboros/` | Код менеджера (loop, planner, tools, память); ставится как зависимость/путь для тестов. |
| `web/` | React-операторка (чат, ранны, workspace’ы, память, MCP, настройки). |

Служебные данные рантайма по умолчанию лежат под `.umbrella/` (логи, диск Ouroboros, web-store и т.д.).

Имя **корневой папки** репозитория на диске не зафиксировано (часто это всё ещё старый каталог вроде `albert7`); в командах и документации ориентируйтесь на наличие `pyproject.toml` и каталога `umbrella/`, а не на имя родительской директории.

## Документация

Оглавление по-русски — **[docs/README.md](docs/README.md)**. Коротко:

- [Архитектура](docs/architecture.md) — три слоя и связи.
- [Технический отчёт (многостраничный)](docs/technical-report/README.md) — Umbrella, Ouroboros, verification, bridge, конфигурация, эксплуатация.
- [Workspaces](docs/workspaces.md), [создание workspace](docs/creating-workspaces.md).
- [Umbrella-слой](docs/umbrella-layer.md), [Ouroboros](docs/ouroboros.md), [GMAS в контексте проекта](docs/gmas.md).
- Документация самого GMAS — в `gmas/docs/`.
- **GitLab Pages:** статический сайт из `docs/` собирается MkDocs (`mkdocs.yml` + job `pages` в `.gitlab-ci.yml`).

## Требования

- **Python** ≥ 3.11 ([`pyproject.toml`](pyproject.toml)).
- **[uv](https://docs.astral.sh/uv/)** для зависимостей и запуска скриптов.
- Для UI: **Node.js** и **Yarn** или **npm** (в `web/` зафиксирован `packageManager` для Yarn; скрипты `build` / `start` те же).

## Установка (терминал)

Из корня репозитория:

```powershell
uv sync --extra dev
```

Опционально профиль с Terminal Bench: `uv sync --extra dev --extra tb` (см. `pyproject.toml`).

Проверка тестов Umbrella и Ouroboros:

```powershell
uv run pytest -q
```

## Конфигурация LLM (`.env`)

В корне репозитория положите `.env` (подхватывается `umbrella.env.load_env`). Минимально для «живого» режима:

- `LLM_API_KEY` — ключ API (при необходимости сработает и `OPENAI_API_KEY`).
- `LLM_MODEL` — модель по умолчанию для части путей.
- `LLM_BASE_URL` — необязательно, если используете совместимый прокси или нестандартный endpoint.

Для Ouroboros/Web часто задают также `OUROBOROS_MODEL` / `LLM_MODEL` (дефолт чата в bridge берётся из этих переменных и `.env`, см. `umbrella/web_bridge/util.py`).

Ограничение числа раундов LLM в цикле Ouroboros: `OUROBOROS_MAX_ROUNDS` (число ≤ 0 обычно означает «без жёсткого потолка» в логике приложения; см. `--max-rounds` в `umbrella/app_ouroboros.py`).

Для Web UI при старте рана через API можно переопределить лимит повторов verification: `OUROBOROS_WEB_MAX_VERIFY_RETRIES` (по умолчанию в коде bridge — 20).

## Запуск из терминала

Все команды — из **корня** репозитория, если не указано иное.

### Одноразовый прогон Ouroboros по workspace

```powershell
uv run python umbrella/app_ouroboros.py workspaces/agent_research --live --verbose --max-verify-retries 3
```

Полезные флаги (см. `umbrella/app_ouroboros.py`): `--task`, `--task-file`, `--timeout-hours`, `--max-budget`, `--max-rounds`, `--mock`, `--no-verify`, `--verification-timeout-seconds`, `--allow-seed-writes` (отключает требование task-instance). Код возврата `0` ожидается, когда прошла runtime-verification (если она не отключена).

### Непрерывное улучшение по `TASK_MAIN.md`

```powershell
uv run python run_ouroboros_self_improve.py
```

Скрипт ожидает ключ в `.env`; параметры — `--help` у файла.

### Meta-Harness (внешний контур экспериментов)

```powershell
uv run python run_meta_harness.py
```

или `uv run python -m umbrella.meta_harness` — см. [docs/meta-harness-improvement-plan.md](docs/meta-harness-improvement-plan.md).

### Web bridge (API + раздача собранного UI)

Точка входа: `uv run bridge` ≡ `uv run python -m umbrella.web_bridge` (по умолчанию порт **8765**).

Аргументы сервера (см. `umbrella/web_bridge/server.py`):

- `--host` (по умолчанию `127.0.0.1`)
- `--port` (по умолчанию `8765`)
- `--repo-root` — корень репозитория, если запускаете не из него
- `--log-level` — например `DEBUG`

## Web UI

### Режим «как у оператора»: одна сборка, один процесс

1. Собрать фронт (из корня). Подойдут **Yarn** (как в `packageManager`) или **npm** — скрипты те же: `build` кладёт статику в `web/build` (см. `web/package.json`).

   **Yarn:**

   ```powershell
   cd web
   yarn install
   yarn build
   cd ..
   ```

   **npm:**

   ```powershell
   cd web
   npm install
   npm run build
   cd ..
   ```

2. Запустить bridge из **корня** репозитория (порт по умолчанию **8765**, см. `umbrella/web_bridge/server.py`):

   ```powershell
   uv run bridge
   ```

   То же самое явно:

   ```powershell
   uv run python -m umbrella.web_bridge --port 8765
   ```

   Без `uv` (если окружение уже с установленным пакетом): `python -m umbrella.web_bridge --port 8765`.

3. Открыть в браузере: `http://127.0.0.1:8765` (или свой `--host` / `--port`).

Статика читается из `web/build` или `web/dist`. Если не сделать `yarn build` / `npm run build`, корень страницы может открыться, но `/api/*` и ассеты будут вести себя непредсказуемо.

**Важно:** после сборки **не** используйте `npm start` / `yarn start` для этого сценария — `start` поднимает отдельный dev-сервер React (обычно порт 3000), см. ниже. Для одного процесса с API и статикой нужен только bridge.

### Режим разработки UI (горячая перезагрузка)

Нужны **два** процесса:

1. Bridge с API: `uv run bridge` (порт по умолчанию **8765**).
2. Dev-сервер React: `cd web && yarn start` — обычно **http://localhost:3000**.

В dev Craco проксирует `/api` на `http://127.0.0.1:8765`; цель можно сменить переменной **`REACT_APP_DEV_API_PROXY`**. Если фронт отдаётся отдельно без bridge, для production-сборки можно задать базовый URL API через **`REACT_APP_BACKEND_URL`**.

Страницы приложения: лендинг `/`, дальше под оболочкой — `/chat`, `/workspaces`, `/runs`, `/memory`, `/logs`, `/dashboard`, `/mcp`, `/settings` (см. `web/src/App.js`).

## Ключевые entrypoints в коде

- Политика: `umbrella/policies/engine.py`
- Реестр workspace’ов: `umbrella/workspace_registry/registry.py`
- Рантайм: `umbrella/workspace_runtime/runner.py`
- Индекс запусков: `umbrella/artifacts/run_index.py`
- Retrieval: `umbrella/retrieval/service.py`
- Синхронный запуск Ouroboros из Umbrella: `umbrella/control_plane/ouroboros_integration.py`
- Verification: `umbrella/verification/`
- HTTP bridge: `umbrella/web_bridge/server.py`, маршруты — `umbrella/web_bridge/handler.py`

## Принципы работы с репозиторием

- Улучшать в первую очередь **workspace**, а не «менеджера ради менеджера».
- Не автопатчить `gmas/` без явного решения человека.
- Мутировать прикладную работу предпочтительно в **task-instance**, а не в seed; материализация — см. документацию в `docs/workspaces.md`.
