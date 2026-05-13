# Umbrella (control-plane)

Umbrella — это набор подсистем, связывающих GMAS, workspaces и Ouroboros. Он реализует политику
границ, реестр workspace, рантайм запусков, retrieval по GMAS, observability и развитую memory-систему
для lessons, competency tracking и семантического накопления знаний.

## UmbrellaServices

Центральная точка входа — класс `UmbrellaServices` в `umbrella/integration/services.py`.
Он инициализирует все подсистемы в правильном порядке зависимостей:

```python
from umbrella.integration.services import UmbrellaServices
from pathlib import Path

services = UmbrellaServices(
    repo_root=Path("."),
    use_live_llm=True,
    llm_model="anthropic/claude-sonnet-4-20250514",
    llm_api_key="sk-...",
)

cp = services.get_control_plane()   # ControlPlaneEngine
reg = services.get_registry()       # WorkspaceRegistry
ret = services.get_retrieval()      # RetrievalService
```

Порядок инициализации: telemetry -> memory -> retrieval -> registry -> control plane.

## Подсистемы

### Политика (`umbrella/policies/`)

Машиночитаемые правила границ репозитория. Определяют, что можно менять, кому и когда.

Ключевые файлы:

- `default_policy.yaml` — значения по умолчанию.
- `engine.py` — `PolicyEngine` с API решений.

Основные функции:

| Функция | Роль |
|---------|------|
| `classify_path(path)` | Категория поверхности (framework, manager, workspace_instance, ...) |
| `can_edit_path(path)` | Можно ли писать по пути |
| `should_prefer_workspace_patch(ctx)` | Предпочтение workspace-патча перед self-improvement |
| `can_trigger_self_improvement(ctx)` | Допустим ли self-improvement |
| `requires_human_escalation(ctx)` | Нужно ли участие человека |

Подробнее: `umbrella/policies/README.md`.

### Реестр workspace (`umbrella/workspace_registry/`)

Каталог всех workspace с обнаружением и выбором.

| Модуль | Назначение |
|--------|------------|
| `registry.py` | `WorkspaceRegistry` — discover, register, select |
| `discovery.py` | Файловое обнаружение `workspace.toml` |
| `models.py` | Типы: `WorkspaceRef`, `SeedWorkspaceProfile`, `TaskInstanceProfile`, `WorkspaceLineageRecord` |
| `task_main.py` | Загрузка и парсинг `TASK_MAIN.md` |

Обнаружение: рекурсивный обход `workspaces/**/workspace.toml` с игнорированием
служебных каталогов (`runs`, `snapshots`, `__pycache__`, ...).

### Рантайм workspace (`umbrella/workspace_runtime/`)

Создание instance, запуск и инспекция workspace.

| Модуль | Назначение |
|--------|------------|
| `runner.py` | Единый раннер: `prepare_workspace()`, `run_workspace()`, `inspect_workspace()` |
| `instances.py` | `create_task_instance()`, `snapshot_instance()`, `archive_instance()` |
| `adapters/` | Адаптеры под конкретные seed: `AgentResearchAdapter`, `WorldPredictionAdapter`, `GenericWorkspaceAdapter` |

Раннер подбирает адаптер по `workspace_id` seed:

```python
_ADAPTER_BY_SEED_ID = {
    "agent_research": AgentResearchAdapter,
    "evaluation": EvaluationAdapter,
    "world_prediction": WorldPredictionAdapter,
}
```

Для workspace без специального адаптера используется `GenericWorkspaceAdapter`.

### Артефакты и observability (`umbrella/artifacts/`)

Индексация запусков workspace, чтение логов и сравнение результатов.

| Модуль | Назначение |
|--------|------------|
| `run_index.py` | `index_workspace_runs()` — индексация всех запусков |
| `log_access.py` | `read_result_summary()`, `read_events_jsonl()` |
| `models.py` | `RunManifest`, `WorkspaceRunIndex`, `RunStatus` |

Менеджер использует индекс для сравнения запусков и отслеживания прогресса.

### Retrieval (`umbrella/retrieval/`)

Поисковая система по коду и документации GMAS.

| Модуль | Назначение |
|--------|------------|
| `service.py` | `RetrievalService` — оркестрация всех методов поиска |
| `gmas_context.py` | `build_gmas_context()` — контекст для Ouroboros |
| `lexical.py` | BM25-индекс |
| `symbols.py` | Символьный индекс (классы, функции, модули) |
| `docs_index.py` | Индекс документации (`mkdocs.yml` навигация) |
| `code_index.py` | Code-aware символьный индекс |
| `workspace_usage.py` | Паттерны использования GMAS в workspace |
| `cards.py` | Генерация retrieval cards |

Подробнее: [gmas.md](gmas.md#retrieval-по-gmas).

### Интеграция с Ouroboros (`umbrella/integration/`)

Мост между Umbrella и Ouroboros.

| Модуль | Назначение |
|--------|------------|
| `services.py` | `UmbrellaServices` — центральный сервис-локатор |
| `ouroboros_bridge.py` | Синхронизация Umbrella-контекста в Ouroboros drive |
| `ouroboros_launcher.py` | Запуск и управление процессом Ouroboros |

`ouroboros_bridge.py` отвечает за:

- Создание layout в `.umbrella/ouroboros_drive/` (logs, memory, state, task_results).
- Синхронизацию workspace-контекста, lessons и задач из Umbrella memory в drive.
- Обеспечение Ouroboros актуальными знаниями о состоянии workspace.

Текущий основной entrypoint Ouroboros-first запуска живёт в `umbrella/app_ouroboros.py`:
он читает `TASK_MAIN.md`, формирует mission prompt и затем вызывает
`run_ouroboros_improvement_sync()`. Операторский UI поднимается отдельно:
`uv run bridge` или `uv run python -m umbrella.web_bridge` (см. `umbrella/web_bridge/`; перед запуском — `yarn build` в `web/`).

### Control plane (`umbrella/control_plane/`)

Менеджерский движок, принимающий решения о выборе workspace, создании instance,
запуске и оценке результатов.

Ключевой класс: `ControlPlaneEngine` в `umbrella/control_plane/engine.py`.

Интеграция с Ouroboros: `umbrella/control_plane/ouroboros_integration.py` —
функции `create_ouroboros_self_improvement_task()` и `run_ouroboros_improvement_sync()`
для постановки задач Ouroboros и синхронного запуска итерации улучшения. Модуль также
связывает запуск с **кандидатами Meta-Harness** (идентификаторы, снимки состояния репозитория),
чтобы итерации можно было воспроизводимо оценивать и сравнивать.

Для временных правок кода менеджера в рамках задачи используется **sandbox self-edit**
(`umbrella/control_plane/sandbox_self_edit.py`): снимок git до правок и откат после завершения
задачи, чтобы не оставлять «грязный» репозиторий после экспериментального self-patch.

### Meta-Harness (`umbrella/meta_harness/`)

Внешний слой оптимизации harness: каждая попытка оформляется как кандидат с манифестом,
снимками (промпт, политика, исходники, входная память), артефактами execution/evaluation
и решением о promotion.

| Модуль | Назначение |
|--------|------------|
| `store.py` | Файловый store под `.umbrella/meta_harness/` |
| `capture.py` | Захват состояния harness для кандидата |
| `evaluator.py` | Оценка кандидата на search set |
| `promotion.py` | Решение о promotion и применение патча |
| `search_sets.py` | Сбор и загрузка search set (в т.ч. из workspace и memory) |
| `cli.py` | CLI; также `python -m umbrella.meta_harness` |

Точка входа верхнего уровня: `run_meta_harness.py`. Подробный
план и обоснование: [meta-harness-improvement-plan.md](meta-harness-improvement-plan.md).

### Memory (`umbrella/memory/`)

Memory в Umbrella — это не просто журнал заметок, а отдельный слой принятия решений.
Он нужен, чтобы менеджер:

- помнил успешные и неуспешные паттерны по workspace;
- отличал проблему конкретного workspace от проблемы самого менеджера;
- накапливал сигналы capability gaps;
- умел собирать компактный prompt-ready context для следующих итераций;
- мог хранить как структурированную локальную память, так и более богатую palace-таксономию.

#### Что именно хранится

Модель памяти разбита на несколько типов (`umbrella/memory/models.py`):

| Тип | Роль |
|-----|------|
| `WorkingMemoryRecord` | Краткоживущая память текущей итерации: brief, hypothesis, last run, patch plan |
| `WorkspaceMemoryRecord` | Память конкретного workspace: lessons, invariants, limitations, successful/failure patterns |
| `ManagerMemoryRecord` | Кросс-workspace память менеджера: стратегии, признаки manager-vs-workspace проблем, retrieval patterns |
| `CompetencyMemoryRecord` | Память о capability areas менеджера и их эволюции |
| `WorkspaceLessonRecord` | Уроки по конкретным workspace-итерациям, включая `files_changed` и `was_promoted` |
| `ManagerLessonRecord` | Уроки уровня менеджера, включая `affected_capability_area` и результат self-improvement |
| `CapabilitySignal` | Сигналы силы/слабости в capability area (`retrieval`, `gmas_knowledge`, `planning`, ...) |
| `CompetencyGapRecord` | Открытые и закрытые capability gaps с severity, evidence и suggested actions |

#### MemoryStore: структурированная локальная память

`MemoryStore` (`umbrella/memory/store.py`) — основной file-backed store. Он:

- хранит lessons, gaps и signals в JSONL под `.umbrella/memory/`;
- держит in-memory индексы для быстрых запросов;
- поддерживает фильтрацию по `task_id`, `workspace_id`, `lesson_type`, `tags`, age и priority;
- умеет делать reprioritization с decay-моделью;
- закрывает или откладывает stale gaps;
- умеет compact/rewrite storage.

Физические файлы по умолчанию:

| Файл | Содержимое |
|------|------------|
| `.umbrella/memory/lessons.jsonl` | Workspace и manager lessons |
| `.umbrella/memory/gaps.jsonl` | Competency gaps |
| `.umbrella/memory/signals.jsonl` | Capability signals |

Это даёт локальный, простой и инспектируемый слой памяти без обязательной внешней инфраструктуры.

#### Competency ledger: когда система понимает, что проблема в ней самой

Отдельный важный кусок — `umbrella/memory/competency.py`.
Он превращает отдельные негативные наблюдения в capability ledger:

- `record_competency_signal(...)` записывает сигнал силы/слабости;
- при накоплении негативных сигналов `_check_and_update_gap(...)` может открыть gap автоматически;
- severity выводится из силы и повторяемости сигналов;
- gap помечается как manager-level или workspace-level;
- `should_trigger_self_improvement(...)` решает, пора ли запускать self-improvement.

Категории сигналов:

- `no_progress_iterations`
- `retrieval_misses`
- `repeated_failure_mode`
- `human_feedback`
- `high_cost_no_gain`
- `missing_capability`

Это как раз тот механизм, который делает self-improvement не «на глаз», а более формализованным.

#### Context builder: память как вход в prompt, а не только как архив

`umbrella/memory/context_builder.py` собирает память в компактные контекстные пакеты:

- `build_manager_context_bundle(...)` — bundle для manager decision-making;
- `build_workspace_context_bundle(...)` — bundle для workspace-level операций;
- `ingest_workspace_run(...)` — переводит завершённый run в memory summary и lessons;
- `update_working_memory(...)` — обновляет краткоживущую память текущей итерации.

Именно здесь память превращается из набора записей в то, что реально попадает в LLM-контекст.

#### Contrastive retrieval: успехи и провалы рядом

`umbrella/memory/contrastive.py` дополняет «топ-k похожих уроков» парами **confirmers /
challengers** — что сработало и что нет в сходном контексте, чтобы менеджер видел обе стороны,
а не только релевантные успехи.

#### MemPalace backend: семантическая память с таксономией wing/hall/room/drawer

`umbrella/memory/palace_backend.py` — одна из самых интересных частей memory-слоя.
Он проецирует память Umbrella на MemPalace-модель:

- `workspace_id -> wing_{workspace_id}`
- `system -> wing_umbrella_system`
- `event_type -> hall_*`
- `room` — свободная тематическая комната внутри wing
- `drawer` — конкретная запись памяти

Карта hall по типам событий:

| Event type | Hall |
|------------|------|
| `command`, `test`, `error`, `bug`, `warning` | `hall_events` |
| `change`, `code`, `decision`, `seed`, `commit` | `hall_facts` |
| `lesson`, `idea`, `observation`, `insight`, `completion` | `hall_discoveries` |
| `preference`, `config` | `hall_preferences` |
| `advice`, `recommendation` | `hall_advice` |

Что умеет backend:

- `add(...)` — положить запись в palace с `wing/hall/room` классификацией;
- `search(...)` — semantic search по palace, с фильтрацией по workspace/room;
- `list_wings()` и `list_rooms()` — агрегаты по структуре памяти;
- `get_taxonomy()` — дерево `{wing: {room: count}}`;
- `recent()` — последние записи;
- `stats()` — сводка по дворцу памяти.

Важно, что это уже не просто JSONL-журнал: это семантически индексируемая память с понятной
пространственной моделью хранения знаний.

#### HierarchicalMemory: лёгкая palace-подобная альтернатива без жёсткой зависимости

Кроме `PalaceBackend`, в коде есть `HierarchicalMemory` (`umbrella/memory/hierarchical.py`).
Это append-only JSONL-представление palace-иерархии, где записи имеют `palace_path`
вроде `workspaces/agent_research/errors` или `ideas/gmas`.

Его роль:

- дать лёгкую локальную иерархическую память без обязательного ChromaDB/MCP;
- сохранить palace-логику даже в более простом runtime;
- обеспечить простое lexical retrieval по `palace_path`, title, content и tags.

То есть memory-слой в Umbrella уже устроен двухконтурно:

- простой структурированный store для lessons/gaps/signals;
- более богатая palace/hierarchical модель для семантического и тематического накопления знаний.

#### Почему это важно архитектурно

Память в Umbrella напрямую участвует в управлении, а не только в архивировании:

- влияет на выбор следующего шага;
- влияет на решение `workspace patch` vs `self-improvement`;
- хранит evidence для promotion в seed;
- формирует prompt-контекст для следующих запусков;
- даёт Web UI и Ouroboros доступ к накопленной картине мира.

Это одно из главных отличий проекта от более простого «агент просто правит код» подхода:
здесь память — часть control-plane, а не побочный лог.

### Web bridge (`umbrella/web_bridge/`)

Один HTTP-процесс: собранный React из `web/build` (или `web/dist`) и JSON API под `/api/*`.
Запуск: `uv run bridge` или `uv run python -m umbrella.web_bridge` (порт по умолчанию 8765). Перед этим — `yarn install` и `yarn build` в `web/` (см. [docs/README.md](README.md#запуск-web-bridge)).

Примеры эндпоинтов:

- `GET /api/health`, `GET /api/workspaces`, `GET /api/runs`, `GET /api/logs`, `GET /api/memory`
- `GET /api/dashboard/stats?workspace_id=...`
- чат: threads, messages, POST сообщения (см. `umbrella/web_bridge/handler.py`)

Старый встроенный HTML/JS dashboard (`umbrella/dashboard/`) удалён.

## Конфигурация

Рантайм-конфигурация задаётся в секции `runtime` файла `umbrella/policies/default_policy.yaml`:

| Параметр | Значение | Назначение |
|----------|---------|------------|
| `max_budget_usd` | `null` | Бюджет (null = без лимита) |
| `quality_completion_threshold` | `0.85` | Минимальный eval score для завершения |
| `self_improve_after_stalled_iterations` | `2` | Когда включать self-improvement |
| `human_review_stages` | `[outline_approved, final_draft]` | Стадии, требующие review |
| `instance_cleanup_enabled` | `true` | Очистка старых runs/snapshots |

Значения можно переопределять через CLI-флаги или `load_runtime_config(overrides={...})`.
