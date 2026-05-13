# Ouroboros

Ouroboros — это менеджер и оператор контуров улучшения в Umbrella. Он не является основным
исполнителем задачи: исполнение рождается в workspace на GMAS. Ouroboros выбирает workspace,
запускает его, анализирует результаты и строит гипотезы улучшения.

## Роль в системе

В целевой архитектуре Ouroboros мыслит так:

1. У меня есть команда из workspaces.
2. Каждый workspace решает свой класс задач на GMAS.
3. Я выбираю подходящий workspace, запускаю его, смотрю, где он слаб.
4. Я усиливаю workspace, пока он не начнёт решать задачу сам.
5. Если для этого мне самому не хватает памяти, поиска или инструментов, я временно
   улучшаю себя.
6. После self-improvement я возвращаюсь к работе с workspace, а не подменяю его собой.

Ключевой принцип: **workspace-first**. Self-improvement допустим только как средство
снова лучше улучшать workspaces.

## Два контура улучшения

### Контур A: улучшение workspaces (основной)

Это основной рабочий цикл. Ouroboros:

- Создаёт task-instance на основе seed workspace.
- Модифицирует графы, промпты, политики, эксперименты и модели внутри instance.
- Запускает и перезапускает workspace.
- Анализирует логи и eval-результаты.
- Сравнивает версии.
- Накапливает прикладную компетенцию.

Каждая итерация теперь **гейтится runtime verification**. После того как
Ouroboros заявил о завершении, Umbrella автоматически запускает шаги из
секции `[verification]` в `workspace.toml` (или авто-детект: `pytest
test_smoke.py` + HTTP-health на `web_server.py`). Если хоть один шаг
провален:

1. Итоговый статус итерации становится `failed_verification`, а не
   `complete`.
2. В prompt следующей попытки подмешивается секция *Previous Verification
   Failure* с точным отчётом по каждому failed step.
3. Promotion кандидата в seed заблокирован до тех пор, пока не будет
   `verified`.

Количеством попыток управляют флаги `--max-verify-retries` (по умолчанию **20**
у `umbrella/app_ouroboros.py` и `run_ouroboros_self_improve.py`; в Web UI дефолт
для старта рана задаётся в bridge и может переопределяться `OUROBOROS_WEB_MAX_VERIFY_RETRIES`);
флаг `--no-verify` возвращает старое поведение (promote по self-report).

### Контур B: self-improvement (вторичный)

Включается только по сигналу дефицита собственной способности. Триггеры
(из `default_policy.yaml`):

| Триггер | Порог |
|---------|-------|
| Повторяющиеся неудачи workspace-итераций | `min_repeated_failures: 3` |
| Стагнация без прогресса | `min_stalled_iterations: 5` |
| Низкий confidence retrieval по GMAS | `retrieval_confidence_threshold: 0.3` |

Self-improvement отвечает за:

- Улучшение системного промпта и BIBLE.
- Перестройку retrieval и контекстной сборки.
- Улучшение памяти и правил компрессии.
- Добавление инструментов менеджера.

Self-improvement **нельзя** запускать только потому, что проще переписать себя,
чем аккуратно улучшить workspace.

## Интеграция с Umbrella

Ouroboros интегрирован с Umbrella через три модуля:

### ouroboros_bridge.py

`umbrella/integration/ouroboros_bridge.py` синхронизирует контекст Umbrella в Ouroboros drive —
файловую систему, из которой Ouroboros читает задачи и знания.

Drive layout (создаётся под `.umbrella/ouroboros_drive/`):

```
.umbrella/ouroboros_drive/
    logs/           # события и логи
    memory/
        knowledge/  # lessons из Umbrella memory
    state/
        state.json  # текущее состояние (бюджет, drift)
    task_results/   # результаты выполненных задач
```

Bridge синхронизирует:

- Workspace-контекст (какой workspace активен, его TASK_MAIN).
- Lessons из Umbrella memory store.
- Budget state и метрики.

### ouroboros_integration.py

`umbrella/control_plane/ouroboros_integration.py` предоставляет функции делегирования:

- `create_ouroboros_self_improvement_task()` — сформировать задачу для асинхронного
  контура self-improvement через launcher.

- `run_ouroboros_improvement_sync()` — запустить синхронную итерацию улучшения
  и дождаться результата.

### ouroboros_launcher.py

`umbrella/integration/ouroboros_launcher.py` управляет процессом Ouroboros: запуск,
отправка задач, ожидание результатов. Поддерживает сессии **sandbox self-edit**
(временные правки `umbrella/` и `ouroboros/` с откатом после задачи — см.
`umbrella/control_plane/sandbox_self_edit.py`).

#### Восстановление осиротевших sandbox-стэшей

Sandbox self-edit использует `git stash push --include-untracked` для
бэкапа текущего состояния репозитория перед выполнением задачи. Если
`exit_sandbox` ловит конфликт при `git stash pop` (например, Ouroboros
нагенерировал файлы, пересекающиеся со стэшем), stash остаётся в списке,
а сессия помечается `rollback_ok: false` с описанием ошибки в
`.umbrella/sandbox_sessions/<id>.json`.

При следующем старте `enter_sandbox` (включая запуск
`umbrella/app_ouroboros.py`) автоматически вызывается
`recover_orphan_sandbox_stashes(repo_root)`, которая:

- находит в `git stash list` все записи с префиксом `umbrella-sandbox-`;
- если worktree чистый — применяет их через `git stash apply` (не `pop`),
  чтобы пользователь не терял данные, и пишет WARNING в лог;
- если worktree грязный — оставляет stash на месте с WARNING и рекомендует
  запустить `git stash list` + `git stash apply <idx>` вручную.

Stash никогда не дропается автоматически — удалять его из списка должен
пользователь после проверки содержимого: `git stash drop stash@{N}`.

## Инструменты Umbrella в Ouroboros

Реализация: `ouroboros/ouroboros/tools/umbrella_tools.py`. Менеджер получает доступ к
Umbrella-слою: GMAS retrieval и контекст, чтение/запуск workspace, метрики и логи,
запись в память и уроки, promotion между seed и instance. Дополнительно:

- `search_meta_harness_experience` — поиск по прошлым кандидатам и экспериментам.
- `inspect_candidate_trace` — просмотр trace выбранного кандидата Meta-Harness.
- `sandbox_self_edit` — оформленный вход во временные правки с откатом.

## Связь с Web UI

Операторский интерфейс: `uv run bridge` ([umbrella/web_bridge/](../umbrella/web_bridge/)); перед запуском — сборка UI в `web/` (см. [docs/README.md](README.md#запуск-web-bridge)) —
один процесс отдаёт собранный React из `web/build` и JSON API под префиксом `/api/*`
(workspaces, threads, runs, logs, memory, dashboard stats и т.д.). Перед запуском выполните `yarn build` в каталоге `web/`.

Старый встроенный HTML/JS dashboard (`umbrella/dashboard`) удалён; детали control plane по-прежнему в [umbrella-layer.md](umbrella-layer.md).

## Отличие от «исполнителя задачи»

В предыдущей модели Ouroboros решал задачу через прямую самомодификацию: он переписывал
собственный код, промпты и инструменты, создавая ad-hoc решение внутри себя.

В Umbrella Ouroboros — **руководитель**, а не исполнитель:

| Аспект | Старая модель | Umbrella |
|--------|---------------|---------|
| Где рождается решение | Внутри Ouroboros | Внутри workspace на GMAS |
| Основная зона изменений | `ouroboros/` | `workspaces/.../instances/` |
| Self-improvement | Первый ответ на задачу | Вторичный контур, по триггерам |
| Итоговый артефакт | Зависит от Ouroboros | Standalone workspace |

Итоговый workspace должен работать автономно, без рантайм-зависимости от Ouroboros.
Ouroboros нужен для построения, улучшения и эволюции, но конечный полезный артефакт
живёт в самом workspace.

## Главное отличие от Claude Code и чистого Ouroboros

### По сравнению с Claude Code

`Claude Code` — это сильный инструмент интерактивного редактирования, но не готовая теория того,
как должна эволюционировать прикладная AI-система в долгом цикле.

Идейные нововведения `Umbrella` по сравнению с таким режимом:

- **выделение product surface**: прикладной результат должен жить в `workspace`, а не оставаться побочным эффектом coding-session;
- **эволюция через seed/instance/promotion**: у системы есть формализованный путь от шаблона к эксперименту и обратно;
- **policy-aware развитие**: границы `gmas`, `ouroboros`, seed-workspaces и instances не держатся только в голове разработчика, а вынесены в policy engine;
- **memory as control-plane**: lessons, competency gaps и palace memory влияют на следующие решения;
- **artifact-first thinking**: успех определяется не только тем, что агент «смог поправить код», а тем, что появился автономный улучшенный workspace.

Поэтому `Umbrella` сильнее там, где нужен не разовый coding-assist, а накопительная система улучшения.

### По сравнению с чистым Ouroboros

Чистый `ouroboros` в этом монорепозитории остаётся отдельным агентом с собственным runtime,
prompt stack и инструментами, включая делегирование code edits через Claude Code CLI.

`Umbrella` меняет его роль:

- `Ouroboros` перестаёт быть главным местом, где «рождается» прикладное решение.
- Основная mutable-поверхность смещается из `ouroboros/` в `workspaces/.../instances/`.
- Self-improvement остаётся, но включается как вторичный контур по policy-триггерам.
- Появляется слой `umbrella/`, который добавляет registry, retrieval, memory, run index и policy.

Главные идейные сдвиги:

- **от self-first к workspace-first**: сначала улучшаем рабочую систему, а не менеджера;
- **от агента к портфелю workspaces**: менеджер работает не с одной собственной кодовой базой, а с набором специализированных исполнительных систем;
- **от неявной эволюции к формализованной**: есть seed profiles, lineage, promotion rules и evaluation evidence;
- **от интуитивного self-improvement к триггерному**: capability gaps и negative signals формализуют момент, когда стоит менять менеджера;
- **от “агент сделал задачу” к “собран reusable artifact”**: ценность переносится в автономный workspace.

Главная идея: не делать ещё одну самоизменяющуюся версию агента под задачу, а получать
отдельный полезный workspace, который потом можно запускать, сравнивать, архивировать и развивать независимо от менеджера.

## Операторские скрипты

Для запуска Ouroboros в режиме непрерывного улучшения используются скрипты из корня
репозитория:

- `umbrella/app_ouroboros.py` — актуальный single-run entrypoint в Ouroboros-first модели.
- `run_ouroboros_self_improve.py` — полный цикл: чтение TASK_MAIN, рендеринг промпта,
  итерации, auto-promotion в seed. Если итерация возвращает `candidate_id` Meta-Harness,
  promotion может быть **ограничен** результатом оценки на search set (см. модуль
  `umbrella/meta_harness/promotion.py`).
- `run_meta_harness.py` — внешний цикл Meta-Harness: предложение изменений harness,
  оценка кандидата, решение о promotion (см. [meta-harness-improvement-plan.md](meta-harness-improvement-plan.md)).

Примеры:

```powershell
uv run python run_meta_harness.py --workspace agent_research --iterations 5
uv run python run_meta_harness.py --experiment latest --resume
```

Подробнее: [creating-workspaces.md](creating-workspaces.md#способ-3-операторские-скрипты).
