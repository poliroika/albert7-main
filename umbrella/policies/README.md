# umbrella/policies

Машиночитаемые и кодовые правила границ репозитория: как соотносятся `gmas`, `ouroboros`, `workspaces` и слой `umbrella`, что можно менять автоматически и когда нужен человек.

## Файлы

| Файл | Назначение |
|------|------------|
| `default_policy.yaml` | Значения по умолчанию (секция `system_boundary`), парсится при `load_default_policy()` |
| Python-модули | Типы, классификация путей, функции решений |

## Быстрый старт

```python
from pathlib import Path

from umbrella.policies import (
    load_default_policy,
    can_edit_path,
    should_prefer_workspace_patch,
    can_trigger_self_improvement,
    requires_human_escalation,
)
from umbrella.policies.engine import PolicyEngine

policy = load_default_policy()
engine = PolicyEngine(policy)

assert can_edit_path(Path("gmas/foo.py")).allowed is False
assert can_edit_path(Path("workspaces/task_x/graph.yaml")).allowed is True
```

## Основные понятия

| Понятие | Смысл |
|---------|--------|
| **Workspace-first** | Сначала правки в `workspaces/` (инстансы задач), а не в `ouroboros` |
| **gmas read-only** | `gmas/` не цель автоматического патчинга; изменения только с явным человеческим решением |
| **Seed vs instance** | Стабильный seed (например `workspaces/agent_research`) не патчится «в лоб»; инстансы под задачи — основная зона изменений |
| **Self-improvement** | Правки `ouroboros/` допустимы как вторичный контур, с триггерами и эскалацией |
| **Standalone workspace** | Итог должен оставаться полезным без рантайм-зависимости от `ouroboros` |
| **Retrieval по gmas** | Документация и код осознанно: BM25-first, при необходимости доработки в control plane |

## API решений

| Функция | Роль |
|---------|------|
| `classify_path(path)` | Категория поверхности (framework, manager, workspace_instance, …) |
| `can_edit_path(path, actor, action)` | Можно ли писать по пути (и нужна ли эскалация) |
| `should_prefer_workspace_patch(context)` | Предпочтение патча workspace |
| `can_trigger_self_improvement(context)` | Допустимы ли триггеры самоулучшения менеджера |
| `requires_human_escalation(context)` | Нужно ли участие человека |
| `load_default_policy()` | Загрузка из `default_policy.yaml` (при ошибке — встроенные дефолты) |
| `load_policy(path)` / `load_policy_from_file(path)` | Загрузка из произвольного YAML/TOML |

## Поведение `can_edit_path` (кратко)

- `gmas/**` — запрещено, эскалация.
- `ouroboros/**` — разрешено только в логике «менеджер может меняться», но решение помечается как требующее эскалации/уведомления человека.
- `workspaces/.../instances/**` и прочие инстансы — разрешено.
- `workspaces/agent_research` (seed) — запрещено без процесса promotion, эскалация.
- `deep_coding_tasks/**` — разрешено (репозиторные описания задач).
- `umbrella/**` — разрешено.

## Связь с документацией

Архитектурный контекст: [docs/architecture.md](../../docs/architecture.md) и [docs/technical-report/README.md](../../docs/technical-report/README.md). Этот пакет закрепляет те же границы в импортируемом коде.
