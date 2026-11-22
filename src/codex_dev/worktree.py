"""Isolated Git Worktree management and safe diff/delivery operations."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


class GitWorktreeError(RuntimeError):
    """Raised when a Git worktree or repository inspection operation fails."""


def _run_git(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GitWorktreeError("git binary not found in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitWorktreeError(f"git {' '.join(args)} timed out after {timeout}s") from exc

    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise GitWorktreeError(f"git {' '.join(args)} failed (exit {proc.returncode}): {msg}")
    return proc


def inspect_repo(repo_path: Path) -> dict[str, Any]:
    """Validate that repo_path is inside a Git repository with at least one commit."""
    resolved = Path(repo_path).expanduser().resolve()
    if not resolved.exists():
        raise GitWorktreeError(f"Repository path does not exist: {resolved}")

    top = _run_git(["rev-parse", "--show-toplevel"], resolved).stdout.strip()
    root = Path(top).resolve()

    head_proc = _run_git(["rev-parse", "HEAD"], root, check=False)
    if head_proc.returncode != 0:
        raise GitWorktreeError(
            f"Repository at {root} has no commits yet (HEAD does not exist). "
            "Create an initial commit before launching a worktree task."
        )
    base_commit = head_proc.stdout.strip()

    branch_proc = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root, check=False)
    base_branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else "HEAD"

    status_proc = _run_git(["status", "--porcelain=v1"], root)
    dirty_lines = [ln for ln in status_proc.stdout.splitlines() if ln.strip()]

    return {
        "repo_root": str(root),
        "base_branch": base_branch,
        "base_commit": base_commit,
        "main_repo_dirty": len(dirty_lines) > 0,
        "main_repo_dirty_count": len(dirty_lines),
    }


def create_isolated_worktree(
    repo_root: Path,
    worktree_path: Path,
    branch_name: str,
    base_commit: str,
) -> dict[str, Any]:
    """Create a dedicated Git worktree at base_commit without touching main repo changes."""
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        raise GitWorktreeError(f"Worktree destination already exists: {worktree_path}")

    # Create a new branch rooted strictly at base_commit; leaves main working tree untouched.
    _run_git(
        ["worktree", "add", "-b", branch_name, str(worktree_path), base_commit],
        repo_root,
    )
    return {
        "worktree_path": str(worktree_path.resolve()),
        "branch": branch_name,
        "base_commit": base_commit,
    }


def inspect_worktree_changes(
    worktree_path: Path,
    base_commit: str,
    *,
    include_diff: bool = True,
) -> dict[str, Any]:
    """Inspect complete Git diff (staged + unstaged + committed on branch) and untracked files.

    When include_diff=False, skips building unified diff text and omits full_diff/diff_excerpt
    to keep summary responses compact and fast.
    """
    wt = Path(worktree_path).resolve()
    if not wt.is_dir():
        raise GitWorktreeError(f"Worktree directory does not exist: {wt}")

    # Status shows modified, staged, deleted, renamed, and ALL untracked files (-uall)
    status_proc = _run_git(["status", "--porcelain=v1", "-uall"], wt)
    untracked: list[str] = []
    status_entries: list[dict[str, str]] = []
    for raw_line in status_proc.stdout.splitlines():
        if len(raw_line) < 4:
            continue
        xy = raw_line[:2]
        path_part = raw_line[3:]
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        if xy == "??":
            untracked.append(path_part)
        status_entries.append({"status": xy.strip() or xy, "path": path_part})

    # Diff against base_commit covers both working-tree changes AND any commits made in worktree
    name_status_proc = _run_git(["diff", "--name-status", base_commit], wt)
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    renamed: list[str] = []
    for line in name_status_proc.stdout.splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        code = parts[0]
        if code.startswith("A") and len(parts) >= 2:
            added.append(parts[1])
        elif code.startswith("M") and len(parts) >= 2:
            modified.append(parts[1])
        elif code.startswith("D") and len(parts) >= 2:
            deleted.append(parts[1])
        elif code.startswith("R") and len(parts) >= 3:
            renamed.append(f"{parts[1]} -> {parts[2]}")
            modified.append(parts[2])

    # Numstat against base_commit
    numstat_proc = _run_git(["diff", "--numstat", base_commit], wt)
    file_stats: dict[str, dict[str, Any]] = {}
    total_insertions = 0
    total_deletions = 0

    for line in numstat_proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ins_raw, del_raw, fpath = parts[0], parts[1], parts[2]
        ins = int(ins_raw) if ins_raw.isdigit() else 0
        dels = int(del_raw) if del_raw.isdigit() else 0
        binary = not (ins_raw.isdigit() and del_raw.isdigit())
        total_insertions += ins
        total_deletions += dels
        change_type = "modified"
        if fpath in added:
            change_type = "added"
        elif fpath in deleted:
            change_type = "deleted"
        file_stats[fpath] = {
            "path": fpath,
            "change_type": change_type,
            "insertions": ins,
            "deletions": dels,
            "binary": binary,
        }

    # Include untracked files in file_stats and line counts!
    for uf in untracked:
        if uf not in added:
            added.append(uf)
        fp = wt / uf
        ins_count = 0
        is_binary = False
        if fp.is_file():
            try:
                raw_bytes = fp.read_bytes()
                if b"\x00" in raw_bytes[:4096]:
                    is_binary = True
                else:
                    text = raw_bytes.decode("utf-8", errors="replace")
                    ins_count = len(text.splitlines())
            except OSError:
                pass
        total_insertions += ins_count
        file_stats[uf] = {
            "path": uf,
            "change_type": "untracked",
            "insertions": ins_count,
            "deletions": 0,
            "binary": is_binary,
        }

    all_changed = sorted(set(added + modified + deleted + untracked))
    files_list = sorted(file_stats.values(), key=lambda x: x["path"])

    result: dict[str, Any] = {
        "files_changed_count": len(all_changed),
        "total_insertions": total_insertions,
        "total_deletions": total_deletions,
        "added": sorted(set(added)),
        "modified": sorted(set(modified)),
        "deleted": sorted(set(deleted)),
        "renamed": renamed,
        "untracked": sorted(set(untracked)),
        "all_changed_files": all_changed,
        "files": files_list,
    }
    if include_diff:
        diff_proc = _run_git(["diff", "--no-color", base_commit], wt)
        unified_diff = diff_proc.stdout
        result["diff_excerpt"] = unified_diff[:24000]
        result["diff_truncated"] = len(unified_diff) > 24000
        result["full_diff"] = unified_diff
    return result


def commit_worktree_changes(
    worktree_path: Path,
    commit_message: str,
) -> dict[str, Any]:
    """Commit staged + untracked changes strictly inside the isolated worktree branch.

    MUST only be called after explicit human confirmation in Antigravity chat.
    Never pushes or merges.
    """
    wt = Path(worktree_path).resolve()
    msg = commit_message.strip()
    if not msg:
        raise GitWorktreeError("Commit message must not be empty.")

    status_proc = _run_git(["status", "--porcelain=v1"], wt)
    if not status_proc.stdout.strip():
        head = _run_git(["rev-parse", "HEAD"], wt).stdout.strip()
        return {"committed": False, "reason": "No uncommitted changes in worktree.", "commit": head}

    _run_git(["add", "-A"], wt)
    _run_git(["commit", "-m", msg], wt)
    head = _run_git(["rev-parse", "HEAD"], wt).stdout.strip()
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], wt).stdout.strip()
    return {
        "committed": True,
        "branch": branch,
        "commit": head,
        "message": msg,
    }
