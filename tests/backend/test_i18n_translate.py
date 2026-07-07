"""jsa/i18n/translate.py — API vs CLI backend dispatch for the UI translation generator."""

from __future__ import annotations

import json
import subprocess

import pytest

from jsa.i18n import translate


class TestTranslateBatchDispatch:
    def test_defaults_to_api_backend(self, monkeypatch):
        calls = []
        monkeypatch.setattr(translate, "_translate_batch_api", lambda p, c: calls.append(("api", p, c)) or {})
        monkeypatch.setattr(translate, "_translate_batch_cli", lambda p, c: calls.append(("cli", p, c)) or {})

        translate._translate_batch({"k": "v"}, "es", "api")

        assert calls == [("api", {"k": "v"}, "es")]

    def test_cli_backend_routes_to_cli_helper(self, monkeypatch):
        calls = []
        monkeypatch.setattr(translate, "_translate_batch_api", lambda p, c: calls.append(("api", p, c)) or {})
        monkeypatch.setattr(translate, "_translate_batch_cli", lambda p, c: calls.append(("cli", p, c)) or {})

        translate._translate_batch({"k": "v"}, "fr", "cli")

        assert calls == [("cli", {"k": "v"}, "fr")]


class TestTranslateBatchCli:
    def test_builds_expected_command_and_parses_json_stdout(self, monkeypatch):
        captured = {}

        def fake_run(cmd, capture_output, text, timeout):
            captured["cmd"] = cmd
            captured["timeout"] = timeout
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"greeting": "Hola"}), stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = translate._translate_batch_cli({"greeting": "Hello"}, "es")

        assert result == {"greeting": "Hola"}
        cmd = captured["cmd"]
        assert cmd[0] == "claude"
        assert "--output-format" in cmd and "text" in cmd
        assert cmd[-2] == "-p"
        assert "Spanish" in cmd[-1] or "es" in cmd[-1]

    def test_strips_markdown_fences_from_output(self, monkeypatch):
        def fake_run(cmd, capture_output, text, timeout):
            return subprocess.CompletedProcess(
                cmd, 0, stdout='```json\n{"a": "b"}\n```', stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = translate._translate_batch_cli({"a": "x"}, "de")

        assert result == {"a": "b"}

    def test_raises_runtime_error_on_nonzero_exit(self, monkeypatch):
        def fake_run(cmd, capture_output, text, timeout):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not logged in")

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="not logged in"):
            translate._translate_batch_cli({"a": "x"}, "de")


class TestRunThreadsBackend:
    def test_run_passes_backend_choice_to_translate_batch(self, tmp_path, monkeypatch):
        i18n_dir = tmp_path / "i18n"
        i18n_dir.mkdir()
        source_path = i18n_dir / "strings.en.json"
        source_path.write_text(json.dumps({"greeting": "Hello"}), encoding="utf-8")

        monkeypatch.setattr(translate, "_I18N_DIR", i18n_dir)
        monkeypatch.setattr(translate, "_SOURCE_PATH", source_path)
        monkeypatch.setattr(translate, "_META_PATH", i18n_dir / "strings.meta.json")
        monkeypatch.setattr(translate, "LANGUAGES", [("en", "English", "English"), ("es", "Spanish", "Español")])

        seen_backends = []

        def fake_translate_batch(pending, code, backend):
            seen_backends.append(backend)
            return {k: v.upper() for k, v in pending.items()}

        monkeypatch.setattr(translate, "_translate_batch", fake_translate_batch)

        rc = translate.run(backend="cli")

        assert rc == 0
        assert seen_backends == ["cli"]

    def _setup(self, tmp_path, monkeypatch, languages):
        i18n_dir = tmp_path / "i18n"
        i18n_dir.mkdir()
        source_path = i18n_dir / "strings.en.json"
        source_path.write_text(json.dumps({"greeting": "Hello"}), encoding="utf-8")

        monkeypatch.setattr(translate, "_I18N_DIR", i18n_dir)
        monkeypatch.setattr(translate, "_SOURCE_PATH", source_path)
        monkeypatch.setattr(translate, "_META_PATH", i18n_dir / "strings.meta.json")
        monkeypatch.setattr(translate, "LANGUAGES", languages)
        return i18n_dir

    def test_translates_multiple_locales_concurrently(self, tmp_path, monkeypatch):
        import threading
        import time

        languages = [
            ("en", "English", "English"),
            ("es", "Spanish", "Español"),
            ("fr", "French", "Français"),
        ]
        self._setup(tmp_path, monkeypatch, languages)

        barrier = threading.Barrier(2, timeout=5)

        def fake_translate_batch(pending, code, backend):
            barrier.wait()  # only passes if both locales are in-flight at once
            return {k: v.upper() for k, v in pending.items()}

        monkeypatch.setattr(translate, "_translate_batch", fake_translate_batch)

        rc = translate.run(backend="cli")

        assert rc == 0

    def test_one_locale_failing_does_not_abort_the_others(self, tmp_path, monkeypatch):
        languages = [
            ("en", "English", "English"),
            ("es", "Spanish", "Español"),
            ("fr", "French", "Français"),
        ]
        i18n_dir = self._setup(tmp_path, monkeypatch, languages)

        def fake_translate_batch(pending, code, backend):
            if code == "es":
                raise RuntimeError("boom")
            return {k: v.upper() for k, v in pending.items()}

        monkeypatch.setattr(translate, "_translate_batch", fake_translate_batch)

        rc = translate.run(backend="cli")

        assert rc == 1  # a failure occurred...
        fr_catalog = json.loads((i18n_dir / "strings.fr.json").read_text())
        assert fr_catalog == {"greeting": "HELLO"}  # ...but fr still got written
        assert not (i18n_dir / "strings.es.json").exists()

    def test_check_mode_reports_all_pending_locales_without_translating(self, tmp_path, monkeypatch):
        languages = [
            ("en", "English", "English"),
            ("es", "Spanish", "Español"),
            ("fr", "French", "Français"),
        ]
        self._setup(tmp_path, monkeypatch, languages)

        def fake_translate_batch(pending, code, backend):
            raise AssertionError("--check must not call the translator")

        monkeypatch.setattr(translate, "_translate_batch", fake_translate_batch)

        rc = translate.run(check=True)

        assert rc == 1
