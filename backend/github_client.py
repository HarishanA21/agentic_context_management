"""GitHub integration helpers.

The user stores a Personal Access Token (PAT) once; we look it up by user_id
on each operation. RLS on `github_credentials` enforces user isolation; this
module trusts that the caller already authenticated the user_id via JWT.
"""

from __future__ import annotations

from typing import Optional

from github import Auth, Github
from github.GithubException import BadCredentialsException, GithubException


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
