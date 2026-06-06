# -*- coding: utf-8 -*-
"""Git 操作封装：统一通过 GitPython 调用，不再直接 subprocess git.exe。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from git import Git, Repo
from git.exc import GitCommandError, GitCommandNotFound, InvalidGitRepositoryError

GIT_HTTP_CONNECT_TIMEOUT_SEC = 5


def _on_windows() -> bool:
    return sys.platform == "win32"


def _safe_directory_option(repo_dir: Path | None) -> list[str]:
    if repo_dir is None:
        return []
    safe_path = str(repo_dir.resolve()).replace("\\", "/")
    return ["-c", f"safe.directory={safe_path}"]


def git_multi_options(repo_dir: Path | None = None) -> list[str]:
    opts = ["-c", f"http.connectTimeout={GIT_HTTP_CONNECT_TIMEOUT_SEC}"]
    opts.extend(_safe_directory_option(repo_dir))
    return opts


def _git_instance(cwd: Path | str | None = None, repo_dir: Path | None = None) -> Git:
    """创建 Git 命令封装；兼容不同 GitPython 版本。"""
    opts = git_multi_options(repo_dir)
    cwd_str = str(cwd) if cwd else None
    try:
        return Git(cwd_str, git_options=opts)
    except TypeError:
        g = Git(cwd_str)
        base = list(getattr(g, "_git_options", None) or [])
        g._git_options = base + list(opts)
        return g


def _run_env(extra: dict | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_HTTP_CONNECT_TIMEOUT"] = str(GIT_HTTP_CONNECT_TIMEOUT_SEC)
    if extra:
        env.update(extra)
    return env


def _format_git_error(exc: GitCommandError) -> str:
    parts = []
    for part in (exc.stdout, exc.stderr, str(exc)):
        text = (part or "").strip() if isinstance(part, str) else ""
        if text and text not in parts:
            parts.append(text)
    return "\n".join(parts) if parts else str(exc)


def check_git_available() -> tuple[bool, str]:
    try:
        version = _git_instance().version()
        return True, (version or "").strip()
    except GitCommandNotFound as exc:
        return False, f"GitPython 无法调用 git（请确认 Git 在 PATH 中）: {exc}"
    except Exception as exc:
        return False, str(exc)


def execute_git(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    repo_dir: Path | None = None,
    timeout: float | None = 120,
    env: dict | None = None,
) -> tuple[bool, str]:
    """执行 git 子命令（不含 'git' 前缀）。

    GitPython 的 Git.execute() 要求 args[0] == 'git'，
    此函数自动处理：调用方传不含 'git' 前缀的子命令即可。
    """
    if not args:
        return False, "empty git command"
    repo_path = repo_dir or (Path(cwd) if cwd else None)
    g = _git_instance(cwd, repo_path)
    run_env = _run_env(env)
    exec_args = list(args)
    # 确保 args[0] 是 'git'，GitPython 的 execute() 要求如此
    if exec_args[0] != "git":
        exec_args = ["git"] + exec_args
    if not getattr(g, "_git_options", None):
        exec_args = [exec_args[0]] + git_multi_options(repo_path) + exec_args[1:]
    exec_kwargs: dict = {
        "with_exceptions": True,
        "as_process": False,
        "stdout_as_string": True,
        "env": run_env,
    }
    if timeout and timeout > 0 and not _on_windows():
        exec_kwargs["kill_after_timeout"] = int(timeout)
    try:
        out = g.execute(exec_args, **exec_kwargs)
        return True, (out or "").strip()
    except GitCommandError as exc:
        return False, _format_git_error(exc)
    except GitCommandNotFound as exc:
        return False, f"Git 未安装或不在 PATH 中: {exc}"
    except Exception as exc:
        return False, str(exc)


def ls_remote(repo_url: str, ref: str = "HEAD", *, timeout_sec: int = 30) -> tuple[bool, str]:
    ok, out = execute_git(
        ["ls-remote", repo_url, ref],
        timeout=timeout_sec,
    )
    if not ok:
        return False, out
    first = (out or "").strip().splitlines()[0] if out else ""
    return True, first or "(empty)"


def clone_from(
    url: str,
    path: Path,
    *,
    safe_dir: Path | None = None,
    timeout: float = 300,
) -> tuple[bool, str]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    opts = git_multi_options(safe_dir or path)
    env = _run_env()
    clone_kwargs: dict = {
        "url": url,
        "to_path": str(path),
        "env": env,
        "multi_options": opts,
    }
    if timeout > 0 and not _on_windows():
        clone_kwargs["kill_after_timeout"] = int(timeout)
    try:
        Repo.clone_from(**clone_kwargs)
        return True, ""
    except GitCommandError as exc:
        return False, _format_git_error(exc)
    except GitCommandNotFound as exc:
        return False, f"Git 未安装或不在 PATH 中: {exc}"
    except Exception as exc:
        return False, str(exc)


def open_repo(pkg_dir: Path) -> Repo | None:
    try:
        if not (pkg_dir / ".git").is_dir():
            return None
        return Repo(str(pkg_dir))
    except (InvalidGitRepositoryError, Exception):
        return None


def init_repo(pkg_dir: Path, branch: str = "main") -> tuple[bool, str]:
    try:
        Repo.init(str(pkg_dir), initial_branch=branch)
        repo = open_repo(pkg_dir)
        if repo is not None:
            with repo.config_writer() as cw:
                cw.set_value("core", "longpaths", True)
        return True, ""
    except Exception as exc:
        return False, str(exc)


# ---- 全局 git config（~/.gitconfig）----


def global_config_get(key: str) -> str | None:
    """读取全局 git config；key 形如 http.proxy、https.proxy、http.version。

    避免直接创建 GitConfigParser。部分 GitPython 版本在 Python 3.12 下，
    GitConfigParser 初始化失败后析构会触发 `_read_only` AttributeError。
    """
    ok, out = execute_git(["config", "--global", "--get", key], timeout=30)
    if not ok:
        return None
    val = (out or "").strip()
    return val or None


def global_config_set(key: str, value: str) -> None:
    """写入全局 git config，使用 git config 命令规避 GitConfigParser 析构异常。"""
    if "." not in (key or ""):
        return
    execute_git(["config", "--global", key, value or ""], timeout=30)


def global_config_unset(key: str) -> None:
    """删除全局 git config；不存在时忽略。"""
    if "." not in (key or ""):
        return
    execute_git(["config", "--global", "--unset-all", key], timeout=30)


def apply_global_proxy(http_proxy: str | None, https_proxy: str | None) -> None:
    for key in ("http.proxy", "https.proxy"):
        global_config_unset(key)
    if http_proxy:
        global_config_set("http.proxy", http_proxy)
    if https_proxy:
        global_config_set("https.proxy", https_proxy or http_proxy)


def apply_global_http_version(version: str) -> None:
    if version:
        global_config_set("http.version", version)
    else:
        global_config_unset("http.version")


def commit(pkg_dir: Path, message: str) -> tuple[bool, str, str]:
    """通过命令行 git commit 提交暂存区变更，返回 (成功, 错误信息, commit_hash)。

    使用命令行而非 Repo.index.commit()，确保日志可见且能拿到 commit hash 验证。
    """
    msg = (message or "").strip() or "sync"
    ok, out = execute_git(
        ["commit", "-m", msg],
        cwd=pkg_dir,
        repo_dir=pkg_dir,
    )
    if not ok:
        low = (out or "").lower()
        if "nothing to commit" in low or "no changes added to commit" in low:
            return True, "", ""
        return False, out, ""
    # 获取刚创建的 commit hash
    ok_hash, hash_out = execute_git(
        ["rev-parse", "--short", "HEAD"],
        cwd=pkg_dir,
        repo_dir=pkg_dir,
    )
    commit_hash = (hash_out or "").strip() if ok_hash else ""
    return True, "", commit_hash


def push(
    pkg_dir: Path,
    branch: str,
    *,
    remote: str = "origin",
    set_upstream: bool = True,
    force_with_lease: bool = False,
) -> tuple[bool, str]:
    """推送分支到远端。"""
    repo = open_repo(pkg_dir)
    if repo is None:
        return False, "非 git 仓库"
    try:
        remote_obj = repo.remotes[remote]
        refspec = branch
        kwargs: dict = {}
        if force_with_lease:
            kwargs["force_with_lease"] = True
        if set_upstream:
            kwargs["set_upstream"] = True
        remote_obj.push(refspec=refspec, **kwargs)
        return True, ""
    except GitCommandError as exc:
        return False, _format_git_error(exc)
    except Exception as exc:
        return False, str(exc)


def cat_file_batch_check(pkg_dir: Path, stdin_text: str) -> tuple[bool, str]:
    """cat-file --batch-check，通过 stdin 传入 object 列表（兼容 GitPython input= 在 Windows 上的问题）。"""
    if not (stdin_text or "").strip():
        return True, ""
    git_cmd = ["git", *git_multi_options(pkg_dir), "cat-file", "--batch-check=%(objecttype) %(objectname) %(objectsize) %(rest)"]
    try:
        proc = subprocess.run(
            git_cmd,
            cwd=str(pkg_dir),
            input=stdin_text.encode("utf-8"),
            capture_output=True,
            env=_run_env(),
            timeout=120,
            check=False,
        )
        out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            merged = "\n".join(x for x in [out, err] if x)
            return False, merged or f"exit code={proc.returncode}"
        return True, out
    except Exception as exc:
        return False, str(exc)
