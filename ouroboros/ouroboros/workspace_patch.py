"""Workspace patch parsing and in-memory application helpers."""

from dataclasses import dataclass, field


@dataclass
class PatchOperation:
    action: str
    path: str
    hunks: list[list[str]] = field(default_factory=list)
    content_lines: list[str] = field(default_factory=list)


def _norm_line(line: str) -> str:
    if line.startswith(" "):
        return line[1:]
    return line


def _find_subseq(haystack: list[str], needle: list[str]) -> int:
    if not needle:
        return 0
    size = len(needle)
    for idx in range(0, len(haystack) - size + 1):
        if all(haystack[idx + offset] == needle[offset] for offset in range(size)):
            return idx
    return -1


def _find_subseq_rstrip(haystack: list[str], needle: list[str]) -> int:
    if not needle:
        return 0
    return _find_subseq(
        [line.rstrip() for line in haystack], [line.rstrip() for line in needle]
    )


def _is_action_boundary(line: str) -> bool:
    return line.startswith("*** ") and any(
        line.startswith(prefix)
        for prefix in (
            "*** Update File:",
            "*** Add File:",
            "*** Delete File:",
            "*** End Patch",
            "*** End of File",
        )
    )


def parse_workspace_patch(patch: str) -> list[PatchOperation]:
    lines = str(patch or "").splitlines()
    operations: list[PatchOperation] = []
    index = 0
    saw_begin = False
    saw_end = False
    while index < len(lines):
        line = lines[index]
        if line.startswith("*** Begin Patch"):
            saw_begin = True
            index += 1
            continue
        if line.startswith("*** End Patch"):
            saw_end = True
            index += 1
            continue
        if line.startswith("*** Update File:"):
            path = line.split(":", 1)[1].strip()
            if not path:
                raise ValueError("update operation requires a file path")
            index += 1
            hunks: list[list[str]] = []
            current: list[str] = []
            while index < len(lines) and not _is_action_boundary(lines[index]):
                if lines[index].startswith("@@"):
                    if current:
                        hunks.append(current)
                        current = []
                    index += 1
                    continue
                current.append(lines[index])
                index += 1
            if current:
                hunks.append(current)
            if not hunks:
                raise ValueError(f"update operation for {path} has no hunks")
            if index < len(lines) and lines[index].startswith("*** End of File"):
                index += 1
            operations.append(PatchOperation(action="update", path=path, hunks=hunks))
            continue
        if line.startswith("*** Add File:"):
            path = line.split(":", 1)[1].strip()
            if not path:
                raise ValueError("add operation requires a file path")
            index += 1
            content_lines: list[str] = []
            while index < len(lines) and not _is_action_boundary(lines[index]):
                item = lines[index]
                if item.startswith("+"):
                    content_lines.append(item[1:])
                elif item.strip():
                    content_lines.append(item)
                index += 1
            if index < len(lines) and lines[index].startswith("*** End of File"):
                index += 1
            operations.append(
                PatchOperation(action="add", path=path, content_lines=content_lines)
            )
            continue
        if line.startswith("*** Delete File:"):
            path = line.split(":", 1)[1].strip()
            if not path:
                raise ValueError("delete operation requires a file path")
            operations.append(PatchOperation(action="delete", path=path))
            index += 1
            continue
        if line.startswith("*** End of File"):
            index += 1
            continue
        if line.startswith("***"):
            raise ValueError(f"unknown patch directive: {line}")
        if line.strip():
            raise ValueError(
                f"unexpected patch content outside an operation: {line[:80]}"
            )
        index += 1
    if not saw_begin:
        raise ValueError("patch must start with *** Begin Patch")
    if not saw_end:
        raise ValueError("patch must end with *** End Patch")
    if not operations:
        raise ValueError("patch contains no operations")
    return operations


def apply_update_to_text(old_text: str, hunks: list[list[str]], path: str = "") -> str:
    source = old_text.splitlines()
    had_trailing_newline = old_text.endswith("\n")
    for hunk in hunks:
        old_seq: list[str] = []
        new_seq: list[str] = []
        for line in hunk:
            if line.startswith("+"):
                new_seq.append(line[1:])
            elif line.startswith("-"):
                old_seq.append(line[1:])
            else:
                context = _norm_line(line)
                old_seq.append(context)
                new_seq.append(context)
        idx = _find_subseq(source, old_seq)
        if idx < 0:
            idx = _find_subseq_rstrip(source, old_seq)
        if idx < 0:
            label = f" in {path}" if path else ""
            raise ValueError(f"failed to match patch hunk{label}")
        source = source[:idx] + new_seq + source[idx + len(old_seq) :]
    new_text = "\n".join(source)
    if had_trailing_newline or source:
        new_text += "\n"
    return new_text


def text_from_add_lines(lines: list[str]) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + "\n"
