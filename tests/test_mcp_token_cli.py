"""Tests for scripts/mcp_token.py."""

import hashlib
import os
import stat

import pytest

import mcp_token
from readsbstats.mcp_server import auth

H1 = hashlib.sha256(b"one").hexdigest()
H2 = hashlib.sha256(b"two").hexdigest()


@pytest.fixture(autouse=True)
def setup():
    yield


@pytest.fixture
def tf(tmp_path):
    return str(tmp_path / "mcp-tokens")


def run(*args):
    return mcp_token.main(list(args))


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class TestAdd:
    def test_creates_file_0640(self, tf, capsys):
        assert run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf) == 0
        assert mode(tf) == 0o640
        toks = auth.load_tokens(tf)
        assert toks[H1] == auth.Client("alice", frozenset({"read"}))
        assert "Restart the server to apply: sudo systemctl restart readsbstats-mcp" in capsys.readouterr().out

    def test_second_add_rewrites_keeps_mode_and_entries(self, tf):
        run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf)
        assert run("add", "bob", "--scopes", "read,watchlist", "--sha256", H2.upper(), "--file", tf) == 0
        assert mode(tf) == 0o640
        toks = auth.load_tokens(tf)
        assert toks[H2].name == "bob" and toks[H2].scopes == frozenset({"read", "watchlist"})
        assert toks[H1].name == "alice"
        assert not [p for p in os.listdir(os.path.dirname(tf)) if p != "mcp-tokens"]   # no temp left

    def test_watchlist_scope_alone(self, tf):
        run("add", "bob", "--scopes", "watchlist", "--sha256", H1, "--file", tf)
        assert auth.load_tokens(tf)[H1].scopes == frozenset({"read", "watchlist"})

    def test_duplicate_name(self, tf, capsys):
        run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf)
        assert run("add", "alice", "--scopes", "read", "--sha256", H2, "--file", tf) == 1
        assert "already exists; revoke it first" in capsys.readouterr().err
        assert list(auth.load_tokens(tf)) == [H1]

    def test_duplicate_hash(self, tf, capsys):
        run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf)
        assert run("add", "bob", "--scopes", "read", "--sha256", H1, "--file", tf) == 1
        assert "hash" in capsys.readouterr().err
        assert len(auth.load_tokens(tf)) == 1

    @pytest.mark.parametrize("name", ["Alice", "a_b", "", "x" * 33, "a b"])
    def test_invalid_name(self, tf, name, capsys):
        assert run("add", name, "--scopes", "read", "--sha256", H1, "--file", tf) == 2
        assert not os.path.exists(tf)

    @pytest.mark.parametrize("h", ["abc", "z" * 64, H1 + "0", ""])
    def test_invalid_hash(self, tf, h):
        assert run("add", "alice", "--scopes", "read", "--sha256", h, "--file", tf) == 2
        assert not os.path.exists(tf)

    @pytest.mark.parametrize("scopes", ["admin", "", "read,x"])
    def test_invalid_scopes(self, tf, scopes):
        assert run("add", "alice", "--scopes", scopes, "--sha256", H1, "--file", tf) == 2
        assert not os.path.exists(tf)

    @pytest.mark.parametrize("m", [0o664, 0o646, 0o660])
    def test_writable_file_refused(self, tf, m, capsys):
        run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf)
        before = open(tf).read()
        os.chmod(tf, m)
        assert run("add", "bob", "--scopes", "read", "--sha256", H2, "--file", tf) == 1
        assert "writable" in capsys.readouterr().err
        assert open(tf).read() == before

    def test_malformed_existing_file_refused(self, tf):
        with open(tf, "w") as f:
            f.write("garbage\n")
        os.chmod(tf, 0o640)
        assert run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf) == 1
        assert open(tf).read() == "garbage\n"

    def test_comments_in_existing_file_are_dropped_but_valid(self, tf):
        with open(tf, "w") as f:
            f.write(f"# mine\nalice read {H1}\n")
        os.chmod(tf, 0o640)
        assert run("add", "bob", "--scopes", "read", "--sha256", H2, "--file", tf) == 0
        assert set(auth.load_tokens(tf)) == {H1, H2}


class TestNew:
    def test_refuses_non_tty(self, tf, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdout.isatty", lambda: False, raising=False)
        assert run("new", "alice", "--scopes", "read", "--file", tf) == 2
        cap = capsys.readouterr()
        assert "terminal" in cap.err and "rsbs_mcp_" not in cap.out + cap.err
        assert not os.path.exists(tf)

    def test_tty_prints_token_once_and_registers_hash(self, tf, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
        assert run("new", "alice", "--scopes", "read,watchlist", "--file", tf) == 0
        out = capsys.readouterr().out
        assert out.count("rsbs_mcp_") == 1
        token = next(w for w in out.split() if w.startswith("rsbs_mcp_"))
        assert len(token) > 40
        toks = auth.load_tokens(tf)
        assert toks[auth.hash_token(token)] == auth.Client("alice", frozenset({"read", "watchlist"}))
        assert mode(tf) == 0o640
        assert token not in open(tf).read()

    def test_two_new_tokens_differ(self, tf, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
        run("new", "a", "--scopes", "read", "--file", tf)
        t1 = [w for w in capsys.readouterr().out.split() if w.startswith("rsbs_mcp_")][0]
        run("new", "b", "--scopes", "read", "--file", tf)
        t2 = [w for w in capsys.readouterr().out.split() if w.startswith("rsbs_mcp_")][0]
        assert t1 != t2 and len(auth.load_tokens(tf)) == 2

    def test_duplicate_name_does_not_print_token(self, tf, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
        run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf)
        capsys.readouterr()
        assert run("new", "alice", "--scopes", "read", "--file", tf) == 1
        assert "rsbs_mcp_" not in capsys.readouterr().out


class TestListRevoke:
    def test_list_never_shows_hashes(self, tf, capsys):
        run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf)
        run("add", "bob", "--scopes", "watchlist", "--sha256", H2, "--file", tf)
        capsys.readouterr()
        assert run("list", "--file", tf) == 0
        out = capsys.readouterr().out
        assert "alice" in out and "bob" in out and "watchlist" in out
        assert H1 not in out and H2 not in out and not any(len(w) >= 40 for w in out.split())

    def test_list_empty_or_missing(self, tf, capsys):
        assert run("list", "--file", tf) == 0
        assert not os.path.exists(tf)

    def test_revoke(self, tf, capsys):
        run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf)
        run("add", "bob", "--scopes", "read", "--sha256", H2, "--file", tf)
        capsys.readouterr()
        assert run("revoke", "alice", "--file", tf) == 0
        assert list(auth.load_tokens(tf)) == [H2]
        assert mode(tf) == 0o640
        assert "Restart the server to apply" in capsys.readouterr().out

    def test_revoke_last_leaves_valid_empty_file(self, tf):
        run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf)
        assert run("revoke", "alice", "--file", tf) == 0
        assert auth.load_tokens(tf) == {}

    def test_revoke_unknown(self, tf, capsys):
        run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf)
        assert run("revoke", "nobody", "--file", tf) == 1
        assert "nobody" in capsys.readouterr().err
        assert list(auth.load_tokens(tf)) == [H1]

    def test_revoke_missing_file(self, tf):
        assert run("revoke", "alice", "--file", tf) == 1

    def test_writable_file_refused_for_revoke(self, tf):
        run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf)
        os.chmod(tf, 0o664)
        assert run("revoke", "alice", "--file", tf) == 1


class TestRoot:
    def test_chown_called_when_root(self, tf, monkeypatch):
        calls = []
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(mcp_token.grp, "getgrnam", lambda n: type("G", (), {"gr_gid": 4242})())
        monkeypatch.setattr(os, "fchown", lambda fd, u, g: calls.append((u, g)))
        assert run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf) == 0
        run("add", "bob", "--scopes", "read", "--sha256", H2, "--file", tf)
        assert calls == [(0, 4242), (0, 4242)]

    def test_missing_group_exits_1(self, tf, monkeypatch, capsys):
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        def boom(n):
            raise KeyError(n)
        monkeypatch.setattr(mcp_token.grp, "getgrnam", boom)
        assert run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf) == 1
        assert "readsbstats" in capsys.readouterr().err
        assert not os.path.exists(tf)

    def test_no_chown_when_not_root(self, tf, monkeypatch):
        monkeypatch.setattr(os, "fchown", lambda *a: pytest.fail("chown as non-root"))
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        assert run("add", "alice", "--scopes", "read", "--sha256", H1, "--file", tf) == 0


def test_default_file_from_config(monkeypatch):
    from readsbstats import config
    monkeypatch.setattr(config, "MCP_TOKENS_FILE", "/x/y")
    ns = mcp_token.build_parser().parse_args(["list"])
    assert ns.file == "/x/y"
