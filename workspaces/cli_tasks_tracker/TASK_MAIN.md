# TASK: CLI Tasks Tracker

## Goal
Сделай CLI-приложение для управления личным списком задач с приоритетами, тегами и
JSON-хранилищем. Никакого UI, никакой сети. Чистый Python-CLI.

## Functional Requirements

### Модель задачи
- `id`: целое число, авто-инкремент.
- `title`: непустая строка.
- `priority`: один из `low | medium | high`.
- `tags`: список строк, опционально.
- `created_at`: ISO-8601 UTC, выставляется автоматически.
- `done_at`: ISO-8601 UTC или `None` (по умолчанию `None`).

### Команды CLI
- `add "<title>" [--priority low|medium|high] [--tag <name>...]`
  - создаёт задачу, печатает её id.
- `list [--priority ...] [--tag <name>] [--done true|false]`
  - печатает задачи в табличном текстовом виде.
- `done <id>`
  - помечает задачу выполненной (выставляет `done_at`).
- `undo <id>`
  - снимает отметку выполнения (`done_at = None`).
- `search "<query>"`
  - поиск по подстроке в `title` (регистронезависимо).
- `stats`
  - выводит:
    - всего задач,
    - сколько выполнено,
    - сколько по каждому приоритету,
    - топ-5 тегов по числу задач.

### Хранение
- JSON-файл в воркспейсе: `data/tasks.json`.
- Атомарная запись (временный файл + `os.replace`).
- При первом запуске файл создаётся автоматически.

### Поведение
- Если ID не существует — `done` / `undo` печатают понятную ошибку и завершаются с `exit code != 0`.
- `add` без `--priority` использует `medium`.
- Допустимые значения `--done`: `true`/`false` (любой регистр).

## Technical Requirements
- Python 3.11+
- Стандартная библиотека достаточна (`argparse`, `dataclasses`, `json`, `pathlib`, `datetime`).
- Архитектура минимум из 4 модулей:
  - модель,
  - хранилище,
  - сервисный слой (бизнес-логика),
  - CLI-слой.
- Без TODO / `pass` / placeholder-заглушек — код должен реально работать.

## Project Layout
```
main.py
tracker/__init__.py
tracker/models.py
tracker/storage.py
tracker/service.py
tracker/cli.py
requirements.txt
README.md
tests/__init__.py
tests/test_models.py
tests/test_storage.py
tests/test_service.py
tests/test_cli_smoke.py
```

## Run / Verify
- Запуск:
  - `python main.py add "Купить молоко" --priority high --tag shopping`
  - `python main.py list`
  - `python main.py done 1`
  - `python main.py stats`
- Тесты:
  - `python -m pytest tests -q`
- Дополнительно:
  - `python -c "import main"`
  - `python -m compileall -q .`

## Definition of Done
- Все файлы из `Project Layout` существуют.
- `python -m pytest tests -q` проходит без падений.
- `python -c "import main"` завершается с exit code 0.
- `python -m compileall -q .` завершается с exit code 0.
- CLI-команды реально работают на свежем воркспейсе:
  - `add`, `list`, `done`, `undo`, `search`, `stats`.
- README содержит:
  - краткое описание;
  - как установить (упоминание venv / requirements);
  - как запустить;
  - примеры команд для всех сценариев;
  - как прогнать тесты.
