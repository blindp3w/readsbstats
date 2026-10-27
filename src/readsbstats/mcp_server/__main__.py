"""Entry point: ``python -m readsbstats.mcp_server`` (systemd unit ``readsbstats-mcp``)."""

import logging
import sys
from urllib.parse import urlparse

import uvicorn

from .. import config, http_safe
from . import auth
from .server import build_app

log = logging.getLogger("readsbstats.mcp_server")

_QUIET_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2", "mcp", "uvicorn.access")


def api_url_error(url: str) -> str | None:
    """None if *url* is an acceptable base URL for the local API, else the reason."""
    if not http_safe.is_loopback_http_url(url):
        return "must be an http:// URL on a loopback host (127.0.0.1, localhost or [::1])"
    parsed = urlparse(url)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.params:
        return "must not contain a path, query or fragment (the API paths are fixed: /api/...)"
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    problem = api_url_error(config.MCP_API_URL)
    if problem:
        log.error("RSBS_MCP_API_URL %s", problem)
        sys.exit(2)

    try:
        tokens = auth.load_tokens(config.MCP_TOKENS_FILE)
    except ValueError as exc:
        log.error("cannot load %s: %s", config.MCP_TOKENS_FILE, exc)
        sys.exit(1)
    except OSError as exc:
        log.error("cannot read %s: %s", config.MCP_TOKENS_FILE, exc)
        sys.exit(1)
    if not tokens:
        log.error("no MCP tokens configured; every request will be rejected "
                  "(see scripts/mcp_token.py)")

    names = ", ".join(sorted(c.name for c in tokens.values())) or "none"
    log.info("readsbstats-mcp listening on %s:%s; clients: %s",
             config.MCP_HOST, config.MCP_PORT, names)
    uvicorn.run(build_app(tokens), host=config.MCP_HOST, port=config.MCP_PORT,
                log_level="warning", server_header=False, proxy_headers=False)


if __name__ == "__main__":
    main()
