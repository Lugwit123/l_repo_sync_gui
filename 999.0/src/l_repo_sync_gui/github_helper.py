# -*- coding: utf-8 -*-
"""GitHub REST API 封装：替代 gh.exe CLI。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

API_ROOT = "https://api.github.com"
USER_AGENT = "l_repo_sync_gui"
TOKEN_CREATE_URL = "https://github.com/settings/tokens/new?scopes=repo,delete_repo,read:org&description=l_repo_sync_gui"


class GitHubApiError(Exception):
    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def _headers(token: str | None, *, accept: str = "application/vnd.github+json") -> dict[str, str]:
    hdrs = {
        "Accept": accept,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    return hdrs


def _read_response(resp: urllib.response.addinfourl) -> Any:
    raw = resp.read().decode("utf-8", errors="replace")
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def api_request(
    method: str,
    path: str,
    token: str | None,
    *,
    body: dict | None = None,
    timeout: float = 30,
) -> Any:
    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    data = None
    hdrs = _headers(token)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _read_response(resp)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        message = err_body
        try:
            payload = json.loads(err_body)
            if isinstance(payload, dict):
                message = payload.get("message") or err_body
        except Exception:
            pass
        raise GitHubApiError(message, status=exc.code, body=err_body) from exc
    except urllib.error.URLError as exc:
        raise GitHubApiError(str(exc.reason or exc)) from exc


def _paginate(path: str, token: str | None, *, timeout: float = 30) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        data = api_request(
            "GET",
            f"{path}{sep}per_page=100&page={page}",
            token,
            timeout=timeout,
        )
        if not isinstance(data, list):
            break
        if not data:
            break
        items.extend(data)
        if len(data) < 100:
            break
        page += 1
    return items


def check_auth(token: str) -> tuple[bool, str]:
    """验证 Token，返回 (ok, 描述信息)。"""
    token = (token or "").strip()
    if not token:
        return False, "未配置 GitHub Token。\n请在界面填写 Token 并保存。"
    try:
        user = api_request("GET", "/user", token)
        if not isinstance(user, dict):
            return False, "GitHub API 返回异常"
        login = str(user.get("login") or "").strip()
        name = str(user.get("name") or login).strip()
        return True, f"已授权：{name} (@{login})"
    except GitHubApiError as exc:
        if exc.status == 401:
            return False, "Token 无效或已过期，请重新生成并保存。"
        return False, str(exc)


def list_repo_names(owner: str, token: str) -> tuple[bool, list[str], str]:
    """列出 owner（用户或组织）下的仓库名。"""
    owner = (owner or "").strip()
    token = (token or "").strip()
    if not owner:
        return False, [], "owner 为空"
    if not token:
        return False, [], "未配置 GitHub Token"

    names: list[str] = []
    seen: set[str] = set()

    def _add(items: list[dict]):
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)

    try:
        org_items = _paginate(f"/orgs/{owner}/repos", token)
        if org_items:
            _add(org_items)
            return True, sorted(names, key=str.lower), ""

        user_items = _paginate(
            "/user/repos?affiliation=owner,organization_member&visibility=all",
            token,
        )
        owner_lower = owner.lower()
        filtered = [
            item
            for item in user_items
            if str((item.get("owner") or {}).get("login") or "").lower() == owner_lower
        ]
        if filtered:
            _add(filtered)
            return True, sorted(names, key=str.lower), ""

        public_items = _paginate(f"/users/{owner}/repos?type=all", token)
        _add(public_items)
        return True, sorted(names, key=str.lower), ""
    except GitHubApiError as exc:
        if exc.status == 404:
            return False, [], f"GitHub 用户/组织不存在: {owner}"
        return False, [], str(exc)
    except Exception as exc:
        return False, [], str(exc)


def create_repo(
    owner: str,
    repo_name: str,
    token: str,
    *,
    public: bool = True,
) -> tuple[bool, str]:
    owner = (owner or "").strip()
    repo_name = (repo_name or "").strip()
    token = (token or "").strip()
    if not token:
        return False, "未配置 GitHub Token"
    if not owner or not repo_name:
        return False, "owner 或仓库名为空"

    payload = {"name": repo_name, "private": not public, "auto_init": False}
    try:
        me = api_request("GET", "/user", token)
        login = str((me or {}).get("login") or "").strip()
        if login.lower() == owner.lower():
            api_request("POST", "/user/repos", token, body=payload)
        else:
            api_request("POST", f"/orgs/{owner}/repos", token, body=payload)
        return True, ""
    except GitHubApiError as exc:
        low = str(exc).lower()
        if exc.status == 422 and ("already exists" in low or "name already exists" in low):
            return False, "repository already exists"
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def delete_repo(owner: str, repo_name: str, token: str) -> tuple[bool, str]:
    owner = (owner or "").strip()
    repo_name = (repo_name or "").strip()
    token = (token or "").strip()
    if not token:
        return False, "未配置 GitHub Token"
    try:
        api_request("DELETE", f"/repos/{owner}/{repo_name}", token)
        return True, ""
    except GitHubApiError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)
