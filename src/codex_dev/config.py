"""Configuration management and verified local Codex model catalog loading."""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "codex_binary": "codex",
    "sandbox_mode": "workspace-write",
    "max_repair_rounds": 3,
    "codex_timeout_sec": 600,
    "codex_idle_timeout_sec": 300,
    "codex_post_turn_grace_sec": 4,
    "codex_active_grace_sec": 300,
    "codex_active_grace_window_sec": 60,
    "codex_max_active_extensions": 2,
    "codex_timeout_by_effort": {
        "low": 600,
        "medium": 600,
        "high": 1200,
        "xhigh": 1500,
    },
    "codex_timeout_by_complexity": {
        "low": 600,
        "medium": 600,
        "high": 1200,
    },
    "verify_timeout_sec": 300,
    "builder_env": {},
    "builder_add_dirs": [],
    "builder_isolation": "strict",
    "builder": {
        "network_access": False,
        "auto_filter_sandbox_tests": True,
        "skip_tests": [],
    },
    "prepare": {
        "enabled": True,
        "auto_go_mod_tidy": True,
        "commands": [],
        "timeout_sec": 180,
        "env": {},
    },
    "verify": {
        "commands": [],
        "env": {},
    },
    "repo_overrides": {},
    "model_policy": {
        "mode": "auto",
        "locked": False,
        "allow_escalation": True,
        "default_model": None,
        "authorized_models": [],
        "complexity_routing": {
            "low": None,
            "medium": None,
            "high": None,
        },
        "escalation_order": [],
        "reasoning_effort": "medium",
        "effort_locked": False,
        "effort_by_complexity": {
            "low": "low",
            "medium": "medium",
            "high": "high",
        },
        "effort_escalation_order": ["low", "medium", "high", "xhigh"],
        "max_auto_effort": "xhigh",
    },
    "reviewer": {
        "mode": "inherit_session",
        "routing_supported": False,
        "model": None,
    },
    "git": {
        "branch_prefix": "codex-dev/",
        "auto_commit": False,
        "auto_push": False,
        "auto_merge": False,
    },
}


def get_codex_dev_home() -> Path:
    """Return the persistent Codex Dev home directory."""
    env_home = os.environ.get("CODEX_DEV_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".codex-dev").resolve()


def get_codex_home() -> Path:
    """Return the Codex CLI home directory (read-only inspection)."""
    env_codex = os.environ.get("CODEX_HOME")
    if env_codex:
        return Path(env_codex).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def read_local_codex_hints(codex_home: Path | None = None) -> dict[str, Any]:
    """Inspect local Codex CLI config.toml and models_cache.json (read-only).

    Never assumes Codex CLI exposes a live model discovery API; only reads
    already-verified local files when present.
    """
    home = codex_home or get_codex_home()
    default_model: str | None = None
    default_effort: str | None = None
    codex_cli_path: str | None = None
    cached_models: list[dict[str, Any]] = []

    config_toml = home / "config.toml"
    if config_toml.is_file():
        try:
            text = config_toml.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'^\s*model\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if m:
                default_model = m.group(1).strip()
            e = re.search(r'^\s*model_reasoning_effort\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if e:
                default_effort = e.group(1).strip()
            c = re.search(r'^\s*CODEX_CLI_PATH\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if c and Path(c.group(1).strip()).is_file():
                codex_cli_path = c.group(1).strip()
        except OSError:
            pass

    cache_file = home / "models_cache.json"
    if cache_file.is_file():
        try:
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
            models = raw.get("models", raw if isinstance(raw, list) else [])
            for item in models:
                if not isinstance(item, dict):
                    continue
                slug = item.get("slug")
                vis = item.get("visibility", "list")
                if isinstance(slug, str) and slug.strip() and vis == "list":
                    cached_models.append(
                        {
                            "slug": slug.strip(),
                            "display_name": str(item.get("display_name") or slug.strip()),
                            "default_reasoning_level": item.get("default_reasoning_level"),
                            "supported_efforts": [
                                r.get("effort")
                                for r in item.get("supported_reasoning_levels", [])
                                if isinstance(r, dict)
                                and isinstance(r.get("effort"), str)
                                and r.get("effort").strip().lower() in ("low", "medium", "high", "xhigh")
                            ],
                        }
                    )
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "default_model": default_model,
        "default_effort": default_effort,
        "codex_cli_path": codex_cli_path,
        "cached_models": cached_models,
    }


def _resolve_repo_override(repo_overrides: Any, repo_path: Path) -> dict[str, Any] | None:
    """Return matching per-repository override dict from global config.repo_overrides."""
    if not isinstance(repo_overrides, dict):
        return None
    resolved_target = Path(repo_path).expanduser().resolve()
    for raw_key, value in repo_overrides.items():
        if not isinstance(raw_key, str) or not raw_key.strip() or not isinstance(value, dict):
            continue
        try:
            candidate = Path(raw_key.strip()).expanduser().resolve()
        except OSError:
            continue
        if candidate == resolved_target:
            return value
    return None


def load_config(repo_path: Path | None = None, home_dir: Path | None = None) -> dict[str, Any]:
    """Load merged global + repository configuration.

    Precedence (lowest to highest):
      1. DEFAULT_CONFIG
      2. ~/.codex-dev/config.json (global defaults)
      3. <repo>/.codex-dev.json (shared repository config)
      4. ~/.codex-dev/config.json -> repo_overrides[<resolved_repo_path>] (non-intrusive local per-repo overrides)
      5. <repo>/.codex-dev.local.json (machine-local repository overrides)
      6. CODEX_DEV_CODEX_BIN environment variable
    """
    home = home_dir or get_codex_dev_home()
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    global_repo_overrides: Any = None

    global_path = home / "config.json"
    if global_path.is_file():
        try:
            global_data = json.loads(global_path.read_text(encoding="utf-8"))
            if isinstance(global_data, dict):
                global_repo_overrides = global_data.get("repo_overrides")
                cfg = _deep_merge(cfg, global_data)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid global config at {global_path}: {exc}") from exc

    if repo_path is not None:
        repo_root = Path(repo_path)
        repo_cfg_path = repo_root / ".codex-dev.json"
        if repo_cfg_path.is_file():
            try:
                repo_data = json.loads(repo_cfg_path.read_text(encoding="utf-8"))
                if isinstance(repo_data, dict):
                    cfg = _deep_merge(cfg, repo_data)
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid repository config at {repo_cfg_path}: {exc}") from exc

        repo_override = _resolve_repo_override(global_repo_overrides, repo_root)
        if repo_override:
            override_clean = {k: v for k, v in repo_override.items() if k != "repo_overrides"}
            cfg = _deep_merge(cfg, override_clean)

        repo_local_cfg_path = repo_root / ".codex-dev.local.json"
        if repo_local_cfg_path.is_file():
            try:
                local_data = json.loads(repo_local_cfg_path.read_text(encoding="utf-8"))
                if isinstance(local_data, dict):
                    cfg = _deep_merge(cfg, local_data)
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid local repository config at {repo_local_cfg_path}: {exc}") from exc

    # Environment override for codex binary (used by mock testing and custom installs)
    env_bin = os.environ.get("CODEX_DEV_CODEX_BIN")
    if env_bin:
        cfg["codex_binary"] = env_bin

    return cfg


def resolve_codex_timeouts(
    config: dict[str, Any],
    *,
    explicit_timeout_sec: int | None = None,
    explicit_idle_timeout_sec: int | None = None,
    complexity: str | None = None,
    reasoning_effort: str | None = None,
) -> tuple[int, int]:
    """Resolve effective (total_timeout_sec, idle_timeout_sec) dynamically.

    - Explicit caller overrides always win.
    - Otherwise total timeout scales up using codex_timeout_by_effort and
      codex_timeout_by_complexity (e.g., high -> 1200s) so deep reasoning runs
      are not prematurely killed while still emitting events.
    - Idle timeout catches genuine hangs with zero output events (0 disables).
    """
    if explicit_timeout_sec is not None:
        total_timeout = max(1, int(explicit_timeout_sec))
    else:
        base_timeout = int(config.get("codex_timeout_sec", 600))
        candidates = [base_timeout]
        effort_map = config.get("codex_timeout_by_effort") or {}
        if reasoning_effort and isinstance(effort_map, dict) and reasoning_effort in effort_map:
            try:
                candidates.append(int(effort_map[reasoning_effort]))
            except (TypeError, ValueError):
                pass
        comp_map = config.get("codex_timeout_by_complexity") or {}
        if complexity and isinstance(comp_map, dict) and complexity in comp_map:
            try:
                candidates.append(int(comp_map[complexity]))
            except (TypeError, ValueError):
                pass
        total_timeout = max(1, max(candidates))

    if explicit_idle_timeout_sec is not None:
        idle_timeout = max(0, int(explicit_idle_timeout_sec))
    else:
        raw_idle = config.get("codex_idle_timeout_sec", 300)
        idle_timeout = max(0, int(raw_idle)) if raw_idle is not None else 0
        if idle_timeout > 0 and explicit_timeout_sec is not None:
            idle_timeout = min(idle_timeout, total_timeout)

    if idle_timeout > total_timeout:
        idle_timeout = total_timeout

    return total_timeout, idle_timeout


def ensure_default_global_config(
    home_dir: Path | None = None,
    codex_home: Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Create ~/.codex-dev/config.json seeded from verified local Codex models if absent."""
    home = home_dir or get_codex_dev_home()
    home.mkdir(parents=True, exist_ok=True)
    cfg_path = home / "config.json"
    if cfg_path.exists() and not overwrite:
        return cfg_path

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    hints = read_local_codex_hints(codex_home)
    slugs = [m["slug"] for m in hints["cached_models"]]
    default_model = hints["default_model"]

    if default_model and default_model not in slugs:
        slugs.append(default_model)

    if slugs:
        cfg["model_policy"]["authorized_models"] = slugs
        chosen_default = default_model if default_model in slugs else slugs[0]
        cfg["model_policy"]["default_model"] = chosen_default
        # Pick conservative verified low/medium/high defaults from the verified list only.
        low_model = chosen_default
        med_model = chosen_default
        high_model = slugs[0]
        cfg["model_policy"]["complexity_routing"] = {
            "low": low_model,
            "medium": med_model,
            "high": high_model,
        }
        # Escalation order: default first, then top visible verified model if distinct
        esc: list[str] = [chosen_default]
        if high_model not in esc:
            esc.append(high_model)
        cfg["model_policy"]["escalation_order"] = esc

    if hints["default_effort"]:
        raw_effort = str(hints["default_effort"]).strip().lower()
        if raw_effort in ("low", "medium", "high", "xhigh"):
            cfg["model_policy"]["reasoning_effort"] = raw_effort
        elif raw_effort in ("max", "ultra"):
            cfg["model_policy"]["reasoning_effort"] = "high"
    if hints.get("codex_cli_path"):
        cfg["codex_binary"] = hints["codex_cli_path"]

    tmp_path = home / f".config.json.tmp.{os.getpid()}"
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, cfg_path)
    return cfg_path
