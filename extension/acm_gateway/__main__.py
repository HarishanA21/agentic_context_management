"""Entry point: ``acm-gateway`` (or ``python -m acm_gateway``)."""

from __future__ import annotations

import uvicorn

from .config import Settings


def main() -> None:
    s = Settings.from_env()
    print(
        f"[acm-gateway] http://{s.host}:{s.port}  ->  {s.upstream_base_url}\n"
        f"[acm-gateway] profile: {s.config_path}",
        flush=True,
    )
    uvicorn.run("acm_gateway.app:app", host=s.host, port=s.port, reload=False)


if __name__ == "__main__":
    main()
