# Технический отчёт Umbrella

Многостраничное описание устройства репозитория: границы модулей, потоки данных, конфигурация, Web bridge и эксплуатация. Документ рассчитан на инженера, который будет править код и сопровождать запуски, а не только читать архитектурный манифест.

---

## Как это открывается на GitHub и GitLab

Оба хостинга нормально рендерят Markdown «как страницы», если навигация строится на **отдельных файлах** и **относительных ссылках**:

| Где вы | Что делать |
|--------|------------|
| **GitHub** | Откройте каталог `docs/technical-report/` — сверху отобразится этот `README.md`. Дальше переходите по ссылкам из оглавления; каждая глава — свой файл. |
| **GitLab** | То же: в репозитории откройте `docs/technical-report/README.md` или дерево каталога — README подставится как вводная страница раздела. |
| **Локально** | Любой редактор с предпросмотром Markdown или `README` как точка входа. |

### GitHub Pages

Если в настройках репозитория включён **GitHub Pages** с источником «Deploy from a branch» и каталогом **`/docs`**, корнем сайта станет [`docs/README.md`](../README.md). Техотчёт при этом не теряется: добавьте в общий `docs/README.md` ссылку на этот раздел или открывайте напрямую URL вида  
`/docs/technical-report/README.md` в браузере репозитория — GitHub отрендерит Markdown как страницу файла.

Отдельный генератор (MkDocs, VitePress и т.д.) для этого техотчёта **не обязателен**: навигация уже работает через оглавление и относительные ссылки между `.md` файлами.

### GitLab Pages (готовый пайплайн)

В корне репозитория лежит **`.gitlab-ci.yml`**: job `pages` ставит **MkDocs Material**, собирает сайт в **`public/`** и публикует его как Pages.

- После успешного пайплайна на ветке по умолчанию адрес вида: **`https://<namespace>.gitlab.io/<project-name>/`** (точный URL — **Deploy → Pages** в проекте).
- Точка входа сайта: **[Главная](../README.md)** → оглавление, техотчёт с боковой навигацией и поиском.

Просмотр «как файлы» в GitLab по-прежнему доступен через **Repository → `docs/technical-report/README.md`**.

**Соглашение по ссылкам:** из глав раздела `technical-report/` соседние части линкуются как `./NN-slug.md`; на общую документацию `docs/` — как `../architecture.md` и т.д.

---

## Оглавление

| Часть | Файл | Содержание |
|-------|------|------------|
| 0 | [Настоящий файл](README.md) | Вводная, навигация по хостингам |
| 1 | [Цели, аудитория, термины](01-scope-terms.md) | Зачем отчёт, словарь, что сознательно не покрыто |
| 2 | [Контекст системы](02-system-context.md) | Роль workspace / GMAS / Umbrella / Ouroboros в одном нарративе |
| 3 | [Топология репозитория](03-repository-topology.md) | Каталоги, зависимости Python, точки входа |
| 4 | [Артефакты и `.umbrella/`](04-runtime-artifacts.md) | Что появляется на диске во время работы |
| 5 | [Слои и потоки данных](05-architecture-flows.md) | Последовательность от оператора до verification |
| 6 | [Umbrella: подсистемы](06-umbrella-subsystems.md) | Policies, registry, runtime, artifacts, retrieval, integration, source_policy |
| 7 | [Workspaces и политика изменений](07-workspaces-and-policy.md) | Seed, instance, контракты файлов |
| 8 | [Ouroboros как рантайм менеджера](08-ouroboros-runtime.md) | Цикл, Task Planner, completion gates, связка с Umbrella |
| 9 | [Verification](09-verification.md) | Спека, раннер, source policy scanner, retry, связь с promotion |
| 10 | [Web bridge и UI](10-web-bridge.md) | HTTP-сервер, маршруты API, фронт, dev/prod |
| 11 | [Конфигурация](11-configuration.md) | `.env`, CLI, переменные Web/Ouroboros |
| 12 | [Meta-Harness и promotion](12-meta-harness.md) | Внешний контур, кандидаты, оценка |
| 13 | [Эксплуатация](13-operations.md) | Типовые сценарии, логи, частые сбои |
| 14 | [Тесты и сопровождение документации](14-testing-and-docs.md) | Pytest, completion gates, что гонять при изменениях |
| 15 | [Adaptive Task Planner](15-task-planner.md) | Декомпозиция задач, completion gates, конфигурация плановщика |

---

## Быстрые ссылки на остальную документацию

- [Оглавление всех документов](../README.md)
- [Архитектура (диаграммы)](../architecture.md)
- [Workspaces](../workspaces.md)
- [Ouroboros (продуктовый угол)](../ouroboros.md)
- [Umbrella-слой](../umbrella-layer.md)

Если текст расходится с кодом, приоритет у кода и тестов; правки вносятся в соответствующую главу этого раздела.
