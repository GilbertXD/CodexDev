"""Model policy validation, selection, locking, escalation, and token tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class InvalidModelError(ValueError):
    """Raised when an unverified or unauthorized model ID is requested."""


VALID_COMPLEXITIES = {"low", "medium", "high"}
VALID_MODES = {"auto", "manual"}


@dataclass(frozen=True)
class ModelSelection:
    model: str
    mode: str  # "auto" | "manual"
    locked: bool
    reason: str
    escalated_from: str | None = None
    reasoning_effort: str | None = None


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


def resolve_task_model_policy(
    config: dict[str, Any],
    *,
    mode: str | None = None,
    requested_model: str | None = None,
    locked: bool | None = None,
    complexity: str = "medium",
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Build and validate a task-level model policy snapshot."""
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
        # Manual mode locks the chosen model unless explicitly unlocked with allow_escalation
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
        reason = (
            f"Auto-selected verified model '{chosen}' for complexity='{eff_complexity}'."
        )

    escalation_order = [
        m for m in (policy.get("escalation_order") or []) if isinstance(m, str) and m in authorized
    ]
    if chosen not in escalation_order and chosen in authorized:
        escalation_order.insert(0, chosen)

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
        "reasoning_effort": reasoning_effort or policy.get("reasoning_effort") or "medium",
        "initial_reason": reason,
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
    escalate: bool = False,
    escalation_reason: str | None = None,
) -> ModelSelection:
    """Select and validate the Builder model for a specific execution/repair round."""
    mode = str(task_model_policy.get("mode") or "auto")
    locked = bool(task_model_policy.get("locked", False))
    allow_escalation = bool(task_model_policy.get("allow_escalation", True)) and not locked
    current = str(task_model_policy.get("current_model") or task_model_policy.get("initial_model") or "")
    effort = task_model_policy.get("reasoning_effort")

    if override_model:
        validated = validate_model_id(override_model, config)
        if locked and validated != current:
            raise InvalidModelError(
                f"Task model is locked to '{current}'. Unlock or start a new task to change to '{validated}'."
            )
        return ModelSelection(
            model=validated,
            mode="manual",
            locked=locked,
            reason=f"Explicit per-round model override to '{validated}'.",
            escalated_from=current if current and current != validated else None,
            reasoning_effort=effort,
        )

    validated_current = validate_model_id(current, config)

    if escalate and repair_round > 0 and mode == "auto" and allow_escalation:
        esc_order = [
            m
            for m in (task_model_policy.get("escalation_order") or [])
            if m in get_authorized_models(config)
        ]
        if validated_current in esc_order:
            idx = esc_order.index(validated_current)
            if idx + 1 < len(esc_order):
                next_model = validate_model_id(esc_order[idx + 1], config)
                return ModelSelection(
                    model=next_model,
                    mode="auto",
                    locked=False,
                    reason=escalation_reason
                    or f"Auto-escalated within authorized models for repair round {repair_round}.",
                    escalated_from=validated_current,
                    reasoning_effort=effort,
                )

    if repair_round == 0:
        reason = str(
            task_model_policy.get("initial_reason")
            or f"Selected '{validated_current}' for initial build round."
        )
    elif locked:
        reason = f"Retaining locked model '{validated_current}' for repair round {repair_round}."
    else:
        reason = f"Retaining verified model '{validated_current}' for repair round {repair_round}."

    return ModelSelection(
        model=validated_current,
        mode=mode,
        locked=locked,
        reason=reason,
        escalated_from=None,
        reasoning_effort=effort,
    )


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
        "token_usage": token_usage,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
