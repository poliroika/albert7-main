"""Revision-instruction contract checks for phase plans."""

from umbrella.deep_agent_tools.phase_contract_common import *
from umbrella.deep_agent_tools.phase_contract_base import *
from umbrella.deep_agent_tools.phase_contract_success import *

def _phase_plan_revision_items(ctx: ToolContext | None) -> list[str]:
    if ctx is None:
        return []
    overlays = getattr(ctx, "context_overlays", {}) or {}
    phase_node = overlays.get("phase_node") if isinstance(overlays, dict) else None
    overlay = phase_node.get("overlay") if isinstance(phase_node, dict) else None
    if not isinstance(overlay, dict):
        return []
    contract = overlay.get("revision_contract")
    if isinstance(contract, dict):
        revisions = contract.get("revisions")
        if isinstance(revisions, list):
            return list(
                dict.fromkeys(str(item).strip() for item in revisions if str(item).strip())
            )
        return []
    reason = str(overlay.get("retry_reason") or "").strip()
    if not reason.lower().startswith("micro review requested revisions"):
        return []
    _, _, details = reason.partition(":")
    return list(
        dict.fromkeys(
            part.strip(" .")
            for part in re.split(r";|\n", details)
            if part.strip(" .")
        )
    )


def _revision_positive_clause(revision: str) -> str:
    text = str(revision or "")
    lower = text.lower()
    if "->" in text:
        text = text.rsplit("->", 1)[1]
        lower = text.lower()
    elif re.search(r"\breplace\b", lower) and " with " in lower:
        start = lower.rfind(" with ") + len(" with ")
        text = text[start:]
        lower = text.lower()
    elif re.search(r"\bremove\b", lower) and re.search(r"\buse\b", lower):
        use_match = list(re.finditer(r"\buse\b", lower))[-1]
        text = text[use_match.end() :]
        lower = text.lower()
    for marker in (" instead of ", " rather than "):
        pos = lower.find(marker)
        if pos >= 0:
            text = text[:pos]
            lower = text.lower()
    optional_parenthetical = re.search(
        r"(?i)\(\s*or\s+(?:add|create|move|note|provide|use)\b", text
    )
    if optional_parenthetical:
        optional_end = text.find(")", optional_parenthetical.start())
        if optional_end >= 0:
            text = (
                text[: optional_parenthetical.start()]
                + " "
                + text[optional_end + 1 :]
            )
        else:
            text = text[: optional_parenthetical.start()]
        lower = text.lower()
    for marker in (
        " or equivalent",
        " or provide ",
        " or use ",
        " or note ",
        " or create ",
    ):
        pos = lower.find(marker)
        if pos >= 0:
            text = text[:pos]
            lower = text.lower()
    return text.replace("`", " ").replace('"', " ").replace("'", " ")


def _revision_keywords(text: str) -> list[str]:
    tokens = [
        match.group(0).lower()
        for match in re.finditer(r"[a-z0-9_]+|[а-яё0-9_]+", str(text or "").lower())
    ]
    keywords: list[str] = []
    for token in tokens:
        if len(token) < 3:
            continue
        if token in _REVISION_STOP_WORDS:
            continue
        if token.startswith("subtask_") or token.startswith("subtask-"):
            continue
        if token.isdigit():
            continue
        if token not in keywords:
            keywords.append(token)
    return keywords


def _normalise_revision_subtask_ref(value: str) -> str:
    text = re.sub(r"\s+", "_", str(value or "").strip().lower()).replace("-", "_")
    text = re.sub(r"^subtasks?_", "subtask_", text)
    text = re.sub(r"^(?:st|task)_", "subtask_", text)
    if re.fullmatch(r"0*\d+", text):
        return f"subtask_{int(text)}"
    if re.fullmatch(r"0*\d+(?:\.\d+)+", text):
        return ".".join(str(int(part)) for part in text.split("."))
    bare_match = re.match(r"0*(\d+(?:\.\d+)?)([a-z][a-z0-9_]*)$", text)
    if bare_match:
        number = ".".join(str(int(part)) for part in bare_match.group(1).split("."))
        suffix = str(bare_match.group(2) or "").strip("_")
        return f"subtask_{number}" + (f"_{suffix}" if suffix else "")
    match = re.match(r"subtask_?0*(\d+(?:\.\d+)?)([a-z0-9_]*)$", text)
    if not match:
        return text
    number = ".".join(str(int(part)) for part in match.group(1).split("."))
    suffix = str(match.group(2) or "").strip("_")
    if "." in number and not suffix:
        return number
    return f"subtask_{number}" + (f"_{suffix}" if suffix else "")


def _normalise_decimal_revision_number(raw: str) -> str:
    text = str(raw or "").strip().lower().replace("_", ".").replace("-", ".")
    match = re.match(r"0*(\d+(?:\.\d+)*)", text)
    if not match:
        return ""
    return ".".join(str(int(part)) for part in match.group(1).split(".") if part)


def _revision_decimal_aliases(value: str) -> set[str]:
    aliases: set[str] = set()
    text = str(value or "").strip().lower()
    for candidate in {text, text.replace("_", "."), text.replace("-", ".")}:
        for match in re.finditer(r"(?:subtask[_.-]?)?0*(\d+(?:[._-]\d+)+)", candidate):
            number = _normalise_decimal_revision_number(match.group(1))
            if not number:
                continue
            aliases.add(number)
            aliases.add(f"subtask_{number}")
            underscored = number.replace(".", "_")
            dashed = number.replace(".", "-")
            aliases.add(underscored)
            aliases.add(f"subtask_{underscored}")
            aliases.add(dashed)
            aliases.add(f"subtask_{dashed}")
            root = number.split(".", 1)[0]
            aliases.add(root)
            aliases.add(f"subtask_{root}")
    return aliases


def _revision_subtask_aliases(value: str) -> set[str]:
    normalised = _normalise_revision_subtask_ref(value)
    raw = str(value or "").strip().lower().replace(" ", "_")
    aliases = {normalised}
    if raw:
        aliases.add(raw)
        aliases.add(raw.replace("-", "_"))
        aliases.add(raw.replace("_", "-"))
    aliases.update(_revision_decimal_aliases(value))
    aliases.update(_revision_decimal_aliases(normalised))
    if normalised.startswith("subtask_"):
        bare = normalised.removeprefix("subtask_")
        aliases.add(bare)
        compact = bare.replace("_", "")
        dashed = bare.replace("_", "-")
        aliases.update({compact, dashed, f"st_{compact}", f"st-{compact}"})
        aliases.update({f"subtask_{compact}", f"subtask-{compact}"})
        aliases.update(
            {
                f"st_{bare}",
                f"st-{dashed}",
                f"task_{bare}",
                f"task-{dashed}",
            }
        )
        bare_lead = re.match(r"0*(\d+)(?:[_.-].*)?$", bare)
        if bare_lead:
            number = str(int(bare_lead.group(1)))
            aliases.add(number)
            aliases.add(f"subtask_{number}")
    else:
        aliases.add(f"subtask_{normalised}")
    lead = re.match(r"0*(\d+)(?:[_.-].*)?$", normalised)
    if lead:
        number = str(int(lead.group(1)))
        aliases.add(number)
        aliases.add(f"subtask_{number}")
    return aliases


def _revision_subtask_ref_numbers(value: str) -> list[str]:
    text = str(value or "")
    text = re.sub(r"(?i)\bsubtasks?(?:[_-]|\s+)?", "", text, count=1)
    numbers: list[str] = []
    for match in re.finditer(r"\b0*\d+(?:\.\d+)?[a-z0-9_]*\b", text):
        number = _normalise_decimal_revision_number(match.group(0))
        if number not in numbers:
            numbers.append(number)
    return numbers


def _revision_number_tokens(text: str) -> list[str]:
    values: list[str] = []
    for match in _REVISION_SUBTASK_RE.finditer(str(text or "")):
        for token in _revision_subtask_ref_numbers(match.group(0)):
            if token not in values:
                values.append(token)
    for match in re.finditer(r"\b0*\d+(?:\.\d+)+[a-z_][a-z0-9_]*\b", str(text or "")):
        token = _normalise_decimal_revision_number(match.group(0))
        if token and token not in values:
            values.append(token)
    for match in re.finditer(r"\b\d+(?:\.\d+)?\b", str(text or "")):
        raw = match.group(0)
        try:
            number = float(raw)
        except ValueError:
            token = raw
        else:
            token = str(int(number)) if number.is_integer() else str(number)
        if token not in values:
            values.append(token)
    return values


def _revision_number_tokens_without_examples(text: str) -> list[str]:
    cleaned = _REVISION_ILLUSTRATIVE_NUMBER_EXAMPLES_RE.sub("", str(text or ""))
    return _revision_number_tokens(cleaned)


def _revision_semantic_number_tokens(text: str) -> list[str]:
    cleaned = _REVISION_ILLUSTRATIVE_NUMBER_EXAMPLES_RE.sub("", str(text or ""))
    cleaned = _REVISION_BUDGET_AMOUNT_RE.sub(" ", cleaned)
    cleaned = _REVISION_PHASE_REF_RE.sub(" ", cleaned)
    cleaned = _REVISION_SUBTASK_RE.sub(" ", cleaned)
    return _revision_number_tokens(cleaned)


def _revision_number_present(
    required: str,
    present_numbers: set[str],
    positive_clause: str,
) -> bool:
    if required in present_numbers:
        return True
    prefix = f"{required}."
    if any(number.startswith(prefix) for number in present_numbers):
        return True
    if "." in required:
        return False
    if not re.search(rf"(?i)\bphase\s+{re.escape(required)}\b", positive_clause):
        return False
    return any(number.startswith(prefix) for number in present_numbers)


def _revision_is_meta_test_strategy_instruction(revision: str) -> bool:
    text = str(revision or "").lower()
    return (
        "test strategy section" in text
        or "convert high-level statements to concrete executable commands" in text
    )


def _revision_is_optional_instruction(revision: str) -> bool:
    text = str(revision or "").strip().lower()
    return bool(
        re.match(
            r"^(?:optional|polish|nice[-\s]?to[-\s]?have|non[-\s]?blocking|consider|could|may)\b",
            text,
        )
    )


def _revision_is_non_actionable_budget_comment(revision: str) -> bool:
    text = str(revision or "").strip().lower()
    if not re.search(r"(?:[$€£]|\bbudget\b|\busd\b|\bdollars?\b|\bresources?\b)", text):
        return False
    if not re.search(
        r"\b(insufficient|too\s+low|not\s+enough|requires?\s+more\s+resources?|"
        r"cannot\s+realistically|can't\s+realistically|misaligned\s+with\s+scope)\b",
        text,
    ):
        return False
    return not bool(
        re.search(
            r"\b(reduce|increase|set|cap|limit|track|add|remove|change|split|"
            r"scope\s+down|document|allocate|implement|create)\b",
            text,
        )
    )


def _revision_is_success_test_quality_instruction(revision: str) -> bool:
    text = str(revision or "").lower()
    return (
        (
            "success test" in text
            and (
                "file existence" in text
                or "verify behavior" in text
                or "verifies behavior" in text
                or "not just file" in text
            )
        )
        or (
            "success test" in text
            and (
                "cross-platform" in text
                or "cross platform" in text
                or "platform-appropriate" in text
                or "portable" in text
                or "non-portable" in text
                or "portability" in text
                or "python -c" in text
                or "checked-in" in text
                or "checked in" in text
            )
        )
        or (
            "test creation" in text
            and "validation" in text
            and ("split" in text or "separate" in text)
        )
        or (
            "create test" in text
            and "validate" in text
            and ("split" in text or "separate" in text)
        )
        or (
            "empty" in text
            and (
                "test" in text
                or "tests" in text
                or "assertion" in text
                or "assertions" in text
            )
        )
        or "passwithnotests" in text
        or "allowempty" in text
        or "allow no tests" in text
        or (
            "functional tests" in text
            and (
                "assertion" in text
                or "assertions" in text
                or "real assertions" in text
            )
        )
        or (
            "file existence" in text
            or "verify behavior" in text
            or "verifies behavior" in text
            or "not just file" in text
        )
    )


def _revision_rename_issue(plan: dict[str, Any], revision: str) -> str | None:
    match = _REVISION_RENAME_RE.search(str(revision or ""))
    if not match:
        return None
    old_name = match.group(1).strip("`\"'").lower()
    new_name = match.group(2).strip("`\"'").lower()
    plan_text = json.dumps(plan, ensure_ascii=False).lower()
    if new_name not in plan_text:
        return (
            "review rename revision appears unaddressed: "
            f"`{revision}`; missing renamed target `{new_name}`"
        )
    if old_name in plan_text:
        return (
            "review rename revision appears unaddressed: "
            f"`{revision}`; old target `{old_name}` is still present"
        )
    return ""


def _expand_revision_range(start: str, end: str) -> list[str]:
    start_number = _normalise_decimal_revision_number(start)
    end_number = _normalise_decimal_revision_number(end)
    if not start_number or not end_number:
        return []
    start_parts = start_number.split(".")
    end_parts = end_number.split(".")
    if len(start_parts) != len(end_parts) or start_parts[:-1] != end_parts[:-1]:
        return [start_number, end_number]
    try:
        first = int(start_parts[-1])
        last = int(end_parts[-1])
    except ValueError:
        return [start_number, end_number]
    if first > last or last - first > 50:
        return [start_number, end_number]
    prefix = ".".join(start_parts[:-1])
    return [
        f"{prefix}.{idx}" if prefix else str(idx)
        for idx in range(first, last + 1)
    ]


def _revision_target_ids(revision: str) -> list[str]:
    target_ids: list[str] = []

    def add(value: str) -> None:
        target_id = _normalise_revision_subtask_ref(value)
        if target_id and target_id not in target_ids:
            target_ids.append(target_id)

    text = str(revision or "")
    if re.search(r"(?is)\badd\b.{0,160}\bafter\s+subtasks?\b", text):
        return []
    for phase_match in re.finditer(
        r"(?i)\ball\s+phase\s+0*(\d+)\s+subtasks?\b", text
    ):
        add(phase_match.group(1))
    for match in _REVISION_SUBTASK_RE.finditer(text):
        raw_ref = match.group(0)
        range_match = re.search(
            r"(?is)\b(?:subtasks?|st|task)(?:[_-]|\s+)?"
            r"(0*\d+(?:\.\d+)?)\s*-\s*(0*\d+(?:\.\d+)?)\b",
            raw_ref,
        )
        if range_match:
            for number in _expand_revision_range(
                range_match.group(1), range_match.group(2)
            ):
                add(number)
            continue
        add(raw_ref)
    for match in re.finditer(r"(?is)\bsubtasks?\b(?P<body>.{0,260})", text):
        body = re.split(r"\s+-\s+|;|\n", match.group("body"), maxsplit=1)[0]
        if not re.match(
            r"(?is)^\s*(?::|,|\(|\)|\[|\]|\s)*(?:0*\d|subtasks?\b|st\b|task\b)",
            body,
        ):
            continue
        for range_match in re.finditer(
            r"\b(0*\d+(?:\.\d+)?)\s*-\s*(0*\d+(?:\.\d+)?)\b",
            body,
        ):
            for number in _expand_revision_range(range_match.group(1), range_match.group(2)):
                add(number)
        for ref_match in re.finditer(
            r"\b(?:subtasks?|st|task)(?:[_-]|\s+)?0*\d+(?:\.\d+)?[a-z0-9_]*\b",
            body,
            re.IGNORECASE,
        ):
            add(ref_match.group(0))
        for ref_match in re.finditer(r"\b0*\d+(?:\.\d+)?[a-z0-9_]*\b", body):
            add(ref_match.group(0))
    return target_ids


def _plan_text_for_revision_target(plan: dict[str, Any], revision: str) -> tuple[str, str]:
    target_ids = _revision_target_ids(revision)
    target_aliases = [_revision_subtask_aliases(target_id) for target_id in target_ids]
    if not target_ids:
        return json.dumps(plan, ensure_ascii=False), ""
    target_texts: list[str] = []
    matched_ids: list[str] = []
    subtasks = _iter_plan_subtasks(plan)
    for subtask in _iter_plan_subtasks(plan):
        subtask_id = str(
            subtask.get("id")
            or subtask.get("subtask_id")
            or subtask.get("name")
            or ""
        )
        aliases = _revision_subtask_aliases(subtask_id)
        if any(aliases & target for target in target_aliases):
            matched = next(
                target_id for target_id, target in zip(target_ids, target_aliases) if aliases & target
            )
            target_texts.append(json.dumps(subtask, ensure_ascii=False))
            if matched not in matched_ids:
                matched_ids.append(matched)
    if target_texts:
        return "\n".join(target_texts), ", ".join(matched_ids)
    for target_id in target_ids:
        if not re.fullmatch(r"(?:subtask_)?\d+", target_id):
            continue
        prefix = target_id.removeprefix("subtask_") + "."
        coarse_texts: list[str] = []
        for subtask in subtasks:
            subtask_id = str(
                subtask.get("id")
                or subtask.get("subtask_id")
                or subtask.get("name")
                or ""
            )
            aliases = _revision_subtask_aliases(subtask_id)
            if any(alias.startswith(prefix) for alias in aliases):
                coarse_texts.append(json.dumps(subtask, ensure_ascii=False))
        if coarse_texts:
            return "\n".join(coarse_texts), target_id
    return "", target_ids[0]


def _phase_plan_revision_contract_issues(
    ctx: ToolContext | None, plan: dict[str, Any]
) -> list[str]:
    if _umbrella_phase_id(ctx) != "plan":
        return []
    issues: list[str] = []
    for revision in _phase_plan_revision_items(ctx):
        rename_issue = _revision_rename_issue(plan, revision)
        if rename_issue is not None:
            if rename_issue:
                issues.append(rename_issue)
            continue
        if _revision_is_optional_instruction(revision):
            continue
        if _revision_is_non_actionable_budget_comment(revision):
            continue
        if _revision_is_meta_test_strategy_instruction(
            revision
        ) or _revision_is_success_test_quality_instruction(revision):
            continue
        positive_clause = _revision_positive_clause(revision)
        keywords = _revision_keywords(positive_clause)
        if not keywords:
            continue
        target_text, target_id = _plan_text_for_revision_target(plan, revision)
        if target_id and not target_text:
            issues.append(
                f"review revision targets `{target_id}` but the new phase plan has no matching subtask"
            )
            continue
        required_numbers = _revision_semantic_number_tokens(positive_clause)
        if required_numbers:
            present_numbers = set(_revision_number_tokens(target_text))
            missing_numbers = [
                number
                for number in required_numbers
                if not _revision_number_present(
                    number,
                    present_numbers,
                    positive_clause,
                )
            ]
            if missing_numbers:
                target_hint = f" in `{target_id}`" if target_id else ""
                issues.append(
                    "review revision numeric requirement appears unaddressed"
                    f"{target_hint}: `{revision}`; missing number(s): "
                    + ", ".join(missing_numbers[:8])
                )
                continue
        haystack = set(_revision_keywords(target_text))
        covered = [keyword for keyword in keywords if keyword in haystack]
        required = min(len(keywords), min(4, max(2, (len(keywords) + 1) // 2)))
        if len(covered) >= required:
            continue
        missing = [keyword for keyword in keywords if keyword not in haystack]
        target_hint = f" in `{target_id}`" if target_id else ""
        issues.append(
            "review revision appears unaddressed"
            f"{target_hint}: `{revision}`; missing keyword(s): "
            + ", ".join(missing[:8])
        )
    return issues


__all__ = [
    '_phase_plan_revision_items',
    '_revision_positive_clause',
    '_revision_keywords',
    '_normalise_revision_subtask_ref',
    '_normalise_decimal_revision_number',
    '_revision_decimal_aliases',
    '_revision_subtask_aliases',
    '_revision_subtask_ref_numbers',
    '_revision_number_tokens',
    '_revision_number_tokens_without_examples',
    '_revision_semantic_number_tokens',
    '_revision_number_present',
    '_revision_is_meta_test_strategy_instruction',
    '_revision_is_optional_instruction',
    '_revision_is_non_actionable_budget_comment',
    '_revision_is_success_test_quality_instruction',
    '_revision_rename_issue',
    '_expand_revision_range',
    '_revision_target_ids',
    '_plan_text_for_revision_target',
    '_phase_plan_revision_contract_issues',
]
