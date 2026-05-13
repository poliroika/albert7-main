# Часть 6. Umbrella: подсистемы control-plane

[← Оглавление](README.md) · [← Часть 5](05-architecture-flows.md) · [Далее: workspaces →](07-workspaces-and-policy.md)

---

## 6.1 Роль пакета `umbrella/`

Umbrella — это не «ещё один агент», а **инфраструктура доверия и запуска** вокруг workspace и менеджера. Ниже перечислены основные подпакеты и типичные причины туда заглянуть при отладке.

## 6.2 Policies (`umbrella/policies/`)

Отвечает на вопрос **куда автоматика имеет право писать** и какие области считаются чувствительными. Интеграция с остальным кодом идёт через движок политики (`engine.py`). При добавлении новых классов путей (например, новый вид sandbox) политику нужно обновлять сознательно, иначе получите либо ложные блокировки, либо дырку.

## 6.3 Workspace registry (`umbrella/workspace_registry/`)

Обнаружение workspace’ов на диске, их идентификаторы и метаданные для UI и CLI. Если новый каталог под `workspaces/` не появляется в списке bridge, первым делом проверяют соглашения об именовании и наличие ожидаемых файлов (`workspace.toml` и др. — см. [07-workspaces-and-policy.md](07-workspaces-and-policy.md)).

## 6.4 Workspace runtime (`umbrella/workspace_runtime/`)

Единый раннер запуска workspace на GMAS и сопутствующая логика **instance** (отделение мутабельной копии от seed). Это критический модуль для понимания «почему правки пошли не туда».

## 6.5 Artifacts (`umbrella/artifacts/`)

Индексация прогонов, связывание run-id с логами и производными структурами. Web UI страницы Runs/Memory опираются на этот слой и на сериализацию событий harness.

## 6.6 Retrieval (`umbrella/retrieval/`)

Поиск по кодовой базе и документации GMAS (BM25 + символы и пр.), чтобы менеджер и операторские инструменты не работали вслепую. Изменения в индексации затрагивают качество подсказок Ouroboros, но не должны ломать детерминизм verification.

## 6.7 Verification (`umbrella/verification/`)

Загрузка спецификации, исполнение шагов, агрегирование отчёта. Ключевые файлы:

| Файл | Роль |
|------|------|
| `spec_loader.py` | Разбор объявлений из `workspace.toml` / авто-детект |
| `runner.py` | Исполнение шагов в subprocess |
| `models.py` | Pydantic-структуры отчёта (VerificationStep, VerificationReport) |
| `skill_compliance.py` | Проверка соблюдения скилловых ограничений, базовые mock-паттерны |
| `source_policy.py` | **Новый:** сканирование исходников workspace на scaffold/placeholder-маркеры |

`source_policy.py` дополняет паттерны из `skill_compliance.py` новыми: numbered placeholders (`News 1`, `Point 2`), `lorem ipsum`, `placeholder.com`, маркеры нереализованного кода (`not implemented yet`, `phase N implement`). Бинарные файлы и мета-файлы (docs, `.memory/`, `TASK_MAIN.md`) исключены из сканирования.

Подробнее в [09-verification.md](09-verification.md).

## 6.8 Control plane (`umbrella/control_plane/`)

Сюда смонтирована **тяжёлая интеграция с Ouroboros**: синхронный запуск улучшения, сбор изменённых файлов git-диффами, взаимодействие с Meta-Harness-кандидатами, sandbox self-edit recovery. Файл `ouroboros_integration.py` — один из самых больших и важных в репозитории; правки здесь требуют регрессии по `umbrella/tests/test_app_ouroboros.py` и web harness тестам.

## 6.9 Integration (`umbrella/integration/`)

Мосты и локатор сервисов (`services.py`), launcher hooks, подготовка skills для workspace перед отправкой промпта в Ouroboros. Ошибки здесь часто проявляются как «в первой итерации не подтянулись skills».

## 6.10 Web bridge (`umbrella/web_bridge/`)

HTTP слой: `server.py` (точка входа `bridge`), `handler.py` (маршруты), `app.py` (бизнес-логика API и фоновые раны). См. [10-web-bridge.md](10-web-bridge.md).

## 6.11 Orchestration (`umbrella/orchestration/`)

Шаблоны задач, рендер промптов workspace/retry, статусы — связующий код между entrypoints и control_plane.

## 6.12 Harness (`umbrella/harness/`)

Оркестрация шагов для Web-запусков (таймлайн, remediation attempts, увязка с verification). Изменения синхронизировать с тестами `test_web_bridge_harness.py`.

---

Далее про контракт workspace: [07-workspaces-and-policy.md](07-workspaces-and-policy.md).
