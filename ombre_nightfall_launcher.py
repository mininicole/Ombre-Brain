"""Run Ombre with Night-Fall while keeping local Uvicorn security settings."""

from __future__ import annotations

import os

import uvicorn
from night_fall.launcher import (
    import_ombre_server,
    load_config,
    register_night_fall,
    run_ombre_server,
)


def main() -> None:
    cfg = load_config(require_ombre=True)
    ombre_server = import_ombre_server(cfg.ombre_home)
    register_night_fall(ombre_server, cfg)

    upstream_uvicorn_run = uvicorn.run

    def run_uvicorn(app, host="0.0.0.0", port=8000, **kwargs):
        del host
        kwargs.pop("access_log", None)
        return upstream_uvicorn_run(
            app,
            host=os.environ.get("OMBRE_HOST", "0.0.0.0"),
            port=port,
            access_log=not bool(os.environ.get("GALE_MCP_SLUG", "").strip()),
            **kwargs,
        )

    uvicorn.run = run_uvicorn
    try:
        run_ombre_server(ombre_server)
    finally:
        uvicorn.run = upstream_uvicorn_run


if __name__ == "__main__":
    main()
