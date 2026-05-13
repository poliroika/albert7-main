# GMAS

GMAS — мультиагентный фреймворк, на котором строятся все workspace в Umbrella.
Он предоставляет движок графов агентов, execution runtime, инструменты, память
и систему callback-ов.

## Роль в Umbrella

GMAS — это vendor-like, read-only слой. Он выступает стандартным языком сборки
мультиагентных систем: любой workspace по умолчанию описывает свою логику средствами GMAS.

```
┌────────────────────────────────────────┐
│         Ouroboros (менеджер)            │
│  выбирает workspace, строит гипотезу   │
├────────────────────────────────────────┤
│        Umbrella (control-plane)          │
│  реестр, политика, рантайм, retrieval  │
├────────────────────────────────────────┤
│        Workspace (прикладной)          │
│  граф агентов, промпты, evals          │
├────────────────────────────────────────┤
│         GMAS (фреймворк)              │
│  раннер, шедулер, инструменты, память  │
└────────────────────────────────────────┘
```

## Правило read-only

Директория `gmas/` защищена политикой (`umbrella/policies/default_policy.yaml`):

```yaml
framework_boundary:
  gmas_readonly: true
  requires_human_approval: true
  framework_paths:
    - "gmas/"
```

Вызов `can_edit_path(Path("gmas/foo.py"))` вернёт `allowed=False` с эскалацией.
Это правило зафиксировано и в коде `umbrella/policies/engine.py`, где `gmas` — один из якорей
классификации путей.

Если возможностей GMAS не хватает, решение ищется через:

1. Конфигурацию и композицию вне `gmas/`.
2. Локальные helper-скрипты в workspace.
3. Адаптеры для доменных инструментов.
4. Thin-layer обёртки поверх GMAS API.

Прямое редактирование `gmas/` — это отдельное архитектурное решение, требующее
одобрения человека.

## Что предоставляет GMAS

| Компонент | Описание |
|-----------|----------|
| Графы агентов | `gmas.core.graph` — определение топологий и маршрутов |
| Execution runtime | `gmas.execution.runner` — запуск графов с состоянием |
| Шедулер | `gmas.execution.scheduler` — порядок обхода агентов |
| Инструменты | `gmas.tools` — web search, file search, MCP client, shell, computer use |
| Память | `gmas.utils.memory` — shared memory между агентами |
| Бюджет | `gmas.execution.budget` — контроль расхода токенов и денег |
| Callback-система | `gmas.callbacks` — события, метрики, хендлеры |
| Стриминг | `gmas.execution.streaming` — потоковая передача результатов |
| Auto-builder | `gmas.builder.auto_builder` — автоматическая сборка графов |

## Контракт использования в workspace

Каждый workspace, если это возможно, должен описывать свою мультиагентность средствами GMAS.

Разрешено поверх GMAS:

- Локальные helper-скрипты запуска.
- Адаптеры для доменных инструментов.
- Внешние eval harness-ы.
- Конвертеры артефактов.
- Workspace-specific policies.

Запрещено по умолчанию:

- Клонировать логику GMAS внутри workspace.
- Заменять раннер GMAS самодельной системой без веской причины.
- Редактировать `gmas/` ради одной задачи.

## Retrieval по GMAS

Umbrella индексирует код и документацию GMAS для того, чтобы менеджер мог грамотно
улучшать workspace. Retrieval реализован в `umbrella/retrieval/`.

### Источники индекса

Конфигурация из `default_policy.yaml`:

```yaml
gmas_retrieval:
  mode: bm25_first
  code_aware: true
  preferred_sources:
    - gmas/README.md
    - gmas/QUICKSTART.md
    - gmas/DOCUMENTATION.md
    - gmas/docs/
    - gmas/src/
    - gmas/examples/
```

### Стек поиска

`RetrievalService` (`umbrella/retrieval/service.py`) комбинирует несколько методов:

1. **Lexical (BM25)** — точные совпадения по символам, конфигам и API.
2. **Symbol index** — классы, функции, модули и инструменты GMAS.
3. **Docs index** — навигация по `gmas/docs/` через `mkdocs.yml`.
4. **Workspace usage index** — паттерны использования GMAS в существующих workspace.
5. **Retrieval cards** — компактные structured briefs с рекомендуемым паттерном, ключевыми
   символами и файлами.

### Контекст для Ouroboros

Модуль `umbrella/retrieval/gmas_context.py` предоставляет функцию `build_gmas_context()`,
которая по запросу возвращает набор retrieval-хитов с кодом и документацией, готовых
для прямого использования в контексте LLM.

## Документация GMAS

Внутренняя документация фреймворка находится в:

- `gmas/README.md` — обзор.
- `gmas/QUICKSTART.md` — быстрый старт.
- `gmas/DOCUMENTATION.md` — полная документация.
- `gmas/docs/` — структурированные руководства (getting started, user guide, API reference,
  examples, contributing).
