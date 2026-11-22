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
    "verify_timeout_sec": 300,
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
                                if isinstance(r, dict) and isinstance(r.get("effort"), str)
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


def load_config(repo_path: Path | None = None, home_dir: Path | None = None) -> dict[str, Any]:
    """Load merged global + repository configuration."""
    home = home_dir or get_codex_dev_home()
    cfg = copy.deepcopy(DEFAULT_CONFIG)

    global_path = home / "config.json"
    if global_path.is_file():
        try:
            global_data = json.loads(global_path.read_text(encoding="utf-8"))
            if isinstance(global_data, dict):
                cfg = _deep_merge(cfg, global_data)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid global config at {global_path}: {exc}") from exc

    if repo_path is not None:
        repo_cfg_path = Path(repo_path) / ".codex-dev.json"
        if repo_cfg_path.is_file():
            try:
                repo_data = json.loads(repo_cfg_path.read_text(encoding="utf-8"))
                if isinstance(repo_data, dict):
                    cfg = _deep_merge(cfg, repo_data)
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid repository config at {repo_cfg_path}: {exc}") from exc

    # Environment override for codex binary (used by mock testing and custom installs)
    env_bin = os.environ.get("CODEX_DEV_CODEX_BIN")
    if env_bin:
        cfg["codex_binary"] = env_bin

    return cfg


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
        cfg["model_policy"]["reasoning_effort"] = hints["default_effort"]
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
