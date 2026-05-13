# Часть 3. Топология репозитория

[← Оглавление](README.md) · [← Часть 2](02-system-context.md) · [Далее: артефакты →](04-runtime-artifacts.md)

---

## 3.1 Крупные блоки на диске

Каталог, в котором лежит `pyproject.toml`, — **корень репозитория** (имя папки на диске может быть любым, например старый клон `albert7/`; дальше в схеме это обозначено как `.`).

```
.
├── umbrella/              # Пакет control-plane (Python)
├── gmas/                  # Фреймворк GMAS (editable dependency, политика: не автопатчить)
├── ouroboros/             # Менеджер + тесты; pythonpath в pytest
├── workspaces/            # Прикладные workspace’ы
├── web/                   # React SPA (CRA + Craco)
├── docs/                  # Документация проекта + этот техотчёт
├── pyproject.toml         # Корневой пакет `umbrella`, scripts, pytest paths
├── run_ouroboros_self_improve.py
├── run_meta_harness.py
└── .umbrella/             # Создаётся рантаймом (см. `.gitignore`)
```

Корень репозитория одновременно является **рабочей директорией** для большинства команд: `uv run`, bridge по умолчанию резолвит пути относительно него (см. `--repo-root` в [10-web-bridge.md](10-web-bridge.md)).

## 3.2 Python-пакет и точки входа

Корневой `pyproject.toml` (в **корне репозитория**, не в `docs/`):

- **`[project.scripts]`**: консольная команда `bridge` → `umbrella.web_bridge.server:main`.
- **`testpaths`**: `umbrella/tests` и `ouroboros/tests`; **`pythonpath`** включает каталог `ouroboros`, чтобы импорты пакета менеджера работали из корня.

Запуск модулей обычно выглядит как `uv run python ...` или `uv run bridge`, чтобы не зависеть от ручной активации venv.

## 3.3 Связь с GMAS

В `[tool.uv.sources]` указан editable-путь на `gmas` как `frontier-ai-gmas`. Это означает: разработка идёт в одном дереве исходников, но **политика семантического разделения** сохраняется — изменения в `gmas/` должны проходить через осознанное ревью, а не через автоматический self-edit менеджера.

## 3.4 Фронтенд

`web/package.json` задаёт скрипты `yarn start` / `yarn build` (через craco). Сборка по умолчанию попадает в `web/build`; bridge ищет также `web/dist` ([10-web-bridge.md](10-web-bridge.md)).

## 3.5 Скрипты верхнего уровня

| Файл | Назначение |
|------|------------|
| `umbrella/app_ouroboros.py` | Разовый запуск Ouroboros по workspace с verification и retry |
| `run_ouroboros_self_improve.py` | Непрерывный цикл по `TASK_MAIN.md`, promotion, интеграция Meta-Harness |
| `run_meta_harness.py` | Внешний контур Meta-Harness |

Детали конфигурации CLI: [11-configuration.md](11-configuration.md).

---

Далее: что появляется на диске при работе — [04-runtime-artifacts.md](04-runtime-artifacts.md).
