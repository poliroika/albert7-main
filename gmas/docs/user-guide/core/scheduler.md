# Scheduler

Determines execution order for agents in the graph.

## Execution Order

```python
from gmas.execution import build_execution_order

order = build_execution_order(graph.A_com, graph.agent_ids)
print(order)  # ['agent1', 'agent2', 'agent3']
```

## Parallel Groups

Find agents that can run in parallel:

```python
from gmas.execution import get_parallel_groups

groups = get_parallel_groups(graph)
# [[agent1, agent2], [agent3]]  # agent1,agent2 run in parallel
```

## Adaptive Scheduling

For complex graphs with cycles:

```python
from gmas.execution import AdaptiveScheduler

scheduler = AdaptiveScheduler(pruning_config=pruning_config)
plan = scheduler.create_plan(graph)

for step in plan.steps:
    # Execute step
    pass
```
