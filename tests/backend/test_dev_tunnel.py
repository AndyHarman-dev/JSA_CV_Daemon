"""Tests for Phase DEV-1: --dev-tunnel flag.

Covers:
  - create_app CORS configuration (default vs dev_tunnel=True)
  - _start_tunnel helper: cloudflared not found, process spawned, URL printed
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import typer

from fastapi.middleware.cors import CORSMiddleware as _CORSMw

from jsa.config import Settings
from jsa.server import create_app
from jsa.cli import _start_tunnel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_cors(app) -> dict | None:
    """Return the kwargs dict of the CORSMiddleware entry in user_middleware,
    or None if not found."""
    for m in app.user_middleware:
        if m.cls is _CORSMw:
            return m.kwargs
    return None


def _make_settings() -> Settings:
    """Build a Settings object using only default values (all fields have defaults)."""
    return Settings()


# ---------------------------------------------------------------------------
# CORS tests — inspect the middleware stack without starting a server
# ---------------------------------------------------------------------------

class TestCorsDefault:
    """Default (dev_tunnel=False): CORS allows only localhost:* via regex."""

    def test_cors_middleware_present(self):
        settings = _make_settings()
        app = create_app(settings, dev_tunnel=False)
        cors = _find_cors(app)
        assert cors is not None, "CORSMiddleware not found in user_middleware"

    def test_cors_default_uses_localhost_regex(self):
        settings = _make_settings()
        app = create_app(settings, dev_tunnel=False)
        cors = _find_cors(app)
        assert "allow_origin_regex" in cors
        assert cors["allow_origin_regex"] == r"http://localhost:\d+"

    def test_cors_default_does_not_allow_all_origins(self):
        settings = _make_settings()
        app = create_app(settings, dev_tunnel=False)
        cors = _find_cors(app)
        # The wildcard allow_origins should not be present
        assert cors.get("allow_origins") != ["*"]

    def test_cors_default_no_wildcard_in_origins(self):
        settings = _make_settings()
        app = create_app(settings, dev_tunnel=False)
        cors = _find_cors(app)
        # allow_origins may be absent or empty — not ["*"]
        origins = cors.get("allow_origins", [])
        assert "*" not in origins


class TestCorsDevTunnel:
    """dev_tunnel=True: CORS uses allow_origins=["*"] for public cloudflared URL."""

    def test_cors_middleware_present(self):
        settings = _make_settings()
        app = create_app(settings, dev_tunnel=True)
        cors = _find_cors(app)
        assert cors is not None, "CORSMiddleware not found in user_middleware"

    def test_cors_dev_tunnel_allows_all_origins(self):
        settings = _make_settings()
        app = create_app(settings, dev_tunnel=True)
        cors = _find_cors(app)
        assert cors.get("allow_origins") == ["*"]

    def test_cors_dev_tunnel_no_regex_restriction(self):
        settings = _make_settings()
        app = create_app(settings, dev_tunnel=True)
        cors = _find_cors(app)
        # allow_origin_regex should be absent when allow_origins=["*"] is used
        assert "allow_origin_regex" not in cors

    def test_cors_dev_tunnel_wildcard_in_origins(self):
        settings = _make_settings()
        app = create_app(settings, dev_tunnel=True)
        cors = _find_cors(app)
        origins = cors.get("allow_origins", [])
        assert "*" in origins


# ---------------------------------------------------------------------------
# _start_tunnel unit tests — mock shutil.which and subprocess.Popen
# ---------------------------------------------------------------------------

class TestStartTunnelCloudflaredNotFound:
    """When cloudflared is absent, _start_tunnel prints a hint and raises typer.Exit."""

    def test_exits_if_cloudflared_not_found(self):
        with patch("jsa.cli.shutil.which", return_value=None):
            with pytest.raises(typer.Exit):
                _start_tunnel(8765)

    def test_exit_is_nonzero(self):
        with patch("jsa.cli.shutil.which", return_value=None):
            with pytest.raises(typer.Exit) as exc_info:
                _start_tunnel(8765)
            assert exc_info.value.exit_code == 1

    def test_prints_install_hint(self, capsys):
        with patch("jsa.cli.shutil.which", return_value=None):
            with pytest.raises(typer.Exit):
                _start_tunnel(8765)
        captured = capsys.readouterr()
        assert "cloudflared" in captured.out


class TestStartTunnelSpawnsProcess:
    """When cloudflared is found, _start_tunnel spawns the correct subprocess."""

    def _make_mock_proc(self, lines=None):
        mock_proc = MagicMock()
        mock_proc.stdout = iter(lines or [])
        return mock_proc

    def test_spawns_cloudflared_process(self):
        mock_proc = self._make_mock_proc()
        with patch("jsa.cli.shutil.which", return_value="/usr/local/bin/cloudflared"):
            with patch("jsa.cli.subprocess.Popen", return_value=mock_proc) as mock_popen:
                _start_tunnel(8765)
                mock_popen.assert_called_once()

    def test_spawned_command_includes_cloudflared(self):
        mock_proc = self._make_mock_proc()
        with patch("jsa.cli.shutil.which", return_value="/usr/local/bin/cloudflared"):
            with patch("jsa.cli.subprocess.Popen", return_value=mock_proc) as mock_popen:
                _start_tunnel(8765)
                args = mock_popen.call_args[0][0]
                assert "cloudflared" in args[0]

    def test_spawned_command_includes_correct_url(self):
        mock_proc = self._make_mock_proc()
        with patch("jsa.cli.shutil.which", return_value="/usr/local/bin/cloudflared"):
            with patch("jsa.cli.subprocess.Popen", return_value=mock_proc) as mock_popen:
                _start_tunnel(8765)
                args = mock_popen.call_args[0][0]
                assert "http://localhost:8765" in args

    def test_spawned_command_includes_tunnel_subcommand(self):
        mock_proc = self._make_mock_proc()
        with patch("jsa.cli.shutil.which", return_value="/usr/local/bin/cloudflared"):
            with patch("jsa.cli.subprocess.Popen", return_value=mock_proc) as mock_popen:
                _start_tunnel(8765)
                args = mock_popen.call_args[0][0]
                assert "tunnel" in args

    def test_respects_custom_port(self):
        mock_proc = self._make_mock_proc()
        with patch("jsa.cli.shutil.which", return_value="/usr/local/bin/cloudflared"):
            with patch("jsa.cli.subprocess.Popen", return_value=mock_proc) as mock_popen:
                _start_tunnel(9999)
                args = mock_popen.call_args[0][0]
                assert "http://localhost:9999" in args


class TestStartTunnelUrlPrinted:
    """The daemon thread scans stdout and prints the trycloudflare URL."""

    def test_url_printed_when_found_in_output(self, capsys):
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            "INFO connecting...\n",
            "INFO | https://xyz-123.trycloudflare.com |\n",
            "INFO tunnel active\n",
        ])
        with patch("jsa.cli.shutil.which", return_value="/usr/local/bin/cloudflared"):
            with patch("jsa.cli.subprocess.Popen", return_value=mock_proc):
                _start_tunnel(8765)
                # Give the daemon thread time to consume the mock iterator
                time.sleep(0.5)
        captured = capsys.readouterr()
        assert "xyz-123.trycloudflare.com" in captured.out

    def test_different_url_printed_correctly(self, capsys):
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            "INFO | https://abc-def-ghi.trycloudflare.com |\n",
        ])
        with patch("jsa.cli.shutil.which", return_value="/usr/local/bin/cloudflared"):
            with patch("jsa.cli.subprocess.Popen", return_value=mock_proc):
                _start_tunnel(8765)
                time.sleep(0.5)
        captured = capsys.readouterr()
        assert "abc-def-ghi.trycloudflare.com" in captured.out

    def test_url_not_printed_when_absent_from_output(self, capsys):
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            "INFO connecting...\n",
            "INFO establishing connection...\n",
        ])
        with patch("jsa.cli.shutil.which", return_value="/usr/local/bin/cloudflared"):
            with patch("jsa.cli.subprocess.Popen", return_value=mock_proc):
                _start_tunnel(8765)
                time.sleep(0.5)
        captured = capsys.readouterr()
        assert "trycloudflare.com" not in captured.out

    def test_daemon_thread_is_started(self):
        """_start_tunnel launches a daemon thread to watch cloudflared output."""
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        before = set(threading.enumerate())
        with patch("jsa.cli.shutil.which", return_value="/usr/local/bin/cloudflared"):
            with patch("jsa.cli.subprocess.Popen", return_value=mock_proc):
                _start_tunnel(8765)
        new = set(threading.enumerate()) - before
        assert new, "Expected _start_tunnel to start a new thread"
        assert any(t.daemon for t in new), "Expected the watcher thread to be a daemon"
