# Ouroboros / Umbrella Civilization Calibration Runbook

Этот документ можно целиком вставлять в новый чат. Он задает общий промпт и рабочий порядок для следующего агента: как запускать calibration run, что смотреть в логах/памяти, каких параллельных агентов подключать и как чинить систему без одноразовых костылей.

Главная цель: довести Ouroboros под управлением Umbrella до состояния, где реальный запуск через Web UI способен достаточно долго работать без ручных правок workspace и собрать большой проект с нуля до финальной проверки.

## 1. Ментальная Модель

Ouroboros - это один deep agent: он получает конкретную задачу от Umbrella, исследует, планирует, пишет файлы, запускает проверки, исправляет ошибки и завершает работу в рамках своего run context.

Umbrella - это контрольная панель и control plane вокруг deep agent:

- Web UI, кнопки запуска/остановки, выбор workspace/task/model/round budgets.
- Orchestrator, phase runner, watcher, review phases, verify retries.
- Tool surface, фазовые tool policies, validators, schemas.
- Memory layer, research findings, phase artifacts, provenance, logs.
- Возможность запускать и контролировать копии Ouroboros, но не смешивать их состояние в одном workspace.

Исправления должны по возможности усиливать Umbrella-level контракты, а не добавлять одноразовые проверки под `civilization`. Если найден плохой результат, надо найти, почему Umbrella/Ouroboros его допустили, и исправить слой, который должен был это предотвратить. Важно помнить: Umbrella поднимает deep-agent runs для фаз, watcher/review и verify, выдает им tool surface, память, prompts и runtime env. Поэтому Umbrella должна оставаться универсальным менеджером, к которому позже можно подключить не только Ouroboros, но и другой deep agent, например Hermes.

## 2. Активная Цель

Рабочая папка:

```text
C:\Users\poliroika\Documents\albert7
```

Целевой workspace:

```text
C:\Users\poliroika\Documents\albert7\workspaces\civilization
```

Файл задачи, который должен оставаться после cleanup:

```text
C:\Users\poliroika\Documents\albert7\workspaces\civilization\TASK_MAIN.md
```

Web UI:

```text
http://127.0.0.1:8780/chat
```

Buglog calibration loop:

```text
C:\Users\poliroika\Documents\albert7\docs\civilization-calibration-buglog.md
```

Перед любыми новыми правками обязательно прочитать buglog и понять, это регрессия старой ошибки или новый класс бага.

Этот runbook можно дополнять, если новые выводы меняют calibration policy, но нельзя ломать его главную идею: Umbrella должна концептуально управлять deep agents, фазами, tool context, памятью, watcher и verify. Периодически перечитывай этот файл, чтобы не терять цель в локальных правках.

## 3. Негласный Контракт С Пользователем

Пиши пользователю по-русски, коротко и регулярно: что сейчас смотришь, что понял, что собираешься менять.

Не делай вид, что "еще один guard" решит архитектурную проблему. Если баг повторяется в разных формах, думай уровнем выше: DomainPolicy, Contract/Evidence Gate, MemoryWriteService, ReviewBundleBuilder, фазовые prompt/tool contracts, watcher, memory hierarchy.

Если изменения занимают долго, не молчи. Дай статус: какая фаза, какой run id, какие последние tool errors, что проверяешь.

Перед правкой сначала разберись, что именно получает deep agent на фазе: system prompt, phase prompt, loaded memory, search snippets, tool schemas, tool policy, watcher bundle, DomainPolicy и предыдущие artifacts. Часто правильное исправление - не новый запрет, а изменение того, какой контекст Umbrella подает агенту.

Если проблема концептуальная или повторяется в разных формах, запускай Conceptual Research Agent: пусть ищет в статьях, проектах, MCP/tool ecosystems и agent-orchestration практиках идейное решение. Новые идеи приветствуются, если они усиливают общий концепт и не превращаются в hardcoding под один run.

Во время live run полезно держать двух read-only аудиторов: один смотрит фазовый прогресс и tool errors, второй смотрит память/provenance/evidence. Их задача - не чинить workspace руками, а быстрее находить, почему система не может сама достроить проект.

Обращайся к `docs/` проекта, чтобы понимать исходную архитектурную идею Umbrella/Ouroboros/GMAS/workspaces. Если код выглядит перегруженным, запускай отдельного Codebase Refactor Agent: он читает buglog и документацию, ищет лишний код, дубли, hardcoded guards и места, которые лучше вынести в policies/prompts/services. Он может править только product code outside `workspaces/civilization`, в bounded scope, с regression tests и без отката чужих изменений.

## 4. Non-Negotiable Rules

1. Не редактировать вручную generated files в `workspaces/civilization`.
2. Читать generated files, logs, memory, plans и tests можно.
3. Если generated result плохой, выяснить, почему система это позволила.
4. Исправлять только main product code, prompts, tools, schemas, validators, tests, Umbrella orchestration или memory/review infrastructure и похожее.
5. После каждого изменения main code или prompt:
   - остановить текущий bridge/run,
   - удалить все внутри `workspaces/civilization`, включая `.memory`,
   - оставить только `TASK_MAIN.md`,
   - перезапустить bridge,
   - снова кликнуть task через Web UI.
6. Никакого hardcoding под `civilization`, brittle workarounds и одноразовых patches.
7. Если старый код стал не нужен, удалить или отрефакторить, а не наслаивать дубликаты.
8. Предпочитать Umbrella-level контракты, потому что Umbrella - точка входа, которая управляет deep agents. Ouroboros - текущий deep agent; Umbrella - control plane. На месте Ouroboros позже может быть другой агент, например Hermes, поэтому контракты должны быть универсальными.
9. Каждый баг из real LLM/Web UI run должен получить regression test на captured payload/log shape.
10. Финальная confidence check - только реальный Web UI button run с inherited LLM env, не локальная fake/CLI симуляция.
11. Прогоны делать с round budget `0` там, где UI/worker трактует `0` как unlimited. Если UI показывает конкретное поле, выставить `0` перед кликом task.
12. Не запускать несколько live Web UI runs в один и тот же `workspaces/civilization` одновременно: память, state и логи смешаются. Параллельность делать через read-only аудиторов или отдельные cloned workspaces/ports.

## 5. Что Сейчас Считается Хорошим Направлением

Не продолжать бесконечно расширять локальные regex guard piles. После contract/evidence refactor hard decisions должны идти через typed contracts, supervisor/verifier evidence, AST/static analyzers и `PhaseDecisionEngine`. Regex допустим только для синтаксических задач: paths/globs/env names/URL/file names/low-level token parsing.

Нужны пять концептуальных ядер:

### DomainPolicy

Один слой, который определяет домен задачи и фазовые требования:

- Нужен ли GMAS context: только если задача про LLM, агентов, multi-agent, bots, agent orchestration, autonomous decision making или GMAS-like systems.
- Какие LLM env aliases разрешены в generated workspace как публичный контракт:
  - `LLM_API_KEY`
  - `LLM_BASE_URL`
  - `LLM_MODEL`
- Какие host/control-plane aliases Umbrella может принять на входе и перед запуском workspace-команд нормализовать в публичные `LLM_*`:
  - `OUROBOROS_LLM_API_KEY` -> `LLM_API_KEY`
  - `OUROBOROS_LLM_BASE_URL` -> `LLM_BASE_URL`
  - `OUROBOROS_MODEL` -> `LLM_MODEL`
- Какие aliases запрещены или являются typos:
  - `OPENAI_API_KEY` как universal workspace LLM credential.
  - `OUROBOROS_LLM_MODEL`; в generated workspace правильно `LLM_MODEL`, а host bridge при необходимости использует `OUROBOROS_MODEL`.
  - `LL_BASE_URL`; правильно `LLM_BASE_URL`.
- Какие tools должны быть доступны на фазе.
- Какие prompts/preludes нужно подгружать в зависимости от домена.

DomainPolicy не должен быть набором строковых костылей в разных файлах. Это должен быть общий источник истины для prompts, validators, runner, watcher и write guards.

### Contract / Evidence Gate

Один слой для связи "typed tool output -> contract bundle -> validators -> evidence refs -> phase decision":

- `umbrella/contracts/models.py` задает `ContractEnvelope`, `EvidenceRef`, `ContractIssue`, `ProofSpec`, `PlanIR`, `ReviewContract`, `CompletionContract`, `TaskRiskProfile`, `ContractBundle` и `VerificationReportRef`.
- `ContractCompiler` собирает persisted run artifacts/tool events/ledger в `ContractBundle`.
- `ContractValidator` проверяет schema version, proof execution/oracle/scope/anti-gaming, research evidence, completion claims, verification reports, stale/fake refs and task risk.
- `PhaseDecisionEngine` решает `continue`, `loop_back`, `abort`, `verify` или `human_checkpoint` по typed issues, а не по словам в notes.
- `EvidenceRef` не строка: он знает `ref_type`, `ref_id`, `hash`, `produced_by`, `phase`, `subtask_id`, `created_after_event`.
- Валидная JSON/typed структура не считается доказательством. Истина - только supervisor/verifier/harness evidence, связанное с текущим `workspace_hash`/`diff_hash`.
- Старые строковые proof fields (`success_test`) и text-only review revisions больше не являются supported core path. Новый план обязан иметь typed `proof`.

Plan leaf теперь ссылается на declared files and typed `ProofSpec`, а `mark_subtask_complete` принимает `CompletionContract` с fresh `EvidenceRef[]` и completed claims. Final verify принимает `VerificationReportRef`, который должен совпасть с ledger/hash/current workspace/diff.

### MemoryWriteService / Trust Ladder

Один canonical путь записи памяти:

- Запись знает `canonical_id`, `aliases`, `kind`, `tier`, `scope`, `phase`, `task_id`, `run_id`.
- Запись знает provenance: из какого tool call, source id, artifact id, command result.
- Memory promotion явный: `agent_claim` -> `observed_artifact` -> `public_verified` -> `mutation_verified` / `hidden_verified` / `adversarial_verified`.
- Durable memory требует verified-or-higher trust и typed `EvidenceRef`; agent diary не может стать durable lesson.
- `contradicted` и `retracted` память не удалять молча: хранить как avoid-pattern/provenance, чтобы система училась на провалах.
- `palace_add`, `palace_search`, phase recall, watcher recall, UI memory graph должны читать/писать через один контракт.

Смотреть отдельно: что пишется, когда пишется, что читается, откуда читается, что чистится при cleanup, что поднимается выше по иерархии и почему.

### ReviewBundleBuilder

Один слой, который собирает watcher/review контекст:

- active subtask card,
- declared files,
- changed files,
- typed `ProofSpec`,
- latest supervisor/verifier evidence refs,
- latest tool errors,
- relevant memory,
- DomainPolicy flags,
- ContractBundle/ContractIssue context,
- accepted plan mutations,
- previous watcher verdicts.

Watcher и review phases должны получать не разрозненный prompt, а структурированный bundle. `submit_micro_review` принимает typed `issues`; notes могут быть на любом языке и не влияют на machine decision.

### Phase-Aware Anti-Gaming Architecture

Goodhart risk является частью основной модели: если видимый сигнал успеха - только "green tests", deep agent может оптимизировать тесты, verifier, логи, fixtures или shell path вместо реального поведения. Это не чинится одним prompt запретом. Umbrella должна разделять candidate workspace, evaluator, ledger и capabilities на уровне фаз.

Эти компоненты должны оставаться Umbrella-level и подгружаться/применяться фазово:

- `CapabilityKernel`: phase-specific profiles. Research read-only; plan writes only plan artifacts; execute writes only active declared source/test/doc paths; watcher/review read source and command evidence; verify не меняет product code; self-improve/product-code fixes отдельны от generated workspace.
- `ImmutableEvaluator` / `VerifierGate`: verifier, hidden/public probes, acceptance manifest and final checks live outside candidate workspace or are mounted read-only. For code tasks, missing verifier/probe coverage is fail unless DomainPolicy says docs-only or explicitly non-code.
- `SupervisorLedger`: tool calls, phase signals, verifier results, acceptance records and required-tool evidence are written only by supervisor. Agent-visible write tools must not write logs/control/verification paths. Long-term target: hash-chained events with actor, previous hash, args/result hashes, touched files and exit code.
- `CompletionContract`: each patch/subtask completion carries completed claims, changed files, typed evidence refs, and fresh verification/probe references. Contract validators check claim -> proof -> changed code, not just "pytest green".
- `AntiGamingScanner`: diff-aware scanner for suspicious changes. Python/JS/TS test tampering should be AST/static-analyzer based where possible; text placeholder scans are advisory unless backed by behavioral verifier failure.
- `MutationMetamorphicGate`: for risky production changes, run cheap mutation/property/metamorphic probes on changed behavior. If mutations survive or invariants are not covered, require stronger tests/evidence instead of accepting cosmetic green checks.
- `BreakageAgent` / adversarial reviewer: separate reviewer tries to find counterexamples, hidden tests and hardcoding after solver passes normal checks. It is not the same agent that wrote the patch.
- `CheatBenchmark`: keep calibration tasks where the easiest path is to cheat, and measure visible pass, hidden pass, stub rate, test deletion, verifier tampering, mutation survival and rollback.

Важно: это не должно ломать идею phased Umbrella. Каждый phase получает свой prompt, loaded memory, tool policy, DomainPolicy, ContractBundle slice, capability profile, evaluator access and review bundle. Execute может работать внутри candidate workspace, но verify/review/evaluator должны быть технически отделены от того, что execute способен менять.

Конкретное направление для Umbrella internals:

- `umbrella/enforcement/kernel.py`: единый enforcement path для `apply_workspace_patch`, delete/write tools, shell, terminal sessions, `sandbox_self_edit`, `claude_code_edit`, MCP/temp tools и любых записей в `.memory`, logs, verification, policies. После каждого tool call kernel снимает filesystem diff и проверяет phase, tool, actor, allowed paths, write kind, mutation count, touches tests/verifier/logs/policies/hidden evaluator.
- Immutable verification layout: `candidate workspace` изменяется агентом; `evaluator workspace` не изменяется агентом; `supervisor ledger` не writable из workspace tools; final verification runs in a clean/fresh env by applying candidate diff to clean checkout, mounting evaluator read-only, then writing a signed report to supervisor ledger.
- `umbrella/contracts/`: central contract/evidence layer. `runner.py` should orchestrate by calling `ContractCompiler`, `ContractValidator`, and `PhaseDecisionEngine`, not by parsing free text.
- `umbrella/analysis/python_tests.py`, `umbrella/analysis/js_tests.py`, `umbrella/analysis/shell_commands.py`, `umbrella/verification/test_tamper.py` and `umbrella/verification/diff_policy.py`: separate gates for deletion/weakening tests, `pytest.skip`/`xfail`, `assert True`, weak assertions, early return, broad `try/except: pass`, fixture changes, target-behavior mocks/monkeypatches, snapshot rewrites, `|| true`, `exit 0`, `check=False`, verifier/fixture/policy edits, and shell masking.
- Mutation smoke for changed production files: cheap mutations such as `>` -> `>=`, `and` -> `or`, `True` -> `False`, constant changes, branch removal, return changes, exception-condition changes. Surviving mutations should block code-task acceptance or require stronger evidence.
- Post-patch metamorphic probes: generate hidden probes after implementation, not all before. Standard probes include input sensitivity, round-trip, idempotency, monotonicity, boundary cases, randomized inputs, differential checks against previous stable/reference behavior, and negative cases. Existing `INPUT_SENSITIVITY` should become a reusable standard check for stub-prone tasks.
- Hash-chained `SupervisorLedger`: events include `event_id`, `prev_hash`, `actor`, `phase`, `tool`, `args_hash`, `result_hash`, `touched_files`, timestamp and signature. Agent may read summaries, but not write ledger records.
- Evidence-bound memory: durable memory requires typed `EvidenceRef` and verified-or-higher trust; manager lessons require verified runs; competency gaps require repeated signed failures; self-improvement triggers require supervisor-confirmed evidence. Memory must be a proof base, not an agent diary.
- `sandbox_self_edit` policy must be consistent. Safe default: rollback on task end. Persistent self-edit requires explicit human approval, signed diff and regression verify. If a no-rollback mode exists, it belongs only to explicit persistent self-improvement.
- PhaseRunner should stay an orchestration layer over auditable components: `PhaseStateMachine`, `PhaseManifestGuard`, `CapabilityKernel`, `ContractCompiler`, `ContractValidator`, `PhaseDecisionEngine`, `GmasPreludeProvider`, `CompletionGate`, `VerificationGate`, `AntiGamingGate`, `LoopbackController`.

Implementation policy: do this in bounded slices with regression tests and without code bloat. First prefer one narrow enforcement boundary that closes a real bypass from logs, then extract shared policy into the kernel. Do not rewrite the whole runner unless a small slice first proves the boundary and preserves current behavior.

Current implemented slice to preserve and extend:

- `umbrella/enforcement/kernel.py` and `umbrella/enforcement/ledger.py` exist for workspace write/shell/self-edit enforcement and supervisor ledger events.
- `umbrella/contracts/` is the central contract layer: typed `ProofSpec`, `EvidenceRef`, `PlanIR`, `ReviewContract`, `CompletionContract`, `VerificationReportRef`, `ContractBundle`, `ContractValidator`, `ContractCompiler`, `PhaseDecisionEngine`.
- `umbrella/analysis/` provides Python/JS/TS/shell analyzers; `umbrella/verification/diff_policy.py`, `umbrella/verification/test_tamper.py`, and `umbrella/verification/mutation_smoke.py` exist for anti-gaming verification.
- Phase-plan validation now rejects obsolete string proof fields such as `success_test`; executable leaves must own typed `proof` objects with execution/oracle/scope/anti-gaming fields.
- Phase-plan path/scope policy treats candidate workspace boundaries as capability boundaries: parent paths, `.git`, host/control-plane paths, and generated-workspace mutation of `workspace.toml`, `verification.toml`, or `verify.sh` are plan blockers. These belong to supervisor/evaluator policy, not generated project implementation.
- `umbrella/deep_agent_tools/memory.py` has evidence-bound durable memory policy with typed `EvidenceRef` and trust levels.
- `umbrella/deep_agent_tools/research_provenance.py` is the shared ResearchProvenance/SourceEvidenceContract for source handles, usable result checks, GMAS fallback/confidence checks, schema text, explicit research-summary `Source:` label binding, and research-summary repair hints. Extend this module instead of adding local provenance parsers in `phase_contract_handlers.py`, `phase_control_research.py`, prompts, or schema text.
- `palace_add(kind="research_finding")` requires concrete current source provenance. Bare result-bearing tool ids such as `github_project_search`, `mcp_discover`, `web_search`, and `deep_search` are too broad; use `github:owner/repo` or a tool-qualified source like `github_project_search:<exact query>`, `mcp_discover:<exact query>`, `web_search:<exact query>`, or `deep_search:<intent-or-query>` only when that logged result has non-empty results. Fallback or low-confidence GMAS retrieval, including tool-qualified `get_gmas_context:<query>` / `search_gmas_knowledge:<query>`, must be observation/lead memory, not verified research finding. Do not assume `result_preview` is parseable JSON; truncated previews still need raw-text checks for `metadata.fallback=true` and `confidence`.
- `umbrella/prompts/phases/research.system.md`, `palace_add` schema text, and validator error feedback must stay aligned with the same source contract: do not tell the agent that bare result-bearing tools or preflight-only calls are valid counted-finding provenance.
- `CompletionGate` and `VerifierGate` must keep different scopes. A bounded execute leaf closes only with a valid `CompletionContract` and fresh evidence refs after the last relevant write. Global safety/evaluator failures (`source_policy`, anti-gaming, mutation, verifier/evaluator tamper) still block immediately and cannot be deferred.
- `request_watcher_review` must treat repeated red `run_workspace_verify` plus rejected `mark_subtask_complete` as semantic deadlock evidence even when local tests are green. `review_not_required` is wrong once the active leaf is trapped by completion/verify disagreement.
- `loop_back_to` is only for current-or-earlier phases. Forward jumps such as execute -> verify are state-machine bugs. A newly submitted phase plan invalidates stale `plan_review` and downstream execute/final/verify state until review runs again.
- Same-path full source replacement must not erase most exported symbols/classes behind a generic `validation_summary`. Large source contract loss belongs behind a real plan/review boundary, not a hunk-mismatch shortcut.
- `web_search` must use the GMAS WebSearchTool provider stack. DuckDuckGo is the default provider and does not require an API key. Optional providers such as Brave, Serper, Tavily, Exa, SearXNG, Bocha, or Google can be configured, but `OPENAI_API_KEY` must not control generic internet access. `deep_search` must be a pluggable engine boundary: GMAS stack plus Playwright/page-reading is the no-key baseline; stronger hosted engines such as Firecrawl (`FIRECRAWL_API_KEY`) or Jina Reader Search (`JINA_API_KEY` optional/when available) can be selected via `OUROBOROS_DEEP_SEARCH_ENGINE` without becoming required.
- `sandbox_self_edit` rolls back by default, and Claude CLI must not fall back to dangerous skip-permissions mode.

## 6. Базовый Операционный Цикл

1. Прочитать `docs/civilization-calibration-buglog.md`.
2. Проверить, не висит ли bridge/run на порту `8780`.
3. Если workspace не чистый, остановить bridge и очистить workspace до одного `TASK_MAIN.md`.
4. Поднять/проверить постоянных sidecar agents для этого прогона: Run Monitor, Evidence/Memory Auditor, Plan Contract Reviewer, Execute/Watcher Auditor, Conceptual Research Agent и Codebase Refactor Agent. Первые четыре read-only смотрят live run; Research/Refactor смотрят весь проект и архитектурные причины.
5. Запустить bridge.
6. Через in-app Browser открыть Web UI, выбрать `civilization`, выставить rounds `0`/unlimited, кликнуть `TASK_MAIN.md`.
7. Мониторить logs/state/memory/ledger/evaluator gates, не редактируя workspace.
8. Если найден баг:
   - зафиксировать shape в buglog,
   - скопировать captured payload из logs/artifacts,
   - написать минимальный regression test,
   - исправить conceptual/product layer,
   - прогнать focused tests и affected suite,
   - stop/clean/restart/click again.
9. Повторять до чистого прохождения research -> research_review -> plan -> plan_review -> execute -> watcher/subtask_review -> final_review -> verify.

## 7. Process Control Commands

### Stop Bridge And Run Processes

```powershell
$ErrorActionPreference = 'Continue'
$targets = @()
$targets += @(Get-CimInstance Win32_Process | Where-Object {
    ($_.CommandLine -match 'bridge(\.exe)?') -and ($_.CommandLine -match '--port\s+8780')
})
$targets = @($targets | Sort-Object ProcessId -Unique)
foreach ($proc in $targets) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
$listeners = @(Get-NetTCPConnection -LocalPort 8780 -State Listen -ErrorAction SilentlyContinue)
$bridges = @(Get-CimInstance Win32_Process | Where-Object {
    ($_.CommandLine -match 'bridge(\.exe)?') -and ($_.CommandLine -match '--port\s+8780')
})
[pscustomobject]@{ listeners=$listeners; bridges=$bridges } | ConvertTo-Json -Depth 6
```

### Clean Workspace To Only TASK_MAIN.md

```powershell
$ErrorActionPreference = 'Stop'
$listeners = @(Get-NetTCPConnection -LocalPort 8780 -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    throw "Port 8780 still has listeners before cleanup: $($listeners | ConvertTo-Json -Compress)"
}

$expected = 'C:\Users\poliroika\Documents\albert7\workspaces\civilization'
$target = Resolve-Path $expected -ErrorAction Stop
if ($target.Path -ne $expected) {
    throw "Refusing to clean unexpected path: $($target.Path)"
}

$taskMain = Join-Path $target.Path 'TASK_MAIN.md'
if (-not (Test-Path -LiteralPath $taskMain -PathType Leaf)) {
    throw 'Refusing to clean: TASK_MAIN.md is missing'
}

Get-ChildItem -LiteralPath $target.Path -Force |
    Where-Object { $_.Name -ne 'TASK_MAIN.md' } |
    ForEach-Object {
        $full = $_.FullName
        if (-not $full.StartsWith($target.Path + [System.IO.Path]::DirectorySeparatorChar)) {
            throw "Refusing to delete outside workspace: $full"
        }
        Remove-Item -LiteralPath $full -Recurse -Force
    }

Get-ChildItem -LiteralPath $target.Path -Force | Select-Object Name,Mode,Length
```

### Start Bridge

```powershell
$ErrorActionPreference = 'Stop'
Start-Process `
  -FilePath .\.venv\Scripts\python.exe `
  -ArgumentList @('.\.venv\Scripts\bridge.exe','--port','8780') `
  -WorkingDirectory 'C:\Users\poliroika\Documents\albert7' `
  -WindowStyle Hidden

Start-Sleep -Seconds 8
$health = Invoke-RestMethod -Uri http://127.0.0.1:8780/api/health -TimeoutSec 10
$listeners = @(Get-NetTCPConnection -LocalPort 8780 -State Listen -ErrorAction SilentlyContinue |
    Select-Object LocalAddress,LocalPort,OwningProcess,State)
$bridges = @(Get-CimInstance Win32_Process | Where-Object {
    ($_.CommandLine -match 'bridge(\.exe)?') -and ($_.CommandLine -match '--port\s+8780')
} | Select-Object ProcessId,ParentProcessId,CommandLine)
[pscustomobject]@{ health=$health; listeners=$listeners; bridges=$bridges } | ConvertTo-Json -Depth 10
```

## 8. Web UI Run

Использовать Browser / in-app browser tooling. Не заменять финальный запуск на CLI.

Ручной сценарий:

1. Открыть `http://127.0.0.1:8780/chat`.
2. Выбрать workspace `civilization`.
3. Убедиться, что model берется из UI/env, например `GLM-4.7`.
4. Убедиться, что tool count соответствует product config, недавно было около `135 tools`.
5. Если UI показывает max rounds / verify retries, выставить rounds `0` как unlimited.
6. Кликнуть `TASK_MAIN.md`.
7. Убедиться, что появилась active stop button.
8. Дальше только мониторить. Workspace руками не помогать.

Node REPL browser automation pattern. Код намеренно использует `globalThis`, чтобы не ловить redeclare errors между повторами:

```javascript
if (!globalThis.agent) {
  const browserRuntimeModule = await import('file:///C:/Users/poliroika/.codex/plugins/cache/openai-bundled/browser/0.1.0-alpha2/scripts/browser-client.mjs');
  await browserRuntimeModule.setupBrowserRuntime({ globals: globalThis });
}

if (!globalThis.browser) {
  globalThis.browser = await agent.browsers.get('iab');
}

await globalThis.browser.nameSession('civilization calibration');

try {
  globalThis.civilizationCalibrationTab = await globalThis.browser.tabs.selected();
} catch {}

if (!globalThis.civilizationCalibrationTab) {
  globalThis.civilizationCalibrationTab = await globalThis.browser.tabs.new();
}

const civilizationCalibrationUrl = 'http://127.0.0.1:8780/chat';
const currentCivilizationUrl = await globalThis.civilizationCalibrationTab.url().catch(() => '');
if (currentCivilizationUrl !== civilizationCalibrationUrl) {
  await globalThis.civilizationCalibrationTab.goto(civilizationCalibrationUrl);
} else {
  await globalThis.civilizationCalibrationTab.reload();
}

await globalThis.civilizationCalibrationTab.playwright.waitForLoadState({
  state: 'domcontentloaded',
  timeoutMs: 15000
});
await new Promise(resolve => setTimeout(resolve, 2500));

const civilizationDom = String(await globalThis.civilizationCalibrationTab.dom_cua.get_visible_dom());
const taskMatch = civilizationDom.match(/button node_id=(\d+)[^>]*>TASK_MAIN\.md<\/button>/);
if (!taskMatch) {
  throw new Error('TASK_MAIN.md button not found');
}
await globalThis.civilizationCalibrationTab.dom_cua.click({ node_id: taskMatch[1] });
```

После клика снять DOM/status и проверить:

- stop button active,
- выбранный workspace/task правильный,
- model/tool count видны,
- max rounds `0` или явно unlimited,
- run id появился в `.memory/drive/state/phase_plan.json`.

## 9. Monitoring Snapshot

Запускать периодически во время live run. Это read-only.

```powershell
$ErrorActionPreference = 'Continue'
$root='C:\Users\poliroika\Documents\albert7\workspaces\civilization'
$phasePlan=Join-Path $root '.memory\drive\state\phase_plan.json'
$result = [ordered]@{
  now=(Get-Date).ToString('o')
  phase_plan_exists=(Test-Path -LiteralPath $phasePlan)
  entries=@(Get-ChildItem -LiteralPath $root -Force -ErrorAction SilentlyContinue | Select-Object Name,Mode,Length)
}

if (Test-Path -LiteralPath $phasePlan) {
  $plan=Get-Content -LiteralPath $phasePlan -Encoding UTF8 -Raw | ConvertFrom-Json
  $runId=$plan.run_id
  $exec=$plan.nodes | Where-Object id -eq 'execute'
  $subtasks= if($exec.subtasks){ @($exec.subtasks) } else { @() }
  $result.run_id=$runId
  $result.statuses=@($plan.nodes | Select-Object id,status,started_at,ended_at)
  $result.subtask_counts=[ordered]@{
    total=$subtasks.Count
    done=@($subtasks|Where-Object status -eq 'done').Count
    running=@($subtasks|Where-Object status -eq 'running').Count
    failed=@($subtasks|Where-Object status -eq 'failed').Count
    pending=@($subtasks|Where-Object status -eq 'pending').Count
  }

  $proposal=Join-Path $root '.memory\drive\state\phase_plan_proposal_latest.json'
  if(Test-Path -LiteralPath $proposal){
    $text=Get-Content -LiteralPath $proposal -Encoding UTF8 -Raw
    try {
      $json=$text | ConvertFrom-Json
      $subtasks = @()
      if ($json.plan -and $json.plan.subtasks) { $subtasks = @($json.plan.subtasks) }
      elseif ($json.subtasks) { $subtasks = @($json.subtasks) }
      $proofRows = @($subtasks | ForEach-Object {
        $proof = $_.proof
        [pscustomobject]@{
          id=$_.id
          title=$_.title
          files_to_create=$_.files_to_create
          files_to_change=$_.files_to_change
          proof_kind=$proof.execution.kind
          oracle_type=$proof.oracle.oracle_type
          required_properties=$proof.oracle.required_properties
          evidence_ref_count=@($proof.evidence_refs).Count
        }
      })
      $result.proposal_contract=[ordered]@{
        size=$text.Length
        hasOpenAiKey=$text.Contains('OPENAI_API_KEY')
        hasUnsupportedOuroborosModel=$text.Contains('OUROBOROS_LLM_MODEL')
        hasUnsupportedLlBaseUrl=$text.Contains('LL_BASE_URL')
        obsolete_success_test_field_count=@($subtasks | Where-Object { $_.PSObject.Properties.Name -contains 'success_test' }).Count
        missing_proof_count=@($subtasks | Where-Object { -not ($_.PSObject.Properties.Name -contains 'proof') }).Count
        proofs=$proofRows
      }
    } catch {
      $result.proposal_contract_parse_error=$_.Exception.Message
    }
  }

  $submitted=Join-Path $root '.memory\drive\state\phase_plan_submitted_latest.json'
  if(Test-Path -LiteralPath $submitted){
    $result.submitted_plan_size=(Get-Item -LiteralPath $submitted).Length
  }

  $research=Join-Path $root '.memory\drive\state\research_summary_latest.json'
  if(Test-Path -LiteralPath $research){
    try {
      $researchJson=Get-Content -LiteralPath $research -Encoding UTF8 -Raw | ConvertFrom-Json
      $result.research_summary=[ordered]@{
        architecture_id=$researchJson.architecture_id
        findings_ids=$researchJson.findings_ids
      }
    } catch {}
  }

  $signals=Join-Path $root '.memory\drive\state\phase_control_signals.jsonl'
  if(Test-Path -LiteralPath $signals){
    $result.latest_phase_signals=@(Get-Content -LiteralPath $signals -Encoding UTF8 |
      Select-Object -Last 80 |
      ForEach-Object { try { $_ | ConvertFrom-Json } catch {$null} } |
      Where-Object { $_ } |
      Select-Object ts,task_id,phase,action,verdict,artifact,reason)
  }

  $tools=Join-Path $root '.memory\drive\logs\tools.jsonl'
  if(Test-Path -LiteralPath $tools){
    $rows=Get-Content -LiteralPath $tools -Encoding UTF8 |
      ForEach-Object { try { $_|ConvertFrom-Json } catch {$null} } |
      Where-Object { $_ -and $_.task_id -like "$runId*" }
    $result.tool_counts=@($rows | Group-Object tool | Sort-Object Count -Descending | Select-Object Count,Name)
    $result.latest_key_tools=@($rows |
      Where-Object { $_.tool -in @(
        'submit_research_summary','propose_phase_plan','submit_phase_plan',
        'submit_micro_review','loop_back_to','github_project_search','mcp_discover',
        'web_search','deep_search','get_gmas_context','search_gmas_knowledge',
        'palace_add','palace_search','get_umbrella_memory',
        'apply_workspace_patch','run_workspace_command','mark_subtask_complete',
        'request_watcher_review','run_workspace_verify','mutate_phase_plan'
      ) } |
      Select-Object -Last 160 ts,task_id,tool,@{n='preview';e={
        $p=([string]$_.result_preview -replace "`r",' ' -replace "`n",' ')
        if($p.Length -gt 1800){$p.Substring(0,1800)+' ...'}else{$p}
      }})
  }
}
$result | ConvertTo-Json -Depth 14
```

## 10. Source Of Truth Files

Читать в первую очередь:

```text
workspaces/civilization/.memory/drive/state/phase_plan.json
workspaces/civilization/.memory/drive/state/state.json
workspaces/civilization/.memory/drive/state/research_summary_latest.json
workspaces/civilization/.memory/drive/state/phase_plan_proposal_latest.json
workspaces/civilization/.memory/drive/state/phase_plan_submitted_latest.json
workspaces/civilization/.memory/drive/state/phase_control_signals.jsonl
workspaces/civilization/.memory/drive/logs/tools.jsonl
workspaces/civilization/.memory/drive/logs/events.jsonl
workspaces/civilization/.umbrella/supervisor_ledger
```

Memory/provenance directories могут отличаться после рефакторов, но нужно смотреть все внутри:

```text
workspaces/civilization/.memory
workspaces/civilization/.memory/drive
workspaces/civilization/.memory/drive/state
workspaces/civilization/.memory/drive/logs
workspaces/civilization/.memory/drive/memory
workspaces/civilization/.memory/palace
```

Generated source/tests читать можно только для диагностики. Ручные patches туда запрещены.
если есть еще артефакты прогонов которые тебе могут помочь тоже читай их.
## 11. Memory Audit Checklist

Память важна не меньше фаз. Чем длиннее прогон, тем более иерархической, полезной и проверяемой она должна становиться.

Смотреть:

- Какие tools пишут память: `palace_add`, `submit_research_summary`, `submit_phase_plan`, `mutate_phase_plan`, `mark_subtask_complete`, watcher/review signals.
- Какие tools читают память: phase recall, `palace_search`, `get_umbrella_memory`, watcher bundle, plan/review prompts.
- Есть ли canonical id и typed `EvidenceRef`, или одна и та же запись считается несколькими findings.
- Не смешиваются ли `scratchpad`, `progress`, `observation`, `research_finding`, `architecture_decision`, `completion_memory`.
- Не промотируется ли непроверенное observation сразу в durable finding.
- Не попадает ли stale/unsubmitted proposal в execute.
- После cleanup исчезает ли `.memory` полностью.
- Использует ли review свежий submitted artifact, а не старую proposal memory.
- Есть ли source provenance: tool result id, URL/repo/MCP result, command evidence, artifact path, ledger event hash.
- Durable memory имеет verified-or-higher trust level и typed evidence refs.

Красные флаги:

- `submit_research_summary` цитирует invented ids или duplicate aliases.
- `palace_add(kind=scratchpad)` засчитывается как research finding.
- `palace_add(kind=research_finding)` использует broad source вроде bare `github_project_search`, `mcp_discover`, `web_search` или `deep_search` вместо конкретного `github:owner/repo` / `tool:<query-or-intent>`.
- Empty-result discovery (`mcp_discover:<query>` with `results=[]`, empty GitHub/web/deep search) засчитывается как verified finding вместо observation/coverage note.
- Fallback/low-confidence GMAS retrieval, включая `get_gmas_context:<query>`, записывается как `verified=true` research finding.
- Truncated/invalid JSON `result_preview` hides `"metadata": {"fallback": true}` or low `confidence`, but source still counts as verified GMAS finding.
- `submit_research_summary` cites valid finding ids, but freeform notes add explicit `Source:` labels that are not exactly the cited findings' `source_path` values.
- Research пишет fallback/mock/heuristic LLM behavior в durable memory.
- Plan review делает `ok`, не прочитав latest submitted plan artifact.
- Watcher принимает `mark_subtask_complete` без command evidence.
- Completion memory утверждает, что поддерживаемые aliases вроде `LLM_API_KEY` obsolete/unsupported.
- Memory из старого run влияет на clean rerun.

Если это повторяется, чинить MemoryWriteService/ReviewBundleBuilder/phase recall, а не добавлять еще один локальный запрет в одном tool.

## 12. Phase Checklist

### Preflight

Проверить:

- `env_check` ran.
- UI/bridge показывает active model из inherited env.
- Tool list соответствует фазам.
- `workspace.toml` generation нормальный.
- Нет раннего утверждения, что `OPENAI_API_KEY` обязателен для workspace LLM runtime.

Важно:

- `OPENAI_API_KEY` не относится к generic `web_search` или `deep_search`. `web_search` идет через GMAS WebSearchTool: DuckDuckGo default без ключа, optional provider keys только для выбранных search providers вроде Brave/Serper/Tavily/Exa/SearXNG/Bocha/Google. `deep_search` использует pluggable engines: GMAS+Playwright no-key baseline, Firecrawl search+scrape при `FIRECRAWL_API_KEY`, Jina Reader Search при `JINA_API_KEY`/явном выборе.
- Workspace LLM/e2e code должен использовать публичные aliases generated project:
  - `LLM_API_KEY`
  - `LLM_BASE_URL`
  - `LLM_MODEL`
- Umbrella отвечает за bridge: если host запущен с control-plane aliases, перед workspace-командой они нормализуются в `LLM_*`. Generated project не должен документировать или тестировать control-plane aliases.

### Research

Искать:

- `github_project_search` с task-specific queries.
- `mcp_discover` с task-specific queries.
- `web_search` или `deep_search` attempted.
- `palace_add` сохраняет concrete findings, а не progress notes.
- `submit_research_summary` использует accepted finding ids, не invented labels.
- Если summary notes пишут `Source:`/`Sources:`, эти handles должны быть exact backed source paths from cited accepted findings, not shortened/invented labels.
- `architecture_id` стабилен: `arch-...` или `architecture-...`.
- Findings имеют provenance/source.

Допустимо:

- `web_search`/`deep_search` могут вернуть `provider_error` или `no_results`, если сеть/провайдер/браузер реально не сработали. Это не должно превращаться в утверждение, что нужен `OPENAI_API_KEY`. `provider_unavailable` из-за отсутствия OpenAI key для generic internet discovery считается регрессией.

Если research пропускает discovery, выдумывает ids или пишет vague architecture, чинить research handoff gates, MemoryWriteService, research prompt или discovery tool policy.

### Research Review

Должен:

- Прочитать `.memory/drive/state/research_summary_latest.json` перед `verdict=ok`.
- Отклонить fabricated findings, duplicate aliases, scratchpad-as-finding, and summary source labels not backed by cited accepted finding source paths.
- Не loop back для мелких wording деталей, если architecture viable.

Likely locations:

```text
ouroboros/ouroboros/tools/phase_control.py
umbrella/prompts/phases/research_review.system.md
ouroboros/tests/test_phase_control_artifacts.py
umbrella/memory/*
```

### Plan

План должен быть executable, compact, universal.

Required shape:

- Compact top-level `subtasks` array или явно projectable nested phases.
- Обычно 8-16 leaves для large greenfield/full-stack задачи.
- Каждый executable leaf:
  - `id`
  - `title`
  - `goal` или `description`
  - `files_to_create` / `files_to_change` / `files_affected`
  - typed `proof` object:
    - `execution.kind`, `execution.command`, `execution.shell=false`
    - `oracle.oracle_type`, `oracle.required_properties`
    - `scope.files_under_test`, `scope.changed_files_expected`, optional `scope.pytest_targets`
    - `anti_gaming.allows_mock=false` for real-runtime proofs
    - optional human-readable `human_claims`, which never act as the machine gate
- Python production code: `src/<package>/...`, не bare `src/*.py`.
- Tests: `tests/`, не `src/`, не `backend/tests`, не `scripts/test_*.py`.
- Durable docs для complex LLM/frontend/backend projects: `docs/`.
- Proof target должен быть уже существующим или owned by same/earlier leaf.
- Code leaves need behavioral or stronger oracle; import/build-only proof is not enough for behavior changes.

Reject/fix if plan contains:

- `OPENAI_API_KEY` как universal LLM credential.
- `OUROBOROS_LLM_MODEL`; в generated workspace правильно `LLM_MODEL`.
- `LL_BASE_URL`; правильно `LLM_BASE_URL`.
- Missing public `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` env contract for real LLM paths.
- Mock/fake/dry-run LLM как proof path.
- Deterministic/static/heuristic/random/cached fallback decisions для LLM bot behavior.
- Manual e2e checks как success criteria.
- File-existence-only checks.
- Complex `python -c` behavioral/server/LLM checks.
- `pytest --collect-only`.
- Obsolete string proof fields.
- `execution.shell=true`, `bash -lc`, `sh -c`, `cmd /c`, `powershell -Command`, `|| true`, `exit 0`, `check=False`, or other failure masking.
- `exit $?`, `Start-Job`, `ps`, `grep`, `pkill`, background `&`, fragile shell process control.
- Generic tool pseudo-args вроде `run_unit_tests tests/test_x.py`.
- Depth-limit placeholders вроде `{"_depth_limit": true}`.
- Broad leaves, которые меняют слишком много файлов сразу.
- Candidate plan paths outside the active workspace, parent-path escapes like `../umbrella/...`, `.git` paths, or generated-workspace edits to `workspace.toml`, `verification.toml`, or `verify.sh`.
- Final/e2e/verification leaf that lacks a distinct final smoke/e2e/browser/HTTP proof or managed verifier gate.

Если плохой план rejected и модель адаптируется - это healthy. Если зацикливается - чинить contract/prompt/review bundle, а не только добавлять новые regex.

Likely locations:

```text
umbrella/contracts/models.py
umbrella/contracts/plan_ir.py
umbrella/contracts/validators.py
umbrella/contracts/decision.py
umbrella/contracts/compiler.py
umbrella/deep_agent_tools/domain_policy.py
umbrella/deep_agent_tools/phase_contract_handlers.py
umbrella/orchestrator/runner.py
umbrella/prompts/phases/plan.system.md
ouroboros/tests/test_phase_contract_tools.py
umbrella/tests/test_contracts.py
umbrella/tests/test_regex_budget.py
```

### Plan Review

Plan review должен возвращать structured `ReviewContract`: `verdict`, typed `issues`, optional `loop_back_target`, optional notes. Notes могут быть на русском, китайском или любом языке и не должны менять machine decision.

Блокировать только реальные blockers:

- missing executable subtasks,
- missing/invalid typed proof,
- unavailable tools,
- unsafe path/layout,
- missing LLM env contract,
- hardcoded/mock/fallback LLM behavior,
- acceptance criteria not covered,
- submitted artifact not read.
- control/evaluator file mutation in candidate plan paths,
- final verification proof gap,
- fake/stale evidence ref,
- proof scope mismatch,
- human checkpoint required by risk profile.

Не loop back за execution-owned details:

- exact topology internals,
- reconnect/backoff constants,
- class names,
- scenario coverage expansion,
- docs examples,
- protective wording like "clarify no caching/no fallback".

Если review пишет "plan is fundamentally sound", но ставит `verdict=revise` без typed blocker, это system bug. Если notes выглядят тревожно, но `issues=[]`, machine decision не должна меняться.

Важно: в Web UI logs `phase_label` может быть `linear`, а настоящий phase виден в `task_id` suffix вроде `...:plan_review`. Guards должны смотреть task_id.

### Execute

Не помогать workspace руками.

Ожидания:

- Execute создает coherent project structure.
- Использует `src/<package>`, `docs`, `tests`.
- Использует `apply_workspace_patch`, не host-side edits.
- Для LLM/agent/multi-agent/bot tasks до первого write должен получить GMAS context или явно показать, что DomainPolicy не требует GMAS.
- Для не-LLM проектов GMAS не нужен.
- Каждый subtask запускает declared typed proof/probe.
- Failing tests ведут к self-remediation внутри execute.
- Subtask не mark done без `CompletionContract` и fresh typed evidence refs.

Если первый write блокируется до GMAS, это нормально для LLM/agent domain. Если модель каждый раз сначала пытается write, значит prompt/prelude/review bundle плохо подгружает DomainPolicy. Чинить фазовый context injection, tool preconditions или write guard feedback.

Likely locations:

```text
ouroboros/ouroboros/tools/umbrella_tools.py
ouroboros/ouroboros/tools/phase_control.py
ouroboros/ouroboros/loop.py
umbrella/orchestrator/worker.py
umbrella/prompts/phases/execute.system.md
umbrella/prompts/phases/subtask_review.system.md
umbrella/deep_agent_tools/domain_policy.py
```

### Watcher / Subtask Review

Должен:

- Реально запускаться там, где configured.
- Читать generated files, proof output, latest tool errors, active card, ledger-backed evidence refs.
- Отличать implementation bug от bad proof contract.
- Не принимать fake/stale evidence.
- Просить retry/remediation при failed tests.
- Уметь предложить `mutate_phase_plan`, если proof contract сам неверен.
- Возвращать structured review issues, не free-text blockers.

Смотреть tools/signals:

```text
request_watcher_review
submit_micro_review
mark_subtask_complete
run_workspace_command
run_workspace_verify
mutate_phase_plan
```

### Final Review / Verify

Должен:

- Читать final actual state.
- Запускать real commands/browser/HTTP/e2e gates.
- Real LLM/e2e proof использует inherited env aliases.
- Missing LLM env должен дать fail/skip/pause с ясной real-LLM-required причиной, не mock.
- Для code tasks отсутствие verifier/probe/behavioral test coverage не является success. `skipped` допустим только для явно docs-only/non-code задач с DomainPolicy reason.
- Запускать verifier из supervisor-controlled evaluator, а не из изменяемого candidate workspace, когда это доступно.
- Проверять diff против anti-gaming scanner: удаление/ослабление тестов, fixture/verifier tampering, fake/stub paths, env-gated bypasses and shell success masking.
- Связывать final acceptance с `VerificationReportRef`, `EvidenceRef` and `CompletionContract`: behavior claims должны иметь executable evidence, а production diff должен иметь хотя бы один relevant test/probe.
- `submit_verification(status="pass")` без matching passed `VerificationReportRef` должен reject.
- Verification report должен совпадать с current `workspace_hash`/`diff_hash`; old proof после patch = `stale_proof_ref`.
- Если verify fails, должны быть remediation loops и convergence.

## 13. GMAS Policy

GMAS нужен не всегда.

Использовать GMAS только если target project или subtask включает:

- LLM runtime,
- agents,
- multi-agent systems,
- bot decision making,
- autonomous reasoning loops,
- GMAS integrations,
- Umbrella/Ouroboros-like control planes.

Не требовать GMAS для обычных CRUD apps, static sites, non-agent games, CLI tools, data transforms и подобных задач без LLM/agent domain.

Где лучше чинить:

- DomainPolicy определяет `requires_gmas_context`.
- Runner/worker injects phase prelude только для нужного domain.
- Execute prompt говорит: если DomainPolicy требует GMAS, вызови `get_gmas_context` до первого write.
- Write guard блокирует first write только при `requires_gmas_context=true`.
- Watcher проверяет GMAS evidence только для таких domains.

## 14. Parallel Agents

Параллельные агенты полезны, но нельзя запускать несколько writers в один `workspaces/civilization`.

Разрешенная параллельность:

- Один live Web UI run пишет workspace.
- Два агента read-only постоянно смотрят live run: Run Monitor и Evidence/Memory Auditor.
- Research agent ищет концептуальные решения в web/projects/MCP, последних исследованиях и похожих agent-orchestration системах. Он предлагает идеи, но не правит код.
- Plan/Execute аудиторы подключаются точечно, когда run застрял в plan/review/execute.
- Patch Worker получает только bounded product-code slice outside `workspaces/civilization`, с disjoint write set.
- Codebase Refactor Agent смотрит всю кодовую базу Umbrella/Ouroboros, особенно Umbrella, и ищет перегруженность, дубли, hardcode, устаревшие guard piles и места, которые лучше перенести в policies/prompts/services. Он может менять product code только в согласованном bounded scope, с тестами и без ручного вмешательства в generated workspace.

### Agent 1: Run Monitor

Назначение: следить за live run и быстро находить, где он застрял.

Prompt:

```text
Ты Run Monitor для calibration run `workspaces/civilization`.
Работай read-only. Не редактируй workspace и product code.
Каждые несколько минут смотри phase_plan.json, tools.jsonl, events.jsonl, phase_control_signals.jsonl и Web UI DOM/status если доступно.
Сообщай:
- run_id,
- текущую фазу и статусы nodes,
- последние ключевые tool calls/errors,
- first blocker candidate,
- были ли memory writes и какие,
- есть ли нарушение non-negotiable rules.
Не предлагай локальных workspace patches. Если видишь баг системы, укажи слой: prompt, validator, runner, watcher, memory, tool schema, Web UI.
```

### Agent 2: Evidence And Memory Auditor

Назначение: смотреть память, provenance и иерархию.

Prompt:

```text
Ты Evidence/Memory Auditor для Umbrella/Ouroboros calibration.
Работай read-only. Не редактируй `workspaces/civilization`.
Проверь `.memory`, `.umbrella/supervisor_ledger`, tools.jsonl, phase_control_signals.jsonl, research_summary_latest.json, phase_plan_* artifacts.
Найди:
- кто писал память и когда,
- какие ids canonical и какие typed EvidenceRef на них ссылаются,
- есть ли duplicate aliases,
- есть ли scratchpad/progress, засчитанный как finding,
- есть ли stale/unsubmitted artifact, который влияет на текущую фазу,
- есть ли claims без provenance,
- есть ли promotion observation -> durable memory без verified evidence/trust.
Вывод дай как bug candidates с exact artifact/tool payload path и рекомендуемым conceptual layer: MemoryWriteService, Contract/Evidence Gate, ReviewBundleBuilder, phase prompt, validator.
```

### Agent 3: Plan Contract Reviewer

Назначение: отдельно проверять submitted/proposed plan против контрактов.

Prompt:

```text
Ты Plan Contract Reviewer.
Работай read-only. Не редактируй workspace.
Проверь latest phase_plan_proposal/submitted artifact и runner state.
Проверь:
- 8-16 bounded executable leaves,
- Python layout `src/<package>/...`,
- tests only under `tests/`,
- each leaf has declared files and typed `proof`,
- pytest/HTTP/build target exists or is owned by same/earlier leaf,
- proof has execution/oracle/scope/anti-gaming fields,
- code behavior leaves do not rely on import/build-only proof,
- LLM env aliases correct,
- no mock/fake/dry-run/fallback proof path,
- no shell masking / collect-only / bash -lc / stale evidence refs,
- GMAS required only for LLM/agent/multi-agent domains.
Отделяй real blockers от execution-owned details.
Если нашел issue, дай minimal captured object shape for regression test.
```

### Agent 4: Execute And Watcher Auditor

Назначение: смотреть execution loop, test failures, watcher quality.

Prompt:

```text
Ты Execute/Watcher Auditor.
Работай read-only.
Смотри active subtask in phase_plan.json, run_workspace_command outputs, apply_workspace_patch failures, request_watcher_review, submit_micro_review, mark_subtask_complete.
Проверь:
- не пишет ли агент до обязательного GMAS context для LLM/agent tasks,
- не marked done ли subtask без `CompletionContract` and fresh typed evidence,
- watcher читал ли relevant generated files and test evidence,
- failure это implementation bug, bad contract, bad test, env blocker или patch mechanics,
- не повторяется ли старый bug из docs/civilization-calibration-buglog.md.
Вывод: current blocker, evidence lines, recommended conceptual fix, regression test shape.
```

### Agent 5: Conceptual Research Agent

Назначение: когда локальные fix loops повторяются, найти идеи в статьях, проектах, agent orchestration literature, MCP/tool ecosystems.

Prompt:

```text
Ты Conceptual Research Agent.
Ищи не локальную строковую правку, а архитектурное решение для повторяющегося класса багов Umbrella/Ouroboros.
Темы: typed contracts, evidence refs, agent memory provenance, tool-use validation, multi-agent orchestration, reviewer bundles, long-running coding agents, immutable evaluators, capability kernels, supervisor ledgers, proof-carrying completions, anti-gaming scanners, mutation/metamorphic tests, adversarial reviewers.
Используй web/project/MCP search where available. Сравнивай не названия библиотек, а принципы: как системы передают контекст агентам, как связывают память с доказательствами, как review получает bundle, как отделяют candidate workspace от evaluator, как технически ограничивают capabilities, как не допускают fake success и test/verifier gaming.
Ориентируйся на Umbrella-internal внедрение: `umbrella/contracts`, `umbrella/enforcement/kernel.py`, immutable evaluator/fresh verification env, hash-chained supervisor ledger, `umbrella/analysis`, `umbrella/verification/test_tamper.py`, `umbrella/verification/diff_policy.py`, mutation smoke, post-patch metamorphic probes, evidence-bound memory, sandbox self-edit rollback, and PhaseRunner as orchestration over contract decision components.
Верни:
- 3-5 concrete patterns,
- как они применимы к DomainPolicy/Contract-Evidence Gate/MemoryWriteService/ReviewBundleBuilder/CapabilityKernel/SupervisorLedger/VerifierGate/AntiGamingScanner,
- какой минимальный продуктовый slice стоит внедрить первым,
- риски и тестовую стратегию.
- какие prompts/tools/orchestration слои затронуть и чего не трогать.
Не предлагай hardcoding под civilization.
```

### Agent 6: Bounded Patch Worker

Назначение: реализовать один disjoint product-code slice, пока основной агент занимается мониторингом.

Prompt:

```text
Ты Patch Worker для Umbrella/Ouroboros.
Не трогай `workspaces/civilization`.
Твоя write scope строго: <files/modules>.
Ты не один в codebase, не откатывай чужие изменения.
Сначала прочитай buglog и relevant tests.
Используй captured payload shape from real logs.
Добавь regression tests, затем product fix.
В финале перечисли changed files, tests run, и какой bug class закрыт.
```

### Agent 7: Codebase Refactor Agent

Назначение: искать и исправлять перегруженность codebase, не добавляя новый слой костылей.

Prompt:

```text
Ты Codebase Refactor Agent для Umbrella/Ouroboros calibration.
Твоя цель - улучшить читаемость и архитектурную устойчивость product code, а не чинить один `civilization` run локальным guard.
Не трогай `workspaces/civilization`.
Сначала прочитай:
- `docs/civilization-calibration-buglog.md`,
- этот runbook,
- релевантные docs (`docs/umbrella-layer.md`, `docs/ouroboros.md`, `docs/gmas.md`, `docs/workspaces.md`),
- тесты вокруг выбранного слоя.

Ищи:
- дублирующиеся validators/guards,
- правила, которые должны быть DomainPolicy, Contract/Evidence Gate, MemoryWriteService, ReviewBundleBuilder, CapabilityKernel, SupervisorLedger, VerifierGate или AntiGamingScanner,
- устаревший код после предыдущих fixes,
- hardcoded civilization-specific assumptions,
- места, где prompt/tool policy лучше, чем новый ad hoc parser,
- места, где prompt-only запрет должен стать техническим sandbox/capability boundary,
- места, где shell/drive_write/Claude-code path может обходить write guards, verifier или supervisor logs/control,
- места, где skipped verifier, missing tests, changed fixtures or weakened assertions могут стать ложным success,
- как аккуратно укрепить `umbrella/contracts`, `umbrella/enforcement/kernel.py`, `VerificationGate`, `AntiGamingGate`, `GmasPreludeProvider` and `LoopbackController` без большого rewrite,
- code paths, где Umbrella перестает быть универсальным control plane для разных deep agents.

Работай только в bounded scope. Если scope не задан, сначала верни refactor map и предложи 1-2 маленьких безопасных slices.
Если правишь код:
- добавь или обнови regression tests,
- не меняй generated workspace files,
- не откатывай чужие изменения,
- не возвращай старые tool payload formats; новая версия контрактов одна,
- в финале перечисли changed files, tests run, removed duplication и какие будущие fixes стали проще.
```

## 15. Где Чинить Что

Чинить validators/schema, если:

- bad plan/output accepted,
- good protective plan falsely rejected,
- same invalid tool call shape recurs,
- phase can mark success without evidence,
- typed contract shape is valid but evidence is fake/stale/wrong scope.

Чинить prompts, если:

- model repeatedly ignores clear validator feedback,
- desired shape needs examples,
- review agents over-escalate notes into blockers,
- phase misunderstands product role.

Чинить orchestration, если:

- phase status/cursor/retry wrong,
- review loop target wrong,
- `phase_label`/`task_id` mismatch bypasses guards,
- execute starts before plan review passes,
- Web UI controls do not affect worker env.

Чинить tool policy/schema, если:

- tools missing from phase,
- obsolete aliases accepted,
- pseudo-command formats accepted,
- fragile shell patterns allowed,
- first-write preconditions are not domain-aware.
- shell/CLI tool can write outside phase capability profile,
- drive_write or workspace write tools can touch logs/control/verification/evaluator paths.

Чинить memory, если:

- accepted findings not traceable,
- stale memory overrides current artifacts,
- duplicate aliases counted,
- durable memory lacks verified-or-higher trust or typed EvidenceRef,
- summaries cannot be audited from `.memory`.

Чинить watcher/review bundle, если:

- watcher misses obvious test evidence,
- accepts fake evidence,
- loops on nonblockers,
- cannot distinguish bad contract from bad implementation,
- does not receive relevant memory/tool excerpts.

Чинить Web UI, если:

- buttons launch wrong workspace/task,
- active run state unclear,
- stop does not stop,
- round/verify settings not propagated,
- logs/status invisible for operator control.

Чинить evaluator/verifier/capability layer, если:

- no verifier/probe is treated as success for a code task,
- production code changes without behavioral test/probe evidence,
- tests/verifier/fixtures/policies are modified to pass instead of behavior being fixed,
- required tool evidence can be forged from the workspace,
- supervisor-injected context is confused with agent-executed proof,
- regex anti-cheat catches a phrase but leaves equivalent stub/mock/env/snapshot bypass.

## 16. Regression Test Discipline

Для каждого fix:

1. Найти real failing payload в:
   - `tools.jsonl`,
   - `phase_control_signals.jsonl`,
   - `phase_plan_proposal_latest.json`,
   - `phase_plan_submitted_latest.json`,
   - memory artifacts.
2. Сжать payload до минимального object shape, который воспроизводит баг.
3. Добавить тест на том слое, где баг должен был быть пойман.
4. Добавить positive и negative tests, если меняется граница.
5. Запустить focused tests.
6. Запустить affected suite.
7. Записать bug и fix в `docs/civilization-calibration-buglog.md`.
8. Только потом stop/clean/restart/Web UI click.

Common test locations:

```text
umbrella/tests/test_contracts.py
umbrella/tests/test_regex_budget.py
umbrella/tests/test_diff_policy.py
umbrella/tests/test_memory_evidence_policy.py
umbrella/tests/test_test_quality.py
umbrella/tests/test_phase_manifests_valid.py
umbrella/tests/fixtures/adversarial_contracts/
ouroboros/tests/test_run_workspace_command_terminal.py
umbrella/tests/test_web_bridge.py
```

Useful commands:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  umbrella\tests\test_contracts.py `
  umbrella\tests\test_regex_budget.py `
  umbrella\tests\test_diff_policy.py `
  umbrella\tests\test_memory_evidence_policy.py `
  umbrella\tests\test_test_quality.py `
  umbrella\tests\test_phase_manifests_valid.py -q

.\.venv\Scripts\python.exe -m pytest `
  ouroboros\tests\test_phase_control_artifacts.py `
  ouroboros\tests\test_run_workspace_command_terminal.py -q

.\.venv\Scripts\python.exe -m compileall umbrella
```

## 17. Known Bug Classes To Keep In Mind

Перед новым fix сравнить с buglog. Уже встречались классы:

- Anti-pattern text like "Mock LLM responses" falsely treated as proposing mock behavior.
- `task_id=...:plan_review` with `phase_label=linear` bypassed review-specific guards.
- Plan review looped back a fundamentally sound plan for nonblocking details.
- LLM/agent plans missing explicit Umbrella runtime aliases.
- Numeric revision checks confused phase numbers with semantic requirements.
- Research summary accepted interrupted/incomplete coverage.
- Research summary cited valid finding ids but added invented/shortened `Source:` labels not bound to those findings' `source_path`.
- Scratchpad/progress memory counted as accepted research finding.
- Duplicate palace ids/aliases double-counted findings.
- Research findings saved forbidden fallback/caching/heuristic LLM behavior.
- Plan accepted fallback conservative strategy.
- Coarse execute leaves exhausted Web UI round budget.
- Tool and runner contracts diverged on leaf count and broad-leaf rules.
- Bare `src/*.py` Python layout accepted.
- Command-prefixed or shell-masked proofs accepted.
- Supported `LLM_*` aliases falsely deprecated in phase memory.
- Plan accepted proof target for a file not created by any current/earlier leaf.
- Plan accepted final verification leaf without distinct final proof or managed verifier report.
- Plan accepted generated-workspace mutation of supervisor/evaluator control files such as `workspace.toml`, `verification.toml`, or `verify.sh`, or parent-path/`.git` boundary escapes.
- Plan accepted typo `LL_BASE_URL`.
- Watcher accepted fake/stale or under-specified completion evidence.
- Patch mismatch feedback hid JSON escaped line endings.
- Old text-only contract payload accepted by mistake instead of typed contract v1.
- Typed payload shape looked valid but used fake/stale/wrong-scope `EvidenceRef`.
- Verification report id existed but hash/workspace/diff did not match current state.
- Missing verifier or skipped verify treated as success for code task.
- Production diff accepted without behavioral tests/probes.
- Shell or Claude-code path bypassed write guards, verifier policy, or logs/control boundaries.
- Agent-writeable drive/log/control paths made required tool evidence forgeable.
- Regex anti-cheat caught one wording but missed equivalent stub/mock/env/test-weakening bypass.

Если новый баг похож, не добавлять второй костыль. Найти общий слой.

## 18. Conceptual Change Preference

Когда видишь проблему, сначала спроси:

- Это отсутствие domain knowledge? Тогда DomainPolicy.
- Это claim без proof или с fake/stale ref? Тогда Contract/Evidence Gate.
- Это память записалась не туда или поднялась слишком высоко? Тогда MemoryWriteService.
- Это reviewer/watcher не видел нужный контекст? Тогда ReviewBundleBuilder.
- Это модель не понимает shape? Тогда prompt examples plus schema/tool feedback.
- Это tool принимает плохой payload? Тогда schema/validator.
- Это runner запускает фазу не вовремя? Тогда orchestration.
- Это успех можно получить через test/verifier/log bypass? Тогда CapabilityKernel, ImmutableEvaluator, SupervisorLedger, VerifierGate или AntiGamingScanner.
- Это tests green, но behavior не доказан? Тогда ProofSpec oracle/properties, CompletionContract, EvidenceRef freshness, mutation/metamorphic probes или adversarial reviewer.
- Это shell/CLI может обойти политику? Тогда технический sandbox/capability wrapper, а не prompt-only запрет.

Regex допустим только как boundary parser. Нельзя строить hard decisions на поиске английских слов, потому что модель может писать по-русски, китайски или менять формулировку. Предпочитать typed fields, issue codes, tool schemas, structured artifacts, provenance links и explicit policy flags.

## 19. Clean Rerun Gate

Перед каждым новым Web UI run:

```powershell
Get-NetTCPConnection -LocalPort 8780 -State Listen -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process | Where-Object {
    ($_.CommandLine -match 'bridge(\.exe)?') -and ($_.CommandLine -match '--port\s+8780')
} | Select-Object ProcessId,ParentProcessId,CommandLine
Get-ChildItem -LiteralPath C:\Users\poliroika\Documents\albert7\workspaces\civilization -Force
```

Если есть listener/bridge - остановить. Если в workspace есть что-то кроме `TASK_MAIN.md` - очистить по safe cleanup command.

## 20. Done Definition

Calibration run не считается завершенным, пока clean Web UI button run не пройдет:

- preflight,
- research,
- research review,
- plan,
- plan review,
- execute,
- watcher/subtask review,
- final review,
- verify,

и не создаст полноценный проект из `TASK_MAIN.md` без ручных правок workspace.

Минимально приемлемый финальный отчет пользователю:

- какой run id прошел,
- какие фазы прошли,
- сколько subtasks done/failed,
- какие final verify commands/browser checks прошли,
- какие memory/research/plan artifacts были использованы,
- были ли skipped checks и почему,
- какие residual risks остались.

## 21. Prompt To Paste Into A Fresh Chat

```text
Ты продолжаешь calibration loop для Ouroboros/Umbrella в `C:\Users\poliroika\Documents\albert7`.

Цель: добиться, чтобы real Web UI button run для `workspaces/civilization/TASK_MAIN.md` с inherited LLM env и rounds=0/unlimited смог построить большой проект до конца без ручных workspace edits.

Модель системы:
- Ouroboros = deep agent.
- Umbrella = control plane/dashboard/orchestrator/tools/memory/watcher/review layer, который запускает и контролирует Ouroboros.
- Исправления должны быть Umbrella-level и универсальными.

Жесткие правила:
- Не редактируй generated files в `workspaces/civilization`; только читай.
- Все product fixes делай в main code/prompts/tools/schemas/tests/orchestration/memory.
- После каждого product/prompt change: stop bridge, clean workspace до одного `TASK_MAIN.md`, restart bridge, click task through Web UI.
- Каждый real bug записывай в `docs/civilization-calibration-buglog.md` и добавляй regression test по captured payload/log.
- Пиши пользователю по-русски.
- Не плодить regex-костыли; если проблема повторяется, думай через DomainPolicy, Contract/Evidence Gate, MemoryWriteService, ReviewBundleBuilder.
- Не принимать green tests как единственный proof. Для code tasks Umbrella должна разделять candidate workspace, immutable/supervisor evaluator, supervisor-only ledger, phase capability profiles, AST/static anti-gaming analyzers, typed `ProofSpec`, `CompletionContract` и watcher/adversarial review.
- Долгосрочная Umbrella architecture target: `umbrella/contracts`, `umbrella/enforcement/kernel.py`, immutable evaluator, diff tamper policy, mutation/metamorphic gates, hash-chained SupervisorLedger, evidence-bound memory, sandbox self-edit rollback, PhaseRunner как orchestration layer над contract decision components.
- Уже есть product slices: enforcement kernel/ledger, contracts/evidence refs, AST/static analyzers, diff/test-tamper/mutation verification, typed proof validation, plan path capability boundary, evidence-bound memory, rollback self-edit, no dangerous Claude permission fallback, and shared `research_provenance.py` for source evidence plus research-summary `Source:` label binding. Расширяй эти слои, не создавай параллельные guard piles.
- `plan_review` не должен превращать structural proof/capability blockers в nonblocking notes. Submitted plan с unsafe paths, missing/weak proof, fake/stale evidence ref, shell masking, verifier/control file mutation or final proof gap должен возвращать typed blocking issue; `verdict=ok` должен блокироваться policy.
- `palace_add(kind="research_finding")` не должен принимать broad source вроде bare `github_project_search`/`mcp_discover`/`web_search`/`deep_search`, empty-result discovery, или weak GMAS tool-qualified source. Используй concrete `github:owner/repo` или `tool:<exact query-or-intent>` только когда текущий tool result действительно содержит usable results; fallback/low-confidence GMAS retrieval не является verified finding. Проверяй и parsed payload, и raw/truncated `result_preview`, потому что fallback metadata может быть видна только в тексте лога.
- `submit_research_summary` и `research_review` должны связывать freeform notes with evidence: explicit `Source:` labels must exactly match source paths of cited accepted findings. Valid ids alone are not enough if notes add unbacked narrative sources.
- Если discovery реально scarce, не ослабляй `palace_add` provenance и не принимай fake findings. `submit_research_summary` может использовать `coverage_status="source_scarce"` только когда supervisor-computed source attempts показывают: GitHub, internet и MCP channels были попытаны; есть хотя бы один accepted `research_finding`; usable source rows меньше finding floor; и все usable rows уже harvested into accepted findings. Artifact должен хранить `coverage_report` and `source_scarcity_reason`, а downstream plan должен видеть это как constrained low-evidence handoff, not positive prior-art proof.
- `web_search` не должен зависеть от `OPENAI_API_KEY` как от universal internet access. Он должен быть thin adapter over GMAS WebSearchTool: default DuckDuckGo без ключа, optional configured providers, structured `status/answer/sources/attempts` payload, and provider errors with query/intent/retryable. `deep_search` должен использовать engine boundary: GMAS+Playwright as default, optional Firecrawl/Jina adapters for stronger hosted deep search, not a parallel OpenAI/DuckDuckGo fallback pile. Не добавляй отдельный OpenAI-vs-DuckDuckGo branch в Ouroboros.
- Перед фиксом проверь, какой context Umbrella подает deep agent на фазе: prompts, memory recall/search, tool schemas, tool policy, watcher bundle, DomainPolicy, artifacts.
- Проверяй, что phase-specific context/capabilities реально применены: research read-only, plan writes plan artifacts, execute пишет только active declared scope, verify не меняет product code, logs/control/verification не forgeable для агента.
- GMAS нужен только для LLM/agent/multi-agent/bot tasks, не для всех проектов.
- Не запускай несколько writer runs в один workspace одновременно. Параллельные агенты могут быть read-only auditors или работать по disjoint product-code scopes.
- Используй `docs/` как архитектурный контекст, чтобы не чинить систему против ее идеи.

Сначала:
1. Прочитай `docs/civilization-calibration-buglog.md`.
2. Перечитай этот runbook и релевантные docs (`umbrella-layer`, `ouroboros`, `gmas`, `workspaces`).
3. Проверь port 8780 и содержимое `workspaces/civilization`.
4. Подними/проверь постоянных sidecar agents: Run Monitor, Evidence/Memory Auditor, Plan Contract Reviewer, Execute/Watcher Auditor, Conceptual Research Agent, Codebase Refactor Agent.
5. Если нужно, stop/clean/start bridge.
6. Открой `http://127.0.0.1:8780/chat` через in-app Browser, выставь rounds=0/unlimited, кликни `TASK_MAIN.md`.
7. Мониторь `.memory/drive/state/*`, `.memory/drive/logs/tools.jsonl`, `.memory/drive/logs/events.jsonl`, `.umbrella/supervisor_ledger/*`, Web UI DOM.

Параллельные агенты:
- Держи Run Monitor, Evidence/Memory Auditor, Plan Contract Reviewer и Execute/Watcher Auditor как read-only наблюдателей live run на каждом прогоне.
- Conceptual Research Agent живет как project-wide sidecar: ищет идеи для больших повторяющихся классов багов, не пишет локальные fixes.
- Codebase Refactor Agent живет как project-wide sidecar: ищет дубли, hardcode, устаревшие guard piles и предлагает маленькие bounded slices без раздувания кода.
- Patch Worker может писать только product code outside `workspaces/civilization`, в disjoint scope, с тестами.

Смотри особенно:
- research findings ids/provenance,
- memory writes/reads/promotions,
- concrete source provenance for research findings; no broad tool ids as proof,
- research source scarcity: source attempts, coverage_status, coverage_report, and no alias inflation,
- plan typed proof ownership,
- LLM env aliases,
- GMAS gating,
- watcher evidence,
- final verify realness.
- evaluator/verifier isolation,
- shell/drive write bypasses,
- suspicious test/verifier/fixture weakening,
- behavior claims linked to tests/probes/evidence.

Если run ломается:
- Не помогай workspace руками.
- Найди failing payload.
- Определи правильный слой fix.
- Добавь regression.
- Исправь концептуально: prompt/context injection, tool policy/schema, DomainPolicy, Contract/Evidence Gate, MemoryWriteService, ReviewBundleBuilder, CapabilityKernel, SupervisorLedger, VerifierGate, AntiGamingScanner, watcher или orchestration.
- Если фикс про bypass/tool writes/verify, предпочитай bounded slice вокруг enforcement kernel, evaluator/ledger или diff policy вместо нового локального regex.
- Прогони tests.
- Запиши buglog.
- Сделай clean Web UI rerun.
```
