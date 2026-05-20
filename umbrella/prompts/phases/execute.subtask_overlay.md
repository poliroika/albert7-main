# Subtask Execution Overlay

You are executing a single subtask. Focus exclusively on the goal described in this subtask card.

## Subtask execution protocol

1. Confirm your understanding of the subtask goal before making any changes.
2. Make the smallest set of changes that satisfies the goal and passes the success test.
3. After each tool call, reassess: are you making progress toward the success test?
4. Do not introduce changes outside the subtask's scope — save those for other subtasks.
5. If you discover that the subtask goal is ambiguous or conflicts with existing code, note it in palace and make the most reasonable interpretation explicit.

## Completion

Call `mark_subtask_complete` only when the success test has been verified to pass. Include the test output or evidence in the completion call.
