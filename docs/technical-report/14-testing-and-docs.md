# Часть 14. Тестирование и сопровождение документации

[← Оглавление](README.md) · [← Часть 13](13-operations.md) · [Далее: Task Planner →](15-task-planner.md)

---

## 14.1 Запуск тестов

Из корня репозитория:

```bash
uv sync --extra dev
uv run pytest -q
```

Конфигурация `pyproject.toml` включает **`umbrella/tests`** и **`ouroboros/tests`** и добавляет `pythonpath` для пакета `ouroboros`. Если тесты не находят модуль менеджера, первым делом проверяют запуск из корня через `uv run`, а не голый `pytest` из произвольной cwd.

---

## 14.2 Таргетированные наборы

При работе с конкретной подсистемой экономят время:

=== "Umbrella"
    ```bash
    uv run pytest -q umbrella/tests/test_app_ouroboros.py
    uv run pytest -q umbrella/tests/test_web_bridge_harness.py
    uv run pytest -q umbrella/tests/test_harness_orchestrator.py
    uv run pytest -q umbrella/tests/test_verification.py
    ```

=== "Ouroboros"
    ```bash
    uv run pytest -q ouroboros/tests/
    uv run pytest -q ouroboros/tests/test_completion_gates.py
    ```

=== "Workspace news_cards_ai"
    ```bash
    uv run pytest -q workspaces/news_cards_ai/tests/
    ```

Точный список файлов меняется; ориентир — имена `test_*.py` рядом с кодом.

---

## 14.3 Описание ключевых наборов

### `ouroboros/tests/test_completion_gates.py`

Тесты **completion gates** из `ouroboros/ouroboros/tools/control.py`. Покрывают инварианты Tier 1.3 + Tier 3.1 + Tier 3.2 без запуска полного loop:

| Тест | Что проверяет |
|------|--------------|
| `test_check_discovery_gate_silent_when_subtask_is_not_domain_unknown` | Gate не срабатывает для обычных подзадач |
| `test_check_discovery_gate_blocks_domain_unknown_with_no_discovery` | Gate блокирует `domain_unknown` без discovery-вызовов |
| `test_check_discovery_gate_passes_after_any_discovery_call` | Gate пропускает после ≥1 discovery-вызова |
| `test_planner_discovery_gate_default_on` | Planner discovery gate включён по умолчанию |
| `test_behavior_evidence_warning_*` | Паттерны behavior evidence (pytest passed, exit 0, ...) |
| `test_validate_delivery_contract_*` | Валидация delivery_contract |
| `test_check_verify_evidence_gate_*` | Verify-evidence gate |

### `umbrella/tests/test_verification.py`

Тесты `umbrella/verification/` включая `source_policy.py`:

- Загрузка спецификации из `workspace.toml`.
- Source policy scanner — обнаружение mock-паттернов.
- Исключение мета-файлов из сканирования.

---

## 14.4 Что гонять перед релизом изменений

| Область правок | Минимальный набор |
|----------------|-------------------|
| `umbrella/verification/` | `test_verification.py` + `test_app_ouroboros.py` |
| `umbrella/web_bridge/` | web bridge tests, harness tests |
| `umbrella/control_plane/ouroboros_integration.py` | ouroboros integration tests, `test_app_ouroboros.py` |
| `ouroboros/` (loop, tools) | `ouroboros/tests/` |
| `ouroboros/ouroboros/tools/control.py` | `test_completion_gates.py` |
| `ouroboros/ouroboros/task_planner.py` | `ouroboros/tests/` + ручной smoke с `OUROBOROS_PLANNER_MODE=always` |

---

## 14.5 Поддержка документации

Правило одно: **если меняется поведение, видимое оператору или разработчику control-plane, меняется соответствующая глава** (`docs/technical-report/NN-*.md`). Оглавление в [README.md](README.md) обновляют при добавлении новых файлов.

**Новые модули → новые главы:**

- `ouroboros/ouroboros/task_planner.py` → [15-task-planner.md](15-task-planner.md)
- `umbrella/verification/source_policy.py` → раздел 9.4 в [09-verification.md](09-verification.md)
- `ouroboros/ouroboros/tools/control.py` → разделы 8.4, 15.4

---

## 14.6 Сборка документации

```bash
# Установить mkdocs-material
pip install mkdocs-material

# Локальный сервер с горячей перезагрузкой
mkdocs serve

# Сборка статики
mkdocs build
```

После успешной сборки сайт появляется в `public/`. CI-job `pages` в `.gitlab-ci.yml` выполняет ту же команду для GitLab Pages.

---

## 14.7 Версионирование документации

Техотчёт описывает **текущее** состояние ветки. Для исторических решений используют git blame и архив обсуждений PR; дублировать prose историю в техотчёте не обязательно.

---

[↑ В начало раздела](README.md)
