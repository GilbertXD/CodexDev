"""Model policy validation, selection, locking, escalation, effort routing, and token tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class InvalidModelError(ValueError):
    """Raised when an unverified/unauthorized model ID or disallowed reasoning effort is requested."""


VALID_COMPLEXITIES = {"low", "medium", "high"}
COMPLEXITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
VALID_MODES = {"auto", "manual"}
ALLOWED_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh")
DISALLOWED_EFFORTS: frozenset[str] = frozenset({"max", "ultra"})
DEFAULT_EFFORT_BY_COMPLEXITY: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
}


@dataclass(frozen=True)
class ModelSelection:
    model: str
    mode: str  # "auto" | "manual"
    locked: bool
    reason: str
    escalated_from: str | None = None
    reasoning_effort: str | None = None
    effort_escalated_from: str | None = None
    effort_locked: bool = False


def get_authorized_models(config: dict[str, Any]) -> list[str]:
    """Return deduplicated list of user-authorized verified model IDs."""
    policy = config.get("model_policy") or {}
    raw = policy.get("authorized_models") or []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            val = item.strip()
            if val and val not in seen:
                seen.add(val)
                out.append(val)
    return out


def validate_model_id(model_id: str | None, config: dict[str, Any]) -> str:
    """Strictly validate that model_id is non-empty and in authorized_models.

    Never substitutes an unauthorized model silently.
    """
    if not isinstance(model_id, str) or not model_id.strip():
        raise InvalidModelError(
            "No verified Builder model specified. Configure authorized_models in "
            "~/.codex-dev/config.json or specify an authorized --model."
        )
    candidate = model_id.strip()
    authorized = get_authorized_models(config)
    if not authorized:
        raise InvalidModelError(
            f"Cannot use model '{candidate}': authorized_models list is empty. "
            "Configure verified authorized_models in ~/.codex-dev/config.json first."
        )
    if candidate not in authorized:
        raise InvalidModelError(
            f"Model '{candidate}' is not in the verified authorized_models list: "
            f"{', '.join(authorized)}. Unauthorized or unverified model IDs are rejected."
        )
    return candidate


def _effort_rank(effort: str) -> int:
    try:
        return ALLOWED_EFFORTS.index(effort)
    except ValueError:
        return -1


def get_model_supported_efforts(
    model_id: str,
    cached_models: list[dict[str, Any]] | None = None,
) -> list[str] | None:
    """Return allowed efforts supported by model_id from local models cache, or None if unknown."""
    if not cached_models:
        return None
    for item in cached_models:
        if not isinstance(item, dict) or item.get("slug") != model_id:
            continue
        raw = item.get("supported_efforts")
        if isinstance(raw, list):
            filtered = [
                str(e).strip().lower()
                for e in raw
                if isinstance(e, str) and str(e).strip().lower() in ALLOWED_EFFORTS
            ]
            if filtered:
                # Preserve canonical ascending effort order
                return [e for e in ALLOWED_EFFORTS if e in filtered]
    return None


def _normalize_max_auto_effort(raw_max: Any) -> str:
    if isinstance(raw_max, str):
        val = raw_max.strip().lower()
        if val in ALLOWED_EFFORTS:
            return val
        if val in DISALLOWED_EFFORTS:
            return "xhigh"
    return "xhigh"


def _normalize_effort_order(raw_order: Any, max_auto_effort: str) -> list[str]:
    cap_rank = _effort_rank(max_auto_effort)
    seen: set[str] = set()
    out: list[str] = []
    if isinstance(raw_order, list):
        for item in raw_order:
            if not isinstance(item, str):
                continue
            val = item.strip().lower()
            if val in ALLOWED_EFFORTS and _effort_rank(val) <= cap_rank and val not in seen:
                seen.add(val)
                out.append(val)
    if not out:
        out = [e for e in ALLOWED_EFFORTS if _effort_rank(e) <= cap_rank]
    return out


def validate_reasoning_effort(
    effort: str | None,
    *,
    model_id: str | None = None,
    cached_models: list[dict[str, Any]] | None = None,
    max_effort: str | None = None,
    clamp: bool = False,
) -> tuple[str, str | None]:
    """Validate (and optionally clamp in auto mode) reasoning effort.

    `max` and `ultra` are permanently disabled. Explicit requests always raise InvalidModelError.
    """
    if not isinstance(effort, str) or not effort.strip():
        raise InvalidModelError(
            f"Reasoning effort must be one of: {', '.join(ALLOWED_EFFORTS)}."
        )
    cand = effort.strip().lower()
    note_parts: list[str] = []

    if cand in DISALLOWED_EFFORTS:
        if not clamp:
            raise InvalidModelError(
                f"Reasoning effort '{cand}' is permanently disabled in Codex Dev "
                f"(disallowed: {', '.join(sorted(DISALLOWED_EFFORTS))}; "
                f"allowed: {', '.join(ALLOWED_EFFORTS)})."
            )
        clamped = _normalize_max_auto_effort(max_effort)
        note_parts.append(f"clamped disabled effort '{cand}' to '{clamped}'")
        cand = clamped

    if cand not in ALLOWED_EFFORTS:
        raise InvalidModelError(
            f"Invalid reasoning effort '{cand}'. Allowed efforts: {', '.join(ALLOWED_EFFORTS)} "
            f"(permanently disabled: {', '.join(sorted(DISALLOWED_EFFORTS))})."
        )

    if max_effort is not None and clamp:
        cap = _normalize_max_auto_effort(max_effort)
        if _effort_rank(cand) > _effort_rank(cap):
            note_parts.append(f"capped effort '{cand}' at max_auto_effort='{cap}'")
            cand = cap

    supported = get_model_supported_efforts(model_id, cached_models) if model_id else None
    if supported:
        if cand not in supported:
            if not clamp:
                raise InvalidModelError(
                    f"Reasoning effort '{cand}' is not supported by model '{model_id}' "
                    f"(supported allowed efforts: {', '.join(supported)})."
                )
            # Clamp downward to highest supported effort <= cand, else lowest supported
            lower = [e for e in supported if _effort_rank(e) <= _effort_rank(cand)]
            fallback = lower[-1] if lower else supported[0]
            note_parts.append(f"clamped unsupported effort '{cand}' for '{model_id}' to '{fallback}'")
            cand = fallback

    return cand, ("; ".join(note_parts) if note_parts else None)


def resolve_task_model_policy(
    config: dict[str, Any],
    *,
    mode: str | None = None,
    requested_model: str | None = None,
    locked: bool | None = None,
    complexity: str = "medium",
    reasoning_effort: str | None = None,
    lock_effort: bool | None = None,
    cached_models: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build and validate a task-level 2D (model, reasoning_effort) policy snapshot."""
    policy = config.get("model_policy") or {}
    eff_complexity = complexity if complexity in VALID_COMPLEXITIES else "medium"

    if requested_model:
        eff_mode = "manual" if mode is None else mode
    else:
        eff_mode = mode or str(policy.get("mode") or "auto")

    if eff_mode not in VALID_MODES:
        raise InvalidModelError(f"Invalid model policy mode '{eff_mode}'; expected 'auto' or 'manual'.")

    eff_locked = bool(policy.get("locked", False)) if locked is None else bool(locked)
    allow_escalation = bool(policy.get("allow_escalation", True)) and not eff_locked
    authorized = get_authorized_models(config)

    if eff_mode == "manual":
        chosen = validate_model_id(requested_model or policy.get("default_model"), config)
        if locked is None:
            eff_locked = True
            allow_escalation = False
        reason = (
            f"Manual model '{chosen}' locked for entire task."
            if eff_locked
            else f"Manual model '{chosen}' selected by user."
        )
    else:
        routing = policy.get("complexity_routing") or {}
        candidate = (
            requested_model
            or routing.get(eff_complexity)
            or policy.get("default_model")
            or (authorized[0] if authorized else None)
        )
        chosen = validate_model_id(candidate, config)
        reason = f"Auto-selected verified model '{chosen}' for complexity='{eff_complexity}'."

    escalation_order = [
        m for m in (policy.get("escalation_order") or []) if isinstance(m, str) and m in authorized
    ]
    if chosen not in escalation_order and chosen in authorized:
        escalation_order.insert(0, chosen)

    # Resolve 2D reasoning_effort policy (permanently banning max and ultra)
    max_auto_effort = _normalize_max_auto_effort(policy.get("max_auto_effort", "xhigh"))
    effort_escalation_order = _normalize_effort_order(policy.get("effort_escalation_order"), max_auto_effort)

    raw_effort_map = policy.get("effort_by_complexity")
    effort_by_complexity: dict[str, str] = {}
    for tier, default_val in DEFAULT_EFFORT_BY_COMPLEXITY.items():
        raw_tier_val = raw_effort_map.get(tier) if isinstance(raw_effort_map, dict) else None
        if isinstance(raw_tier_val, str) and raw_tier_val.strip():
            validated_tier, _ = validate_reasoning_effort(
                raw_tier_val,
                max_effort=max_auto_effort,
                clamp=True,
            )
            effort_by_complexity[tier] = validated_tier
        else:
            effort_by_complexity[tier] = default_val

    if reasoning_effort is not None:
        # Explicit user effort: strict validation (rejects max/ultra immediately)
        chosen_effort, clamp_note = validate_reasoning_effort(
            reasoning_effort,
            model_id=chosen,
            cached_models=cached_models,
            clamp=False,
        )
        eff_effort_locked = True if lock_effort is None else bool(lock_effort)
        effort_reason = f"explicit reasoning_effort='{chosen_effort}'"
        if eff_effort_locked:
            effort_reason += " (locked)"
    else:
        eff_effort_locked = bool(policy.get("effort_locked", False)) if lock_effort is None else bool(lock_effort)
        if eff_mode == "auto":
            cand_effort = effort_by_complexity.get(eff_complexity) or str(policy.get("reasoning_effort") or "medium")
            chosen_effort, clamp_note = validate_reasoning_effort(
                cand_effort,
                model_id=chosen,
                cached_models=cached_models,
                max_effort=max_auto_effort,
                clamp=True,
            )
            effort_reason = f"effort='{chosen_effort}' for complexity='{eff_complexity}'"
        else:
            cand_effort = str(policy.get("reasoning_effort") or effort_by_complexity.get(eff_complexity) or "medium")
            chosen_effort, clamp_note = validate_reasoning_effort(
                cand_effort,
                model_id=chosen,
                cached_models=cached_models,
                max_effort=max_auto_effort,
                clamp=True,
            )
            effort_reason = f"effort='{chosen_effort}'"
        if clamp_note:
            effort_reason += f" ({clamp_note})"

    full_reason = f"{reason} [{effort_reason}]"

    reviewer_cfg = config.get("reviewer") or {}
    reviewer_routing_supported = bool(reviewer_cfg.get("routing_supported", False))
    reviewer_model = (
        reviewer_cfg.get("model")
        if reviewer_routing_supported and reviewer_cfg.get("model")
        else "inherit_session"
    )

    return {
        "mode": eff_mode,
        "locked": eff_locked,
        "allow_escalation": allow_escalation,
        "complexity": eff_complexity,
        "initial_model": chosen,
        "current_model": chosen,
        "authorized_models": authorized,
        "escalation_order": escalation_order,
        "initial_reasoning_effort": chosen_effort,
        "current_reasoning_effort": chosen_effort,
        "reasoning_effort": chosen_effort,
        "effort_locked": eff_effort_locked,
        "effort_by_complexity": effort_by_complexity,
        "effort_escalation_order": effort_escalation_order,
        "max_auto_effort": max_auto_effort,
        "initial_reason": full_reason,
        "reviewer_mode": "explicit" if reviewer_routing_supported and reviewer_cfg.get("model") else "inherit_session",
        "reviewer_model": reviewer_model,
        "reviewer_routing_supported": reviewer_routing_supported,
    }


def select_model_for_round(
    task_model_policy: dict[str, Any],
    config: dict[str, Any],
    *,
    repair_round: int,
    override_model: str | None = None,
    override_effort: str | None = None,
    escalate: bool = False,
    escalation_reason: str | None = None,
    failure_category: str | None = None,
    cached_models: list[dict[str, Any]] | None = None,
) -> ModelSelection:
    """Select and validate the Builder (model, reasoning_effort) pair for an execution/repair round."""
    mode = str(task_model_policy.get("mode") or "auto")
    locked = bool(task_model_policy.get("locked", False))
    allow_escalation = bool(task_model_policy.get("allow_escalation", True)) and not locked
    current = str(task_model_policy.get("current_model") or task_model_policy.get("initial_model") or "")
    current_effort_raw = str(
        task_model_policy.get("current_reasoning_effort")
        or task_model_policy.get("reasoning_effort")
        or "medium"
    )
    effort_locked = bool(task_model_policy.get("effort_locked", False))
    max_auto_effort = _normalize_max_auto_effort(task_model_policy.get("max_auto_effort", "xhigh"))
    effort_order = _normalize_effort_order(task_model_policy.get("effort_escalation_order"), max_auto_effort)

    validated_current = validate_model_id(current, config)
    validated_current_effort, _ = validate_reasoning_effort(
        current_effort_raw,
        model_id=validated_current,
        cached_models=cached_models,
        max_effort=max_auto_effort,
        clamp=True,
    )

    chosen_model = validated_current
    chosen_mode = mode
    model_escalated_from: str | None = None
    reason_parts: list[str] = []

    if override_model:
        validated = validate_model_id(override_model, config)
        if locked and validated != validated_current:
            raise InvalidModelError(
                f"Task model is locked to '{validated_current}'. Unlock or start a new task to change to '{validated}'."
            )
        chosen_model = validated
        chosen_mode = "manual"
        if validated != validated_current:
            model_escalated_from = validated_current
        reason_parts.append(f"Explicit per-round model override to '{validated}'.")
    elif escalate and repair_round > 0 and mode == "auto" and allow_escalation:
        esc_order = [
            m
            for m in (task_model_policy.get("escalation_order") or [])
            if m in get_authorized_models(config)
        ]
        if validated_current in esc_order:
            idx = esc_order.index(validated_current)
            if idx + 1 < len(esc_order):
                chosen_model = validate_model_id(esc_order[idx + 1], config)
                model_escalated_from = validated_current
                reason_parts.append(
                    escalation_reason
                    or f"Auto-escalated model from '{validated_current}' to '{chosen_model}' for repair round {repair_round}."
                )

    chosen_effort = validated_current_effort
    effort_escalated_from: str | None = None

    if override_effort is not None:
        validated_eff, _ = validate_reasoning_effort(
            override_effort,
            model_id=chosen_model,
            cached_models=cached_models,
            clamp=False,
        )
        if effort_locked and validated_eff != validated_current_effort:
            raise InvalidModelError(
                f"Task reasoning effort is locked to '{validated_current_effort}'. "
                f"Unlock or start a new task to change to '{validated_eff}'."
            )
        if validated_eff != validated_current_effort:
            effort_escalated_from = validated_current_effort
        chosen_effort = validated_eff
        reason_parts.append(f"Explicit per-round reasoning_effort override to '{chosen_effort}'.")
    else:
        # Clamp current effort to chosen_model capabilities if model changed
        chosen_effort, clamp_note = validate_reasoning_effort(
            validated_current_effort,
            model_id=chosen_model,
            cached_models=cached_models,
            max_effort=max_auto_effort,
            clamp=True,
        )
        if clamp_note:
            reason_parts.append(clamp_note)

        # Dynamic effort escalation on repair rounds when model did not escalate
        # and failure was NOT a straightforward review_failure.
        should_escalate_effort = (
            escalate
            and repair_round > 0
            and mode == "auto"
            and not effort_locked
            and model_escalated_from is None
            and failure_category != "review_failure"
        )
        if should_escalate_effort:
            supported = get_model_supported_efforts(chosen_model, cached_models)
            effective_order = [e for e in effort_order if not supported or e in supported]
            if chosen_effort in effective_order:
                e_idx = effective_order.index(chosen_effort)
                if e_idx + 1 < len(effective_order):
                    next_effort = effective_order[e_idx + 1]
                    effort_escalated_from = chosen_effort
                    chosen_effort = next_effort
                    reason_parts.append(
                        f"Auto-escalated reasoning_effort from '{effort_escalated_from}' to '{chosen_effort}' "
                        f"for repair round {repair_round} (failure={failure_category or 'unspecified'})."
                    )

    if not reason_parts:
        if repair_round == 0:
            reason_parts.append(
                str(
                    task_model_policy.get("initial_reason")
                    or f"Selected '{chosen_model}' (effort='{chosen_effort}') for initial build round."
                )
            )
        elif locked:
            reason_parts.append(
                f"Retaining locked model '{chosen_model}' (effort='{chosen_effort}') for repair round {repair_round}."
            )
        elif failure_category == "review_failure":
            reason_parts.append(
                f"Retaining verified model '{chosen_model}' and effort='{chosen_effort}' for targeted review repair round {repair_round}."
            )
        else:
            reason_parts.append(
                f"Retaining verified model '{chosen_model}' (effort='{chosen_effort}') for repair round {repair_round}."
            )

    return ModelSelection(
        model=chosen_model,
        mode=chosen_mode,
        locked=locked,
        reason=" ".join(reason_parts),
        escalated_from=model_escalated_from,
        reasoning_effort=chosen_effort,
        effort_escalated_from=effort_escalated_from,
        effort_locked=effort_locked,
    )


def _classify_target_layer(path: str) -> str:
    p = path.strip().lower()
    if "migration" in p or p.endswith(".sql") or p.endswith("schema.prisma"):
        return "migration"
    if (
        p.startswith("tests/")
        or "/tests/" in p
        or "/test/" in p
        or "/__tests__/" in p
        or p.endswith("_test.go")
        or p.endswith("_test.py")
        or p.endswith("_test.rs")
        or ".test." in p
        or ".spec." in p
        or p.split("/")[-1].startswith("test_")
    ):
        return "tests"
    if p.endswith(".md") or p.startswith("docs/") or "/docs/" in p:
        return "docs"
    base = p.split("/")[-1]
    if (
        base.startswith("dockerfile")
        or base.startswith("docker-compose")
        or base.startswith(".env")
        or base in {"makefile", "pyproject.toml", "package.json", "go.mod", "cargo.toml"}
        or p.startswith("configs/")
        or "/configs/" in p
        or base.endswith((".yaml", ".yml", ".toml"))
    ):
        return "config_deploy"
    return "source"


def assess_plan_complexity(
    contract: dict[str, Any] | None,
    *,
    high_target_files: int = 8,
    high_criteria: int = 6,
) -> tuple[str, list[str]]:
    """Deterministically estimate minimum complexity tier from a locked PLAN.md contract."""
    if not isinstance(contract, dict):
        return ("low", [])
    target_files = [str(f).strip() for f in (contract.get("target_files") or []) if str(f).strip()]
    criteria = [str(c).strip() for c in (contract.get("acceptance_criteria") or []) if str(c).strip()]
    layers = {_classify_target_layer(f) for f in target_files}

    reasons: list[str] = []
    if len(target_files) >= max(1, int(high_target_files)):
        reasons.append(f"{len(target_files)} target files (>= {int(high_target_files)})")
    if len(criteria) >= max(1, int(high_criteria)) and len(target_files) >= 4:
        reasons.append(f"{len(criteria)} acceptance criteria across {len(target_files)} target files")
    if {"migration", "source", "tests"}.issubset(layers):
        reasons.append("cross-layer scope spanning database migrations, source code, and tests")
    if reasons:
        return ("high", reasons)

    med_reasons: list[str] = []
    if len(target_files) >= 4:
        med_reasons.append(f"{len(target_files)} target files")
    if {"source", "tests"}.issubset(layers) and len(criteria) >= 3:
        med_reasons.append(f"source + tests with {len(criteria)} acceptance criteria")
    if med_reasons:
        return ("medium", med_reasons)

    return ("low", [])


def promote_policy_for_plan_scope(
    task_model_policy: dict[str, Any],
    config: dict[str, Any],
    contract: dict[str, Any] | None,
    *,
    cached_models: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Promote auto-routed task complexity/model/effort upward when locked PLAN.md scope warrants it.

    Strictly preserves manual mode, locked models, locked efforts, and authorized_models validation.
    Never downgrades complexity.
    """
    policy_cfg = config.get("model_policy") or {}
    if not bool(policy_cfg.get("auto_promote_by_plan_scope", True)):
        return {"promoted": False}
    if str(task_model_policy.get("mode") or "auto") != "auto":
        return {"promoted": False}
    if bool(task_model_policy.get("locked", False)):
        return {"promoted": False}

    cur_comp = str(task_model_policy.get("complexity") or "medium")
    if cur_comp not in COMPLEXITY_RANK:
        cur_comp = "medium"

    high_tf = int(policy_cfg.get("plan_scope_high_target_files", 8))
    high_ac = int(policy_cfg.get("plan_scope_high_criteria", 6))
    recommended, reasons = assess_plan_complexity(
        contract,
        high_target_files=high_tf,
        high_criteria=high_ac,
    )
    if COMPLEXITY_RANK.get(recommended, 0) <= COMPLEXITY_RANK[cur_comp]:
        return {"promoted": False}

    routing = policy_cfg.get("complexity_routing") or {}
    authorized = get_authorized_models(config)
    old_model = str(task_model_policy.get("current_model") or task_model_policy.get("initial_model") or "")
    cand_model = routing.get(recommended) or old_model or policy_cfg.get("default_model") or (authorized[0] if authorized else None)
    new_model = validate_model_id(cand_model, config)

    old_effort = str(
        task_model_policy.get("current_reasoning_effort")
        or task_model_policy.get("reasoning_effort")
        or "medium"
    )
    effort_locked = bool(task_model_policy.get("effort_locked", False))
    max_auto_effort = _normalize_max_auto_effort(task_model_policy.get("max_auto_effort", "xhigh"))
    effort_map = task_model_policy.get("effort_by_complexity") or DEFAULT_EFFORT_BY_COMPLEXITY

    if effort_locked:
        new_effort = old_effort
    else:
        cand_effort = str(effort_map.get(recommended) or old_effort)
        validated_effort, _ = validate_reasoning_effort(
            cand_effort,
            model_id=new_model,
            cached_models=cached_models,
            max_effort=max_auto_effort,
            clamp=True,
        )
        # Never downgrade effort when promoting scope
        new_effort = (
            validated_effort
            if _effort_rank(validated_effort) >= _effort_rank(old_effort)
            else old_effort
        )

    reason_text = (
        f"Auto-promoted complexity from '{cur_comp}' to '{recommended}' based on PLAN.md scope "
        f"({'; '.join(reasons)}): model '{old_model}' -> '{new_model}', effort '{old_effort}' -> '{new_effort}'."
    )

    task_model_policy["complexity"] = recommended
    task_model_policy["scope_promoted_from"] = cur_comp
    task_model_policy["scope_promotion_reasons"] = reasons
    task_model_policy["initial_model"] = new_model
    task_model_policy["current_model"] = new_model
    task_model_policy["initial_reasoning_effort"] = new_effort
    task_model_policy["current_reasoning_effort"] = new_effort
    task_model_policy["reasoning_effort"] = new_effort
    task_model_policy["initial_reason"] = reason_text

    esc_order = list(task_model_policy.get("escalation_order") or [])
    if new_model not in esc_order and new_model in authorized:
        esc_order.insert(0, new_model)
        task_model_policy["escalation_order"] = esc_order

    return {
        "promoted": True,
        "from_complexity": cur_comp,
        "to_complexity": recommended,
        "from_model": old_model,
        "to_model": new_model,
        "from_effort": old_effort,
        "to_effort": new_effort,
        "reasons": reasons,
        "reason": reason_text,
    }


def make_model_history_entry(
    *,
    round_num: int,
    role: str,
    selection: ModelSelection | None = None,
    model_name: str | None = None,
    mode: str = "auto",
    reason: str = "",
    token_usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a structured per-round model usage ledger record."""
    return {
        "round": round_num,
        "role": role,
        "model": selection.model if selection else (model_name or "unknown"),
        "mode": selection.mode if selection else mode,
        "locked": selection.locked if selection else False,
        "reason": selection.reason if selection else reason,
        "escalated_from": selection.escalated_from if selection else None,
        "reasoning_effort": selection.reasoning_effort if selection else None,
        "effort_escalated_from": selection.effort_escalated_from if selection else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "token_usage": token_usage,
    }
