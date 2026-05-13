"""GitHub integration helpers.

The user stores a Personal Access Token (PAT) once; we look it up by user_id
on each operation. RLS on `github_credentials` enforces user isolation; this
module trusts that the caller already authenticated the user_id via JWT.
"""

from __future__ import annotations

import re
from typing import Optional

import requests
from github import Auth, Github
from github.GithubException import BadCredentialsException, GithubException


# Letters, digits, dot, hyphen, underscore. Max 100 chars (GitHub's limit).
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def get_token(conn, user_id: str) -> Optional[str]:
    """Return the saved PAT for this user, or None if none stored."""
    row = conn.execute(
        "SELECT token FROM github_credentials WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    return row[0] if row else None


def get_username(conn, user_id: str) -> Optional[str]:
    """Return the saved GitHub username for this user, or None."""
    row = conn.execute(
        "SELECT github_username FROM github_credentials WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    return row[0] if row else None


def save_token(conn, user_id: str, token: str) -> str:
    """Verify the token works, then upsert it. Returns the GitHub username.

    Raises ValueError on any invalid token / API error so the caller can
    surface a 400/401 to the user.
    """
    try:
        gh = Github(auth=Auth.Token(token))
        login = gh.get_user().login
    except BadCredentialsException:
        raise ValueError("Invalid GitHub token (bad credentials).")
    except GithubException as e:
        raise ValueError(f"GitHub rejected the token: {e.data.get('message', e)}")
    except Exception as e:
        raise ValueError(f"Could not reach GitHub: {e}")

    conn.execute(
        """
        INSERT INTO github_credentials (user_id, token, github_username, updated_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (user_id) DO UPDATE
            SET token = EXCLUDED.token,
                github_username = EXCLUDED.github_username,
                updated_at = now()
        """,
        (user_id, token, login),
    )
    return login


def delete_token(conn, user_id: str) -> None:
    conn.execute(
        "DELETE FROM github_credentials WHERE user_id = %s",
        (user_id,),
    )


def get_session_repo(conn, user_id: str, session_id: str) -> Optional[dict]:
    """Return the repo linked to this session, or None if not linked."""
    row = conn.execute(
        """
        SELECT github_owner, github_repo, github_branch
        FROM sessions
        WHERE id = %s AND user_id = %s
        """,
        (session_id, user_id),
    ).fetchone()
    if not row or not row[0] or not row[1]:
        return None
    return {"owner": row[0], "repo": row[1], "branch": row[2] or "main"}


def link_session_repo(
    conn, user_id: str, session_id: str, owner: str, repo: str, branch: str
) -> None:
    conn.execute(
        """
        UPDATE sessions
        SET github_owner = %s, github_repo = %s, github_branch = %s
        WHERE id = %s AND user_id = %s
        """,
        (owner, repo, branch, session_id, user_id),
    )


def unlink_session_repo(conn, user_id: str, session_id: str) -> None:
    conn.execute(
        """
        UPDATE sessions
        SET github_owner = NULL, github_repo = NULL, github_branch = NULL
        WHERE id = %s AND user_id = %s
        """,
        (session_id, user_id),
    )


def get_client(token: str) -> Github:
    return Github(auth=Auth.Token(token))


def verify_token_scopes(token: str) -> dict:
    """Return a dict describing the token's authentication shape.

    Result keys:
        type:   'classic' | 'fine_grained' | 'unknown'
        scopes: list[str] — OAuth scopes for classic PATs (empty for fine-grained)
        login:  the GitHub username the token authenticates as

    Raises ValueError on a bad/invalid token. We call GitHub's /user endpoint
    directly so we can read the X-OAuth-Scopes response header, which PyGithub
    does not expose.
    """
    try:
        r = requests.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10,
        )
    except requests.RequestException as e:
        raise ValueError(f"Could not reach GitHub: {e}")

    if r.status_code == 401:
        raise ValueError("Invalid GitHub token (401 from GitHub).")
    if r.status_code >= 400:
        raise ValueError(f"GitHub returned {r.status_code}: {r.text[:200]}")

    scopes_header = r.headers.get("X-OAuth-Scopes", "") or ""
    scopes = [s.strip() for s in scopes_header.split(",") if s.strip()]
    login = (r.json() or {}).get("login", "")

    # Fine-grained PATs start with `github_pat_` and don't report classic
    # scopes via X-OAuth-Scopes (their permissions are per-resource).
    if token.startswith("github_pat_"):
        token_type = "fine_grained"
    elif scopes_header == "" and not token.startswith("github_pat_"):
        # Empty header on a classic-looking token is unusual; flag as unknown.
        token_type = "unknown"
    else:
        token_type = "classic"

    return {"type": token_type, "scopes": scopes, "login": login}


def can_create_repos(scope_info: dict) -> tuple[bool, str]:
    """Decide whether a token can create new user-owned repos.

    Returns (allowed, reason). `reason` is empty when allowed, or a
    user-facing message explaining the missing permission.
    """
    if scope_info["type"] == "classic":
        if "repo" in scope_info["scopes"]:
            return True, ""
        return (
            False,
            "Your GitHub token is missing the 'repo' scope, which is required "
            "to create new repositories. Re-paste a classic PAT with 'repo' "
            "(or 'public_repo' for public-only repos) checked.",
        )
    if scope_info["type"] == "fine_grained":
        return (
            False,
            "Fine-grained personal access tokens cannot create new repositories. "
            "Re-paste a classic PAT with the 'repo' scope.",
        )
    return (
        False,
        "Could not determine your token's permissions. Re-paste a classic PAT "
        "with the 'repo' scope.",
    )


def create_repo(token: str, name: str, *, private: bool = True) -> dict:
    """Create a new repo on the authenticated user's account.

    Returns `{owner, repo, default_branch, html_url, clone_url, ssh_url}`.
    Raises ValueError on validation errors and GithubException on API errors.

    Uses auto_init=False so the repo starts empty — the workspace will push
    its own initial commits as the first content, avoiding merge conflicts
    with an auto-generated README.
    """
    name = (name or "").strip()
    if not _REPO_NAME_RE.match(name):
        raise ValueError(
            f"Invalid repo name {name!r}. Use letters, digits, '.', '-', '_'; "
            "max 100 chars."
        )

    try:
        gh = Github(auth=Auth.Token(token))
        user = gh.get_user()
        repo = user.create_repo(
            name=name,
            private=private,
            auto_init=False,
            description="Created by ACM",
        )
    except BadCredentialsException:
        raise ValueError("Invalid GitHub token (bad credentials).")
    except GithubException as e:
        # GitHub's "name already exists" comes back as 422 with a specific
        # message — surface it cleanly.
        data = getattr(e, "data", {}) or {}
        msg = data.get("message", str(e))
        errors = data.get("errors") or []
        for err in errors:
            field_msg = err.get("message") or err.get("code")
            if field_msg:
                msg = f"{msg}: {field_msg}"
                break
        raise ValueError(f"GitHub rejected the request: {msg}")

    return {
        "owner": repo.owner.login,
        "repo": repo.name,
        "default_branch": repo.default_branch or "main",
        "html_url": repo.html_url,
        "clone_url": repo.clone_url,
        "ssh_url": repo.ssh_url,
    }
