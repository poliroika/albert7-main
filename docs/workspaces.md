# Workspaces

Workspace — это основная единица производства в Umbrella: прикладная система, заточенная под
определённый класс задач. Каждый workspace содержит граф агентов, промпты, модели, evals и
артефакты, работающие поверх GMAS.

## Seed и task-instance

Система разделяет workspaces на два уровня, чтобы стабильные шаблоны не засорялись
экспериментальными патчами конкретных задач.

### Seed workspace

Стабильный шаблон, созданный человеком. Живёт в `workspaces/<workspace_id>/`.

Характеристики:

- Версионируется в git.
- Не патчится автоматически — изменения проходят через promotion (минимальный eval score 0.7).
- Определяет базовый граф, промпты, роли, инструменты и политики для класса задач.
- Содержит `TASK_MAIN.md` с общей миссией seed.

Зарегистрированные seed workspaces перечислены в `workspaces/registry.toml`:

```toml
version = "0.1.0"
seeds = ["agent_research", "world_prediction"]
instances = []
```

### Task-instance

Мутабельная копия seed под конкретную задачу. Создаётся автоматически через
`create_task_instance()` (модуль `umbrella/workspace_runtime/instances.py`).

Путь instance: `workspaces/<seed_id>/instances/<instance_id>_<timestamp>/`.

При создании instance происходит:

1. Копирование файлов seed (за исключением `runs/`, `snapshots/`, `reports/`, `memory/`,
   `logs/`, `instances/`, `__pycache__/`, `.git/`).
2. Создание служебных каталогов: `runs/`, `snapshots/`, `reports/`, `memory/`, `logs/`.
3. Перезапись идентичности workspace (новый `workspace_id`, привязка к `task_id`).
4. Инициализация `TASK_MAIN.md` из task brief.
5. Запись lineage-метаданных.

Instance — основная зона итеративного улучшения. Ouroboros свободно модифицирует графы,
промпты, evals и эксперименты внутри instance, оценивает результат и повторяет цикл.

По завершении задачи полезные патчи могут быть promoted обратно в seed через review.

## Обязательные файлы workspace

### workspace.toml

Контракт workspace: описание структуры, ссылки на файлы, список мутабельных путей.

Пример (`workspaces/agent_research/workspace.toml`):

```toml
workspace_id = "agent_research"
name = "Agent Research"
description = "A gMAS-first article writing workspace..."
task_main_file = "TASK_MAIN.md"
graph_file = "graph/topology.toml"
agents_dir = "agents"
prompts_dir = "prompts"
tools_allowlist_file = "tools/allowlist.toml"
models_file = "models/models.toml"
policies_file = "policies.toml"
evals_dir = "evals"
experiments_dir = "experiments"
runs_dir = "runs"
snapshots_dir = "snapshots"
reports_dir = "reports"

mutable_paths = [
    "graph", "agents", "prompts", "tools", "models",
    "evals", "experiments", "runs", "snapshots", "reports",
]

[metadata]
owner = "manual"
engine = "gmas"
engine_mutable = false
notes = "Standalone workspace. No ouroboros dependency."
```

Ключевые поля:

| Поле | Назначение |
|------|------------|
| `workspace_id` | Уникальный идентификатор workspace |
| `task_main_file` | Путь к `TASK_MAIN.md` |
| `graph_file` | Файл топологии графа GMAS |
| `mutable_paths` | Каталоги, разрешённые для автоматических изменений |
| `metadata.engine` | Фреймворк (`gmas`) |
| `metadata.engine_mutable` | Можно ли менять движок (всегда `false`) |

### TASK_MAIN.md

Главный задачный контракт workspace. Хранит цель, deliverables, критерии успеха и ограничения.

Для seed — это общая миссия шаблона. Для instance — конкретная прикладная задача.

Менеджер и рантайм опираются на `TASK_MAIN.md` как на основной task brief. Пример
из `workspaces/agent_research/TASK_MAIN.md`:

```markdown
## Objective
Develop a multi-agent workspace that can take a user's topic or query,
perform the necessary research, plan the article structure, write a draft,
revise weak sections, and deliver a convincing final article.

## Success Criteria
- The workspace can complete the full article-writing loop
- The final article is coherent, structured, and useful
- Weak drafts are improved through review and revision
```

### seed_profile.toml

Профиль seed workspace с метаданными для автоматического выбора: capabilities, task classes,
selection hints.

```toml
name = "Agent Research"
maturity = "stable"
primary_task_classes = ["article_writing", "article_research"]
human_dependency_level = "medium"

[[capabilities]]
name = "article_writing"
description = "Write technical articles from research through final delivery"
weight = 1.5

[selection_hints]
task_classes = ["article_writing", "article_research"]
keywords = ["article", "research", "writing", "paper"]
preferred_for_domains = ["technology", "software_engineering", "science"]
```

## Обнаружение workspace

Модуль `umbrella/workspace_registry/discovery.py` сканирует файловую систему в поисках
`workspace.toml`:

1. Рекурсивный обход `workspaces/**/workspace.toml`.
2. Игнорирование служебных каталогов: `runs`, `snapshots`, `reports`, `memory`, `logs`,
   `__pycache__`, `.git`, `archived`.
3. Загрузка конфигурации в `WorkspaceRef`.
4. Для seed — дополнительная загрузка `seed_profile.toml` в `SeedWorkspaceProfile`.

Всё это оркестрируется через `WorkspaceRegistry` (`umbrella/workspace_registry/registry.py`),
который предоставляет API: `discover()`, `register_workspace()`, `get_workspace()`,
`get_seed_profile()`, `match()` и `select_best()`.

Отдельный нюанс текущей реализации: policy engine в `umbrella/policies/engine.py` пока
жёстко выделяет `workspaces/agent_research` как canonical seed при классификации edit surface.
Архитектурно модель уже шире (`workspaces/registry.toml` содержит несколько seed), но
часть policy-логики всё ещё ориентирована на канонический seed.

## Lineage

Каждый instance хранит запись о происхождении:

- Из какого seed создан.
- Под какую задачу (`task_id`).
- Какие итерации и патчи были сделаны.
- Какие патчи оказались полезными и были promoted в seed.

Модель lineage определена в `umbrella/workspace_registry/models.py` (`WorkspaceLineageRecord`).

## Структура каталогов workspace

```
workspaces/agent_research/
    workspace.toml          # контракт workspace
    TASK_MAIN.md            # задачный контракт
    seed_profile.toml       # профиль seed для реестра
    graph/
        topology.toml       # граф агентов GMAS
    agents/                 # конфиги агентов (.toml)
    prompts/                # промпты агентов (.md)
    tools/
        allowlist.toml      # разрешённые инструменты
    models/
        models.toml         # конфигурация LLM-моделей
    policies.toml           # локальные политики workspace
    evals/                  # eval harness
    experiments/            # эксперименты и скрипты запуска
    runs/                   # результаты запусков (runtime)
    snapshots/              # снимки состояния (runtime)
    reports/                # сгенерированные отчёты (runtime)
    instances/              # task-instances (runtime)
```

Каталоги `runs/`, `snapshots/`, `reports/`, `memory/`, `logs/` и `instances/`
заполняются в рантайме и не входят в seed-шаблон.
