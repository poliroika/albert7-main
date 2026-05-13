# Документация Umbrella

На **GitLab Pages** этот же каталог собирается в статический сайт (MkDocs Material): в **корне репозитория** лежат `mkdocs.yml` и `.gitlab-ci.yml`. Имя **папки клона** на диске может быть любым (например, старый каталог `albert7`); в текстах ниже важны относительные пути внутри неё: `umbrella/`, `gmas/`, `ouroboros/`, `workspaces/`, `docs/`. После деплоя используйте поиск и боковое меню на сайте.

Umbrella — это workspace-first control-plane и интеграционный слой вокруг фреймворка GMAS.
Репозиторий объединяет четыре крупных блока, каждый со своей ролью:

| Блок | Роль | Изменяемость |
|------|------|--------------|
| `gmas/` | Мультиагентный фреймворк (движок графов, раннеры, инструменты, память) | Read-only |
| `workspaces/` | Прикладные системы, решающие конкретные задачи поверх GMAS | Основная зона изменений |
| `umbrella/` | Control-plane: политика, реестр, рантайм, retrieval, observability | Свободно изменяем |
| `ouroboros/` | Менеджер: оркестрация улучшений, долгая память, вторичная самомодификация | Mutable, по правилам |

Формула целевой системы:

> **Ouroboros управляет, GMAS исполняет, workspaces решают прикладную задачу.**

Отдельно важно: у `Umbrella` уже есть развитый memory-слой. Это не просто лог событий, а
структурированная система lessons, competency gaps/signals и palace-подобной семантической памяти,
которая влияет на выбор стратегии и решение о self-improvement.

**Техотчёт по коду (много страниц, как книга):** [technical-report/README.md](technical-report/README.md) — удобно листать на GitHub/GitLab по ссылкам между главами.

## Разделы документации

| Раздел | Описание |
|--------|----------|
| [Технический отчёт](technical-report/README.md) | Многостраничный разбор (оглавление + части 01–14): код, потоки, API bridge, конфигурация, эксплуатация |
| [Архитектура](architecture.md) | Три слоя системы, потоки данных, политика границ, внешний контур Meta-Harness |
| [Workspaces](workspaces.md) | Seed и task-instance, `workspace.toml`, `TASK_MAIN.md`, реестр |
| [Создание workspace](creating-workspaces.md) | Практические сценарии: ручной seed, программный instance, скрипты |
| [GMAS](gmas.md) | Роль фреймворка, контракт использования, retrieval |
| [Umbrella](umbrella-layer.md) | Подсистемы Umbrella: registry, runtime, artifacts, retrieval, control plane, memory, dashboard, Meta-Harness |
| [Ouroboros](ouroboros.md) | Роль менеджера, два контура улучшения, интеграция с Umbrella, инструменты |
| [Meta-Harness (план и идеи)](meta-harness-improvement-plan.md) | Концепция внешней оптимизации harness; соответствует реализации в `umbrella/meta_harness/` и `.umbrella/meta_harness/` |

## Ключевые entrypoints (код)

| Модуль | Назначение |
|--------|------------|
| `umbrella/policies/engine.py` | Политика границ репозитория |
| `umbrella/workspace_registry/registry.py` | Обнаружение и каталог workspace |
| `umbrella/workspace_runtime/runner.py` | Единый раннер workspace |
| `umbrella/workspace_runtime/instances.py` | Создание task-instance из seed |
| `umbrella/artifacts/run_index.py` | Индексация и observability запусков |
| `umbrella/retrieval/service.py` | Retrieval по GMAS (BM25 + символы + docs) |
| `umbrella/integration/services.py` | Центральный сервис-локатор `UmbrellaServices` |
| `umbrella/control_plane/ouroboros_integration.py` | Синхронный запуск итераций Ouroboros, связка с Meta-Harness (кандидаты, снимки) |
| `umbrella/meta_harness/store.py` | Файловый store экспериментов и кандидатов (см. `.umbrella/meta_harness/`) |
| `umbrella/web_bridge/server.py` | Операторский UI: React из `web/build` + JSON API под `/api/*` — см. [Запуск web bridge](#запуск-web-bridge) |
| `run_meta_harness.py` | Внешний цикл Meta-Harness: предложение → оценка на search set → решение о promotion |
| `run_ouroboros_self_improve.py` | Непрерывное улучшение по `TASK_MAIN`; при наличии `candidate_id` — проверка promotion через Meta-Harness |

## Запуск web bridge (операторский UI)

Один процесс отдаёт **статику React** и **JSON API** (`/api/*`) на одном порту (по умолчанию `8765`). Порядок шагов важен: сначала фронт, затем Python-окружение, затем bridge.

### 1. Собрать UI

Из **корня репозитория**:

```bash
cd web
yarn install
yarn build
cd ..
```

В каталоге `web/` появится `build/` — его читает `umbrella.web_bridge` (см. `umbrella/web_bridge/util.py`).

### 2. Синхронизировать зависимости Python (uv)

Из корня:

```bash
uv sync --extra dev
```

### 3. Запустить bridge

```bash
uv run bridge
```

Эквивалент: `uv run python -m umbrella.web_bridge`. Другой порт: `uv run bridge --port 8766`.

После запуска откройте в браузере `http://127.0.0.1:8765` (или указанный `--host` / `--port`).

**Без шага 1** страница может открываться без стилей/скриптов или с ошибками — сначала всегда делайте `yarn build`.

## Быстрый старт

```powershell
# Один раз подготовить окружение
uv sync --extra dev

# Запуск тестов
uv run pytest -q umbrella/tests
```

Операторский UI — см. раздел [Запуск web bridge](#запуск-web-bridge) выше (`yarn build` в `web/`, затем `uv run bridge`).

## Чем это отличается от Claude Code и чистого Ouroboros

Это отличие не только в том, **где** пишется код, а в том, **какую модель развития системы**
предлагает проект.

### Идейно по сравнению с Claude Code

`Claude Code` силён как универсальный интерактивный coding assistant, но он не задаёт
собственную продуктовую архитектуру долгого цикла. Он помогает редактировать текущий репозиторий,
но не отвечает сам по себе на вопросы:

- где должна жить прикладная компетенция;
- как отделить reusable шаблон от одноразовой задачи;
- когда проблема в коде задачи, а когда в менеджере;
- как накапливать lessons и переводить их в следующий цикл улучшения.

`Umbrella` добавляет поверх такого режима несколько нововведений:

- **workspace-first модель**: прикладная компетенция живёт в `workspaces`, а не размазывается по менеджеру;
- **seed -> instance -> promotion цикл**: есть явная эволюционная модель, как шаблон превращается в боевой артефакт и обратно обогащается;
- **policy-driven boundaries**: формализовано, что можно менять автоматически, а что нельзя;
- **competency ledger и memory**: система копит не просто логи, а сигналы, gaps, lessons и prompt-ready summaries;
- **standalone artifact mindset**: итогом должен быть переносимый workspace, а не только успешная сессия редактирования.

То есть `Claude Code` хорош как исполнитель правок, а `Umbrella` сильнее как архитектура
долгоживущего улучшения прикладных систем.

### Идейно по сравнению с чистым Ouroboros

Чистый `ouroboros` близок по духу к самоулучшающемуся агенту: если он видит проблему,
естественный соблазн — улучшать самого себя, свои промпты, свои инструменты и свой контекст.

`Umbrella` вводит несколько ключевых сдвигов:

- **разделение менеджера и продукта**: менеджер больше не должен быть главным местом, где рождается решение;
- **внешняя поверхность эволюции**: улучшения по умолчанию направляются в workspace, а не в самого агента;
- **двухконтурная модель**: self-improvement сохраняется, но становится вторичным контуром после исчерпания workspace-итераций;
- **GMAS как стабильный execution substrate**: вместо ad-hoc оркестрации есть единый фреймворк, вокруг которого строятся workspaces;
- **retrieval + memory + observability как части control-plane**: менеджер принимает решения не вслепую, а с опорой на накопленное знание и run evidence;
- **lineage и promotion**: у результата есть происхождение, история экспериментов и путь обратно в seed.

Поэтому `Umbrella` лучше не в абстрактном смысле «умнее», а в более узком и важном для проекта:
он лучше подходит для построения системы, где прикладная компетенция постепенно кристаллизуется
в отдельные workspaces, а менеджер управляет их эволюцией вместо постоянной самомодификации.
