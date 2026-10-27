#!/usr/bin/env python3
"""Manage the MCP server's per-client token file (ADR-0022).

The file holds ``<name> <scopes> <sha256>`` lines, never a token. It is
``root:root 0600``: the service does not read it directly, systemd reads it as
root (``LoadCredential=``) and hands the service a private copy. Run as root on
the Pi:

    sudo /opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/mcp_token.py add alice \\
        --scopes read --sha256 <64 hex>

Scopes: ``read``, ``watchlist`` (add/remove watchlist entries) and ``location``
(opt-in: absolute coordinates in three tools); the last two imply ``read``.

``add`` registers a hash of a token generated on the client host, so the
secret never reaches the receiver. ``new`` generates a token and prints it
once, only to a terminal. The server loads the credential copy at startup:
restart it after every change.

``--file`` defaults to the literal ``/etc/readsbstats/mcp-tokens`` (the source
of the credential), not ``RSBS_MCP_TOKENS_FILE``, which inside the service
points at the credential copy.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys

from readsbstats.mcp_server import auth

FILE_MODE = 0o600
DEFAULT_FILE = "/etc/readsbstats/mcp-tokens"
RESTART_HINT = "Restart the server to apply: sudo systemctl restart readsbstats-mcp"
_HEADER = "# readsbstats-mcp tokens: <name> <scopes> <sha256>. Hashes only; edit with scripts/mcp_token.py\n"


class CliError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def _render(tokens: dict[str, auth.Client]) -> str:
    lines = [_HEADER]
    for digest, c in tokens.items():
        lines.append(f"{c.name} {','.join(sorted(c.scopes))} {digest}\n")
    return "".join(lines)


def _write_fd(fd: int, text: str) -> None:
    os.fchmod(fd, FILE_MODE)
    if os.geteuid() == 0:
        os.fchown(fd, 0, 0)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _save(path: str, tokens: dict[str, auth.Client]) -> None:
    text = _render(tokens)
    if not os.path.exists(path):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        except FileExistsError:
            raise CliError(f"{path} appeared concurrently; retry") from None
        _write_fd(fd, text)
        return
    tmp = os.path.join(os.path.dirname(os.path.abspath(path)),
                       f".{os.path.basename(path)}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        _write_fd(fd, text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _load(path: str) -> dict[str, auth.Client]:
    try:
        return auth.load_tokens(path)
    except ValueError as exc:
        raise CliError(f"{path}: {exc}") from None


def _register(path: str, name: str, scopes_spec: str, digest: str) -> None:
    if not auth.NAME_RE.match(name):
        raise CliError("invalid name; use a-z, 0-9 and '-', 1 to 32 characters", 2)
    try:
        scopes = auth.normalize_scopes(scopes_spec)
    except ValueError as exc:
        raise CliError(str(exc), 2) from None
    digest = digest.strip().lower()
    if not auth.HASH_RE.match(digest):
        raise CliError("invalid sha256; need 64 hex characters", 2)
    tokens = _load(path)
    if any(c.name == name for c in tokens.values()):
        raise CliError(f"'{name}' already exists; revoke it first")
    if digest in tokens:
        raise CliError(f"this hash is already registered (as '{tokens[digest].name}')")
    tokens[digest] = auth.Client(name, scopes)
    _save(path, tokens)


def cmd_add(ns) -> int:
    _register(ns.file, ns.name, ns.scopes, ns.sha256)
    print(f"Added '{ns.name}'.")
    print(RESTART_HINT)
    return 0


def cmd_new(ns) -> int:
    if not sys.stdout.isatty():
        raise CliError("refusing to print a token to a non-terminal (it would land in a "
                       "log or transcript); run this in an interactive shell, or generate "
                       "the token on the client and use 'add --sha256'", 2)
    token = "rsbs_mcp_" + secrets.token_urlsafe(32)
    _register(ns.file, ns.name, ns.scopes, auth.hash_token(token))
    print(f"Added '{ns.name}'. The token is shown once; store it now:")
    print(token)
    print(RESTART_HINT)
    return 0


def cmd_list(ns) -> int:
    for c in _load(ns.file).values():
        print(f"{c.name}\t{','.join(sorted(c.scopes))}")
    return 0


def cmd_revoke(ns) -> int:
    tokens = _load(ns.file)
    keep = {h: c for h, c in tokens.items() if c.name != ns.name}
    if len(keep) == len(tokens):
        raise CliError(f"no client named '{ns.name}'")
    _save(ns.file, keep)
    print(f"Revoked '{ns.name}'.")
    print(RESTART_HINT)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Manage readsbstats MCP client tokens.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_cmd(name, fn, help_, scopes=False, sha=False, with_name=True):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--file", default=DEFAULT_FILE,
                        help=f"tokens file (default: {DEFAULT_FILE}, the source of the service's "
                             "credential; not RSBS_MCP_TOKENS_FILE)")
        if with_name:
            sp.add_argument("name")
        if scopes:
            sp.add_argument("--scopes", required=True, help="read[,watchlist][,location]")
        if sha:
            sp.add_argument("--sha256", required=True, help="hex sha256 of the client's token")
        sp.set_defaults(func=fn)

    add_cmd("add", cmd_add, "register a client by token hash", scopes=True, sha=True)
    add_cmd("new", cmd_new, "generate a token, register it, print it once", scopes=True)
    add_cmd("list", cmd_list, "list clients and scopes", with_name=False)
    add_cmd("revoke", cmd_revoke, "remove a client")
    return p


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    try:
        return ns.func(ns)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
