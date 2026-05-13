# Часть 5. Слои и потоки данных

[← Оглавление](README.md) · [← Часть 4](04-runtime-artifacts.md) · [Далее: подсистемы Umbrella →](06-umbrella-subsystems.md)

---

## 5.1 Три слоя плюс операционная оболочка

Диаграммы уровня «кто к кому стрелочкой» есть в [../architecture.md](../architecture.md). Здесь — **поток решения** в терминах действий:

1. **Оператор** задаёт workspace и текст задачи (или принимает дефолт из `TASK_MAIN.md`).
2. **Umbrella** проверяет политику путей, резолвит instance/seed, поднимает конфиг рантайма (бюджет, таймауты, quality threshold), пишет статус для UI.
3. **Ouroboros** в цикле вызывает инструменты Umbrella: чтение/запись в пределах workspace, запуск тестов через обвязку, retrieval, работу с памятью и т.д.
4. **GMAS** внутри workspace исполняет конкретный граф агентов — это «дно» стека исполнения прикладной логики.
5. По завершении попытки **verification** прогоняет объявленные шаги и формирует отчёт.
6. При провале Umbrella формирует **retry-контекст** (предыдущий отчёт verification + статус) и отдаёт его в следующую попытку Ouroboros — без этого цикл превращался бы в бессмысленные повторы.

## 5.2 Сквозная трассировка: CLI `app_ouroboros`

Упрощённая цепочка модулей:

`umbrella/app_ouroboros.py` → загрузка `.env`, резолв workspace → `write_status` → цикл попыток → внутри попытки подготовка skills (`prepare_active_skills_for_workspace`), сбор промпта (`render_workspace_prompt`), вызов **`run_ouroboros_improvement_sync`** из `umbrella/control_plane/ouroboros_integration.py` → после выхода verification и интерпретация статуса.

Точные флаги CLI описаны в [11-configuration.md](11-configuration.md).

## 5.3 Сквозная трассировка: Web UI

Пользователь в Chat/Runs инициирует действие → `POST /api/runs` (и родственные эндпоинты) в `umbrella/web_bridge/handler.py` → логика в `WebBridgeApp` (`umbrella/web_bridge/app.py`) порождает фонового воркера с тем же семейством вызовов control-plane, что и CLI, увязанным с run-id для таймлайна и отмены.

Детали API: [10-web-bridge.md](10-web-bridge.md).

## 5.4 Где разрыв между «идеей» и «фактом»

Граница проходит между:

- **заявлением агента о завершении** (текст, статус в результате итерации);
- **таблицей результатов verification** (passed/failed по именованным шагам).

Promotion изменений в seed и доверие со стороны Meta-Harness завязаны на успешное прохождение этого факта, а не на красивый финальный абзац.

---

Далее детальный разбор пакета `umbrella/`: [06-umbrella-subsystems.md](06-umbrella-subsystems.md).
