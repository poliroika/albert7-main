# Создание workspace

Workspace можно создать тремя способами: вручную как новый seed, программно как task-instance
через Umbrella control plane, или операторскими скриптами из корня репозитория.

## Способ 1: новый seed workspace вручную

Seed workspace — это стабильный шаблон для класса задач. Создание нового seed сводится
к пяти шагам.

### Шаг 1. Скопировать структуру

Возьмите существующий seed как образец. Канонический пример — `workspaces/agent_research/`:

```powershell
Copy-Item -Recurse workspaces\agent_research workspaces\my_workspace
```

Удалите из копии каталоги, генерируемые в рантайме (`runs/`, `snapshots/`, `reports/`,
`memory/`, `logs/`, `instances/`), и файлы, специфичные для исходного seed
(`instance_metadata.json`).

### Шаг 2. Заполнить workspace.toml

Откройте `workspaces/my_workspace/workspace.toml` и замените метаданные:

```toml
workspace_id = "my_workspace"
name = "My Workspace"
description = "Описание назначения workspace"
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
notes = "Standalone workspace."
```

Обязательные поля: `workspace_id`, `name`, `description`, `task_main_file`.
Поле `metadata.engine = "gmas"` гарантирует, что workspace использует GMAS как фреймворк.

### Шаг 3. Написать TASK_MAIN.md

Создайте `workspaces/my_workspace/TASK_MAIN.md` с описанием задачи:

```markdown
# My Workspace

## Objective
Что должен делать workspace — чёткая формулировка цели.

## Final Deliverable
Какой артефакт workspace производит.

## Success Criteria
- Критерий 1
- Критерий 2

## Constraints
- Ограничение 1
```

Этот файл станет основным task brief для менеджера и рантайма.

### Шаг 4. Зарегистрировать seed

Добавьте workspace в `workspaces/registry.toml`:

```toml
seeds = ["agent_research", "world_prediction", "my_workspace"]
```

При желании создайте `seed_profile.toml` с capabilities и selection hints, чтобы
Umbrella мог автоматически выбирать workspace:

```toml
name = "My Workspace"
maturity = "experimental"
primary_task_classes = ["my_task_class"]
human_dependency_level = "low"

[[capabilities]]
name = "my_capability"
description = "Что умеет workspace"
weight = 1.0

[selection_hints]
task_classes = ["my_task_class"]
keywords = ["keyword1", "keyword2"]
```

### Шаг 5. Настроить граф агентов

Опишите топологию в `graph/topology.toml`:

```toml
name = "my_graph"
description = "Описание графа"
start_node = "first_agent"
end_node = "last_agent"
agents = ["first_agent", "processor", "last_agent"]

[[edges]]
source = "first_agent"
target = "processor"
weight = 1.0

[[edges]]
source = "processor"
target = "last_agent"
weight = 1.0
```

Для каждого агента создайте `.toml` в `agents/` и соответствующий промпт в `prompts/`.

### Проверка

Убедитесь, что workspace обнаруживается реестром:

```powershell
uv run python -c "
from umbrella.workspace_registry.registry import WorkspaceRegistry
from pathlib import Path
reg = WorkspaceRegistry(Path('.'))
found = reg.discover()
print([w.workspace_id for w in found])
"
```

## Способ 2: task-instance через код (программный)

Task-instance создаётся автоматически, когда Umbrella получает задачу. Вот как это работает
внутри.

### Через ControlPlaneEngine

Основной путь — через control plane. Когда менеджер выбрал workspace, вызывается
`ControlPlaneEngine._handle_workspace_selected()` в `umbrella/control_plane/engine.py`:

```python
instance = create_task_instance(
    seed_profile,
    runtime_task_brief,
    instances_root=self.workspaces_root / seed_profile.workspace_id / "instances",
    task_id=task.id,
    copy_seed_files=True,
)
```

### Через create_task_instance напрямую

Можно создать instance программно, без control plane:

```python
from pathlib import Path
from umbrella.workspace_registry.discovery import load_seed_profile
from umbrella.workspace_registry.models import TaskBrief
from umbrella.workspace_runtime.instances import create_task_instance

seed = load_seed_profile(Path("workspaces/agent_research"))
brief = TaskBrief(
    description="Исследовать и написать техническую статью о transformer архитектурах",
    task_id="task_transformers_article",
    task_class="article_writing",
    domains=["technology", "software_engineering"],
)
instance = create_task_instance(seed, brief, copy_seed_files=True)
print(f"Instance path: {instance.path}")
```

Результат: новая директория `workspaces/agent_research/instances/<id>_<timestamp>/`
с копией seed, собственным `TASK_MAIN.md`, и пустыми `runs/`, `snapshots/`, `reports/`,
`memory/`, `logs/`.

### Через UmbrellaServices (полный стек)

Для запуска полного цикла используйте `UmbrellaServices`:

```python
from pathlib import Path
from umbrella.integration.services import UmbrellaServices

services = UmbrellaServices(
    repo_root=Path("."),
    use_live_llm=True,
    llm_model="anthropic/claude-sonnet-4-20250514",
    llm_api_key="sk-...",
)

cp = services.get_control_plane()
# Control plane сам выберет workspace, создаст instance и запустит его
```

## Способ 3: операторские скрипты

### run_ouroboros_self_improve.py

Главный скрипт непрерывного улучшения. Читает `TASK_MAIN.md` из workspace, рендерит
промпт для Ouroboros и запускает цикл итераций:

```powershell
uv run python run_ouroboros_self_improve.py
```

Конфигурация через параметры `continuous_improvement_loop()`:

| Параметр | По умолчанию | Назначение |
|----------|-------------|------------|
| `workspace_id` | `"agent_research"` | Целевой seed workspace |
| `max_iterations` | `None` (без лимита) | Максимум итераций |
| `quality_threshold` | `0.70` | Минимальный eval score для завершения |
| `auto_promote` | `True` | Автоматический promotion в seed |
| `max_budget_usd` | `None` | Бюджет в USD |
| `timeout_hours` | `24.0` | Таймаут в часах |

После каждой успешной итерации скрипт пытается promote изменённые файлы из instance
обратно в seed (если `auto_promote=True`).

### umbrella/app_ouroboros.py

Актуальный single-run entrypoint в Ouroboros-first модели:

```powershell
uv run python umbrella\app_ouroboros.py workspaces\agent_research
```

Скрипт собирает workspace mission из `TASK_MAIN.md` и запускает
итерацию Ouroboros через инструменты Umbrella.

### Legacy entrypoint

`run_continuous_improvement.py` был удалён как часть
verification-loop-интеграции. Используйте Ouroboros-first entrypoints:
`umbrella/app_ouroboros.py` для одиночного прогона и
`run_ouroboros_self_improve.py` для непрерывного цикла с verification-гейтом.

## Проверка окружения

Перед созданием workspace убедитесь, что тесты проходят:

```powershell
# Все тесты
uv run pytest -q umbrella/tests

# Тест реестра
uv run pytest -q umbrella/tests/test_workspace_registry.py

# Тест рантайма
uv run pytest -q umbrella/tests/test_workspace_runtime.py
```
