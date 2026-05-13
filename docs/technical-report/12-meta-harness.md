# Часть 12. Meta-Harness, кандидаты и promotion

[← Оглавление](README.md) · [← Часть 11](11-configuration.md) · [Далее: эксплуатация →](13-operations.md)

---

## 12.1 Зачем нужен внешний контур

Даже при исправном verification отдельного workspace остаётся вопрос **обобщения**: не сломали ли мы косвенно другие задачи или типовые harness-сценарии? Meta-Harness — это слой экспериментов над кандидатами изменений с файловым store и процедурой принятия решения о promotion.

План и философия (частично опережают код): [../meta-harness-improvement-plan.md](../meta-harness-improvement-plan.md).

## 12.2 Где лежит состояние

Рабочие файлы — под **`.umbrella/meta_harness/`** (конкретная схема каталогов определяется `umbrella/meta_harness/store.py` и связанными модулями). Это не замена git-истории, а **операционный журнал экспериментов**.

## 12.3 Связка с Ouroboros integration

`umbrella/control_plane/ouroboros_integration.py` умеет прикреплять **`candidate_id`** к результату прогона. При наличии кандидата непрерывный раннер (`run_ouroboros_self_improve.py`) может вызывать оценку на search set и `decide_candidate_promotion` **до** того, как изменения считаются достойными слияния в seed.

## 12.4 Что должно быть истиной для promotion

1. Локальный **verification** workspace прошёл обязательные шаги.
2. Решение Meta-Harness (где применимо) не заблокировано порогами качества на наборе сценариев.

Пропуск одного из уровней возвращает систему к модели «доверяем тексту модели», от которой архитектура уходит.

## 12.5 Entrypoints

- `run_meta_harness.py` в корне.
- `python -m umbrella.meta_harness` (CLI модуль).

---

Далее практическая эксплуатация: [13-operations.md](13-operations.md).
