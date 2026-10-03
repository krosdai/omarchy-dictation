"""Installer checks with a mocked desktop and local-only fake text providers."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import install  # noqa: E402

KEY_VARIABLES = (
    install.TEXT_KEY_VARIABLE,
    *install.PROVIDER_KEY_VARIABLES.values(),
    *install.RECOGNITION_KEY_VARIABLES,
)


def without_key_variables(test):
    """Keep a developer's own API keys out of tests that expect key files."""
    environment = patch.dict(os.environ)
    environment.start()
    test.addCleanup(environment.stop)
    for name in KEY_VARIABLES:
        os.environ.pop(name, None)


class FakeSystem:
    """Records system and pip commands without executing any of them."""

    def __init__(self):
        self.commands = []

    def run(self, *args, **kwargs):
        self.commands.append(args)
        return ""


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = install.Paths(
            voxtype_config=root / "voxtype/config.toml",
            bindings=root / "hypr/bindings.lua",
            settings=root / "omarchy-dictation/settings.json",
            vocabulary=root / "omarchy-dictation/vocabulary.txt",
            api_key=root / "omarchy-dictation/api_key",
            bin_dir=root / "bin",
            state=root / "state",
            elevenlabs_api_key=root / "omarchy-dictation/elevenlabs_api_key",
            runtime=root / "data/venv/bin/python",
        )
        # Keep installer tests independent of the parallel backend implementation.
        self.source = root / "plugin checkout's files"
        self.source.mkdir()
        for name in (install.SCRIPT, install.WRAPPER, "vocabulary.example.txt"):
            shutil.copyfile(ROOT / name, self.source / name)
        (self.source / "voice.py").write_text("# fake standalone backend\n")
        self.voxtype_original = '[whisper]\nmodel = "large-v3"\nlanguage = "auto"\n'
        self.bindings_original = (
            'hl.bind("SUPER + RETURN", function() hl.exec_cmd("ghostty") end)\n'
        )
        self.paths.voxtype_config.parent.mkdir(parents=True)
        self.paths.voxtype_config.write_text(self.voxtype_original)
        self.paths.bindings.parent.mkdir(parents=True)
        self.paths.bindings.write_text(self.bindings_original)
        self.system = FakeSystem()
        without_key_variables(self)

    def apply(self, **kwargs):
        with patch.object(install, "run", side_effect=self.system.run):
            install.apply(self.source, self.paths, prompt=lambda _: "test-key", **kwargs)

    def test_install_is_idempotent_and_uninstall_preserves_edits(self):
        self.apply()
        installed_voxtype = self.paths.voxtype_config.read_text()
        installed_bindings = self.paths.bindings.read_text()
        self.assertEqual(installed_voxtype, self.voxtype_original)
        for chord in install.DEFAULT_SETTINGS["chords"]:
            self.assertIn(json.dumps(chord), installed_bindings)
        self.assertIn('hl.is_key_down("Shift_R")', installed_bindings)
        self.assertIn("start --mode translate", installed_bindings)
        self.assertIn("omarchy-dictation stop", installed_bindings)
        self.assertIn("omarchy-dictation cancel", installed_bindings)
        self.assertIn("ignore_mods = true", installed_bindings)
        self.assertIn("non_consuming = true", installed_bindings)
        self.assertNotIn("dictation_recording", installed_bindings)
        self.assertNotIn("voxtype", installed_bindings)
        self.assertTrue((self.paths.bin_dir / "dictation-llm").stat().st_mode & 0o111)
        self.assertTrue((self.paths.bin_dir / "polish-clipboard").stat().st_mode & 0o111)
        self.assertIn(
            f'o.bind("SUPER + SHIFT + T", "Polish clipboard text into English", '
            f'"{self.paths.bin_dir}/polish-clipboard")',
            installed_bindings,
        )
        for name in install.COMMANDS:
            self.assertFalse((self.paths.bin_dir / name).is_symlink())
            subprocess.run(["sh", "-n", str(self.paths.bin_dir / name)], check=True)
        launcher = (self.paths.bin_dir / install.LAUNCHER).read_text()
        self.assertIn(str(self.paths.runtime), launcher)
        self.assertIn("voice.py", launcher)
        self.assertIn("'\"'\"'", launcher, "checkout paths must be shell quoted")
        self.assertTrue(self.paths.vocabulary.exists())
        self.assertEqual(self.paths.api_key.read_text(), "test-key\n")
        self.assertEqual(self.paths.api_key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.paths.elevenlabs_api_key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(install.load_settings(self.paths.settings), install.DEFAULT_SETTINGS)
        marker = json.loads((self.paths.state / "installed.json").read_text())
        self.assertEqual(marker["version"], install.VERSION)
        self.assertEqual(Path(marker["backup"], "config.toml").read_text(), self.voxtype_original)
        self.assertFalse(any("systemctl" in c or "voxtype" in c for c in self.system.commands))
        self.assertIn(
            ("/usr/bin/python", "-m", "venv", str(self.paths.runtime.parent.parent)),
            self.system.commands,
        )
        self.assertIn(
            (str(self.paths.runtime), "-m", "pip", "install", "websockets==15.0.1"),
            self.system.commands,
        )
        self.assertIn(("hyprctl", "reload", "config-only"), self.system.commands)

        self.apply()
        self.assertEqual(self.paths.voxtype_config.read_text(), installed_voxtype)
        self.assertEqual(self.paths.bindings.read_text(), installed_bindings)

        self.paths.bindings.write_text(installed_bindings + "-- later personal edit\n")
        self.apply(remove=True)
        self.assertEqual(self.paths.voxtype_config.read_text(), self.voxtype_original)
        self.assertEqual(
            self.paths.bindings.read_text(), self.bindings_original + "-- later personal edit\n"
        )
        self.assertFalse((self.paths.bin_dir / "dictation-llm").exists())
        self.assertFalse((self.paths.bin_dir / "polish-clipboard").exists())
        self.assertFalse((self.paths.bin_dir / install.LAUNCHER).exists())
        self.assertFalse((self.paths.state / "installed.json").exists())
        self.assertTrue(self.paths.api_key.exists(), "uninstall keeps the key")
        self.assertTrue(self.paths.elevenlabs_api_key.exists(), "uninstall keeps recognition key")
        self.assertTrue(self.paths.vocabulary.exists(), "uninstall keeps the vocabulary")

    def test_settings_change_replaces_the_managed_block(self):
        self.apply()
        self.paths.settings.parent.mkdir(parents=True, exist_ok=True)
        self.paths.settings.write_text(
            json.dumps(
                {
                    "chords": ["SUPER + D"],
                    "translate_key": "Alt_R",
                    "polish_chord": None,
                    "mode": "translate",
                    "microphone": "42",
                }
            )
        )
        self.apply()
        bindings = self.paths.bindings.read_text()
        self.assertEqual(bindings.count(install.LUA_BEGIN), 1)
        self.assertIn('{ "SUPER + D" }', bindings)
        self.assertNotIn("F23", bindings)
        self.assertIn('hl.is_key_down("Alt_R")', bindings)
        self.assertNotIn("polish-clipboard", bindings)
        self.assertNotIn("start --mode rephrase", bindings)
        self.assertIn('omarchy-dictation start" .. " --operation "', bindings)

    def test_refuses_to_shadow_existing_chords_but_ignores_unrelated_profiles(self):
        self.paths.voxtype_config.write_text(
            self.voxtype_original + '[profiles.translate]\npost_process_command = "mine"\n'
        )
        self.apply()
        self.assertIn('post_process_command = "mine"', self.paths.voxtype_config.read_text())
        self.paths.bindings.write_text('hl.bind("ALT + SHIFT + F23", function() end)\n')
        with self.assertRaisesRegex(ValueError, "already bound"):
            self.apply()
        self.paths.bindings.write_text('o.bind("SUPER + SHIFT + T", "Mine", "true")\n')
        with self.assertRaisesRegex(ValueError, "SUPER \\+ SHIFT \\+ T.*already bound"):
            self.apply()

    def test_failed_verification_restores_preexisting_commands_and_marker(self):
        self.apply()
        marker = (self.paths.state / "installed.json").read_text()
        bindings = self.paths.bindings.read_text()
        command = self.paths.bin_dir / install.LAUNCHER
        original = command.read_bytes()
        command.chmod(0o750)
        with (
            patch.object(install, "run", side_effect=self.system.run),
            patch.object(
                install, "restart_services", side_effect=[RuntimeError("bad config"), None]
            ),
            self.assertRaisesRegex(RuntimeError, "bad config"),
        ):
            install.apply(self.source, self.paths, prompt=lambda _: "test-key")
        self.assertEqual(self.paths.voxtype_config.read_text(), self.voxtype_original)
        self.assertEqual(self.paths.bindings.read_text(), bindings)
        self.assertEqual(command.read_bytes(), original)
        self.assertEqual(command.stat().st_mode & 0o777, 0o750)
        self.assertEqual((self.paths.state / "installed.json").read_text(), marker)

    def test_edit_block_round_trips_without_trailing_newline(self):
        original = "-- no newline at end"
        block = install.lua_block(install.DEFAULT_SETTINGS, Path("/opt/bin"))
        added = install.edit_block(original, block, install.LUA_BEGIN, install.LUA_END)
        self.assertEqual(added, original + block)
        removed = install.edit_block(added, block, install.LUA_BEGIN, install.LUA_END, remove=True)
        self.assertEqual(removed, original)
        with self.assertRaisesRegex(ValueError, "duplicated"):
            install.edit_block(added + block, block, install.LUA_BEGIN, install.LUA_END)

    def test_settings_validation(self):
        self.paths.settings.parent.mkdir(parents=True, exist_ok=True)
        for bad in (
            {"chords": []},
            {"chords": ["SUPER + ; rm -rf"]},
            {"translate_key": "Shift_R; os.execute"},
            {"post_process_timeout_ms": "20000"},
            {"post_process_timeout_ms": 10},
            {"post_process_timeout_ms": True},
            {"mode": "polish"},
            {"mode": []},
            {"microphone": ""},
            {"microphone": "--target=evil"},
            {"microphone": "input\nnode"},
            {"microphone": "input\x00node"},
            {"microphone": "input; rm"},
            {"microphone": 123},
            {"polish_chord": "SUPER + T; rm"},
            {"polish_chord": ["SUPER + T"]},
            {"unknown": 1},
            {"base_url": "ftp://example.com/v1"},
            {"base_url": "http://api.example.com/v1"},
            {"base_url": "https:///v1"},
            {"base_url": "https://api.example.com/v1#x"},
            {"base_url": "https://api.example.com/v1?x=1"},
            {"base_url": "http://localhost:x@evil.example/v1"},
            {"base_url": "https://user@api.example.com/v1"},
            {"base_url": "https://api.example.com:notaport/v1"},
            {"base_url": "https://api.example.com:99999/v1"},
            {"base_url": "https://api.example.com/\x00"},
            {"model": ""},
            {"model": "gpt 5"},
            {"reasoning_effort": "none; rm"},
            {"reasoning_effort": 1},
        ):
            with self.subTest(bad=bad):
                self.paths.settings.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    install.load_settings(self.paths.settings)
        self.paths.settings.write_text(json.dumps({"post_process_timeout_ms": 5000}))
        settings = install.load_settings(self.paths.settings)
        self.assertEqual(settings["post_process_timeout_ms"], 5000)
        self.assertEqual(settings["chords"], install.DEFAULT_SETTINGS["chords"])
        self.paths.settings.write_text(
            json.dumps(
                {
                    "base_url": "http://localhost:11434/v1",
                    "model": "qwen3:8b",
                    "reasoning_effort": None,
                }
            )
        )
        settings = install.load_settings(self.paths.settings)
        self.assertEqual(install.api_host(settings), "localhost:11434")
        for microphone in ("auto", "123", "alsa_input.usb-Example.analog-stereo"):
            self.paths.settings.write_text(json.dumps({"microphone": microphone}))
            self.assertEqual(install.load_settings(self.paths.settings)["microphone"], microphone)
        for malformed in ("[", "[]", "null"):
            self.paths.settings.write_text(malformed)
            with self.assertRaises(ValueError):
                install.load_settings(self.paths.settings)

    def test_internal_chord_collisions_abort_before_setup_side_effects(self):
        for settings in (
            {"polish_chord": "F23"},
            {"polish_chord": "SHIFT+F23"},
            {"chords": ["Escape"]},
            {"polish_chord": "Escape"},
            {"chords": ["CTRL + SHIFT + F23"], "polish_chord": "SHIFT + CTRL + F23"},
            {"chords": ["CTRL + F23", "CTRL+F23"]},
        ):
            with self.subTest(settings=settings):
                install.atomic_write(self.paths.settings, json.dumps(settings))
                with (
                    patch.object(install, "run") as run,
                    patch.object(install, "ensure_api_key") as key,
                    self.assertRaises(ValueError),
                ):
                    install.apply(self.source, self.paths)
                run.assert_not_called()
                key.assert_not_called()
                self.assertEqual(self.paths.bindings.read_text(), self.bindings_original)
                self.assertFalse((self.paths.state / "installed.json").exists())
        for polish in ("CTRL + SHIFT + F23", "CTRL + Escape", None):
            with self.subTest(valid_polish=polish):
                install.atomic_write(
                    self.paths.settings,
                    json.dumps({"chords": ["CTRL + F23"], "polish_chord": polish}),
                )
                self.assertEqual(install.load_settings(self.paths.settings)["polish_chord"], polish)

    def test_missing_api_key_aborts_before_touching_files(self):
        with (
            patch.object(install, "run", side_effect=self.system.run),
            self.assertRaisesRegex(ValueError, "API key is required"),
        ):
            install.apply(self.source, self.paths, prompt=lambda _: "   ")
        self.assertEqual(self.paths.voxtype_config.read_text(), self.voxtype_original)
        self.assertEqual(self.paths.bindings.read_text(), self.bindings_original)
        self.assertFalse((self.paths.bin_dir / install.LAUNCHER).exists())
        self.assertEqual(self.system.commands, [])

    def test_missing_recognition_key_aborts_before_desktop_or_pip(self):
        install.atomic_write(self.paths.api_key, "text-key\n", 0o600)
        with (
            patch.object(install, "run", side_effect=self.system.run),
            self.assertRaisesRegex(ValueError, "ElevenLabs"),
        ):
            install.apply(self.source, self.paths, prompt=lambda _: "")
        self.assertEqual(self.paths.bindings.read_text(), self.bindings_original)
        self.assertEqual(self.system.commands, [])

    def test_legacy_migration_and_vocabulary_are_narrow(self):
        block = (
            f"\n{install.TOML_BEGIN}\n[profiles.rephrase]\n"
            f'post_process_command = "old"\n{install.TOML_END}\n'
        )
        self.paths.voxtype_config.write_text(self.voxtype_original + block + "# personal\n")
        self.paths.legacy_vocabulary.write_text("Personal product\n")
        install.atomic_write(self.paths.state / "installed.json", '{"version":"0.1.0"}')
        self.assertIn("legacy", install.runtime_error(self.source, self.paths))
        self.apply()
        self.assertEqual(
            self.paths.voxtype_config.read_text(), self.voxtype_original + "# personal\n"
        )
        self.assertEqual(self.paths.vocabulary.read_text(), "Personal product\n")
        self.paths.legacy_vocabulary.write_text("Changed old vocabulary\n")
        self.apply()
        self.assertEqual(self.paths.vocabulary.read_text(), "Personal product\n")

    def test_no_voxtype_config_is_required(self):
        self.paths.voxtype_config.unlink()
        self.apply()
        self.assertFalse(self.paths.voxtype_config.exists())

    def test_uninstall_keeps_personal_command_and_invalid_settings(self):
        self.apply()
        command = self.paths.bin_dir / install.SCRIPT
        command.write_bytes(b"personal binary\x80")
        self.paths.settings.write_text("invalid JSON")
        self.apply(remove=True)
        self.assertEqual(command.read_bytes(), b"personal binary\x80")
        self.assertEqual(self.paths.settings.read_text(), "invalid JSON")

    def test_first_install_refuses_binary_command_before_side_effects(self):
        self.paths.bin_dir.mkdir()
        command = self.paths.bin_dir / install.LAUNCHER
        command.write_bytes(b"\x7fELF\x80personal")
        command.chmod(0o751)
        with (
            patch.object(install, "run", side_effect=self.system.run),
            patch.object(install, "ensure_api_key") as credentials,
            self.assertRaisesRegex(ValueError, "unowned command"),
        ):
            install.apply(self.source, self.paths, prompt=lambda _: "key")
        credentials.assert_not_called()
        self.assertEqual(self.system.commands, [])
        self.assertEqual(command.read_bytes(), b"\x7fELF\x80personal")
        self.assertEqual(command.stat().st_mode & 0o777, 0o751)
        self.assertFalse((self.paths.state / "installed.json").exists())
        self.assertFalse(self.paths.settings.exists())
        self.assertFalse(self.paths.vocabulary.exists())
        self.assertEqual(self.paths.bindings.read_text(), self.bindings_original)

    def legacy_commands(self):
        self.paths.bin_dir.mkdir(exist_ok=True)
        for name in install.LEGACY_HASHES:
            data = subprocess.run(
                ["git", "show", f"610ae09:{name}"], cwd=ROOT, check=True, capture_output=True
            ).stdout
            install.atomic_write(self.paths.bin_dir / name, data, 0o755)
        for name in install.LEGACY_LINKS:
            (self.paths.bin_dir / name).symlink_to("voxtype-llm")
        install.atomic_write(self.paths.state / "installed.json", '{"version":"0.1.0"}\n')

    def test_exact_legacy_commands_migrate_and_uninstall(self):
        for remove in (False, True):
            with self.subTest(remove=remove):
                self.legacy_commands()
                self.apply(remove=remove)
                for name in ("voxtype-llm", *install.LEGACY_LINKS):
                    path = self.paths.bin_dir / name
                    self.assertFalse(path.exists() or path.is_symlink())
                if not remove:
                    self.apply(remove=True)
                self.assertFalse((self.paths.bin_dir / install.WRAPPER).exists())

    def test_markerless_legacy_remnants_preserve_replaced_links(self):
        self.legacy_commands()
        (self.paths.state / "installed.json").unlink()
        replacement = self.paths.bin_dir / install.LEGACY_LINKS[0]
        replacement.unlink()
        replacement.symlink_to("personal-target")
        self.apply(remove=True)
        self.assertEqual(os.readlink(replacement), "personal-target")
        self.assertFalse((self.paths.bin_dir / "voxtype-llm").exists())
        self.assertFalse((self.paths.bin_dir / install.LEGACY_LINKS[1]).is_symlink())
        self.assertFalse((self.paths.bin_dir / install.WRAPPER).exists())

    def test_legacy_removal_rolls_back_files_links_and_marker(self):
        self.legacy_commands()
        files = {name: (self.paths.bin_dir / name).read_bytes() for name in install.LEGACY_HASHES}
        marker = (self.paths.state / "installed.json").read_bytes()
        for remove in (False, True):
            with (
                self.subTest(remove=remove),
                patch.object(install, "run", side_effect=self.system.run),
                patch.object(install, "restart_services", side_effect=[RuntimeError("bad"), None]),
                self.assertRaisesRegex(RuntimeError, "bad"),
            ):
                install.apply(self.source, self.paths, remove=remove, prompt=lambda _: "key")
            for name, data in files.items():
                self.assertEqual((self.paths.bin_dir / name).read_bytes(), data)
            for name in install.LEGACY_LINKS:
                self.assertEqual(os.readlink(self.paths.bin_dir / name), "voxtype-llm")
            self.assertEqual((self.paths.state / "installed.json").read_bytes(), marker)

    def test_legacy_personal_replacements_are_preserved(self):
        self.legacy_commands()
        (self.paths.bin_dir / "voxtype-llm").write_text("personal script")
        (self.paths.bin_dir / install.WRAPPER).write_text("personal clipboard")
        with self.assertRaisesRegex(ValueError, "unowned command"):
            self.apply()
        self.apply(remove=True)
        self.assertEqual((self.paths.bin_dir / "voxtype-llm").read_text(), "personal script")
        self.assertEqual((self.paths.bin_dir / install.WRAPPER).read_text(), "personal clipboard")
        for name in install.LEGACY_LINKS:
            self.assertTrue((self.paths.bin_dir / name).is_symlink())

    def test_collisions_include_symlinks_directories_and_replaced_launchers(self):
        self.apply()
        for kind in ("symlink", "directory", "file"):
            path = self.paths.bin_dir / install.LAUNCHER
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
            if kind == "symlink":
                path.symlink_to("missing-personal-target")
            elif kind == "directory":
                path.mkdir()
            else:
                path.write_text("personal")
            with (
                self.subTest(kind=kind),
                patch.object(install, "ensure_api_key") as credentials,
                patch.object(install, "provision_runtime") as provision,
                self.assertRaisesRegex(ValueError, "unowned command"),
            ):
                self.apply()
            credentials.assert_not_called()
            provision.assert_not_called()
        with (
            patch.object(install.Paths, "default", return_value=self.paths),
            patch.object(sys, "argv", ["install.py"]),
            patch("builtins.input") as confirmation,
            patch.object(install, "preflight") as preflight,
            self.assertRaisesRegex(ValueError, "unowned command"),
        ):
            install.main()
        confirmation.assert_not_called()
        preflight.assert_not_called()

    @unittest.skipUnless(shutil.which("lua"), "Lua not installed")
    def test_lua_callbacks_track_physical_key_and_operation(self):
        settings = dict(install.DEFAULT_SETTINGS, chords=["F23", "SHIFT + F23", "SUPER + D"])
        harness = """
local presses, releases, commands = {}, {}, {}
local translate = false
hl = {
  bind = function(key, callback, options)
    if options.release then releases[key] = callback else presses[key] = callback end
  end,
  is_key_down = function() return translate end,
  exec_cmd = function(command) table.insert(commands, command) end
}
o = {bind = function() end}
"""
        checks = """
releases.D()
assert(#commands == 0)
presses.F23()
presses.F23()
presses['SHIFT + F23']()
presses['SUPER + D']()
releases.D()
assert(#commands == 1)
releases.F23()
releases.F23()
assert(#commands == 2)
local first = commands[1]:match('start %-%-operation ([%w_-]+)$')
assert(first and #first <= 128)
assert(commands[2]:match('stop %-%-operation ([%w_-]+)$') == first)
translate = true
presses['SUPER + D']()
releases.F23()
assert(#commands == 3)
releases.D()
assert(#commands == 4)
local second = commands[3]:match('start %-%-mode translate %-%-operation ([%w_-]+)$')
assert(second and second ~= first)
assert(commands[4]:match('stop %-%-operation ([%w_-]+)$') == second)
"""
        subprocess.run(
            ["lua", "-"],
            input=harness + install.lua_block(settings, Path("/opt/bin")) + checks,
            text=True,
            check=True,
            capture_output=True,
        )

    def test_credentials_wake_only_existing_marker_after_both_saves(self):
        marker = self.paths.state / "installed.json"
        original = b'{ "version": "0.2.0", "custom": true }\n'
        real_save = install.ensure_api_key
        for outcome in ("failure", "success", "missing"):
            if outcome == "missing":
                marker.unlink()
            else:
                install.atomic_write(marker, original, 0o640)
            inode = marker.stat().st_ino if marker.exists() else None
            saved = []

            def save(path, host, replace=False, variables=()):
                self.assertEqual(marker.stat().st_ino if marker.exists() else None, inode)
                saved.append(path)
                if outcome == "failure" and len(saved) == 2:
                    raise ValueError("repair failed")
                real_save(
                    path,
                    host,
                    prompt=lambda _: "repaired-key",
                    replace=replace,
                    variables=variables,
                )

            with (
                self.subTest(outcome=outcome),
                patch.object(install.Paths, "default", return_value=self.paths),
                patch.object(sys, "argv", ["install.py", "--credentials"]),
                patch.object(install.os, "isatty", return_value=True),
                patch.object(install, "ensure_api_key", side_effect=save),
            ):
                if outcome == "failure":
                    with self.assertRaisesRegex(ValueError, "repair failed"):
                        install.main()
                else:
                    install.main()
            self.assertEqual(saved, [self.paths.api_key, self.paths.elevenlabs_api_key])
            if outcome == "missing":
                self.assertFalse(marker.exists())
            else:
                self.assertEqual(marker.read_bytes(), original)
                self.assertEqual(marker.stat().st_mode & 0o777, 0o640)
                self.assertEqual(marker.stat().st_ino == inode, outcome == "failure")

    def test_detached_credentials_launcher_does_not_wake_marker(self):
        marker = self.paths.state / "installed.json"
        install.atomic_write(marker, '{"version":"0.2.0"}\n')
        inode = marker.stat().st_ino
        with (
            patch.object(install.Paths, "default", return_value=self.paths),
            patch.object(sys, "argv", ["install.py", "--credentials"]),
            patch.object(install.os, "isatty", return_value=False),
            patch.object(install, "run", side_effect=self.system.run),
            patch.object(install, "ensure_api_key") as save,
        ):
            install.main()
        save.assert_not_called()
        self.assertEqual(marker.stat().st_ino, inode)

    def test_run_daemon_checks_dependencies_without_installing_or_leaking(self):
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch.object(install.subprocess, "Popen") as execute,
            patch.object(install, "run") as run,
        ):
            install.run_daemon(self.source, self.paths)
        state = json.loads(output.getvalue())
        self.assertEqual(state["phase"], "error")
        execute.assert_not_called()
        run.assert_not_called()
        self.apply()
        install.atomic_write(self.paths.runtime, "fake runtime", 0o755)
        with (
            patch.object(install.shutil, "which", return_value="/mock/tool"),
            patch.object(
                install,
                "run",
                side_effect=subprocess.CalledProcessError(1, "dependency", stderr="secret-key"),
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            install.run_daemon(self.source, self.paths)
        self.assertEqual(json.loads(output.getvalue())["phase"], "error")
        self.assertNotIn("secret-key", output.getvalue())
        with (
            patch.object(install.shutil, "which", return_value="/mock/tool"),
            patch.object(install, "run", side_effect=self.system.run),
            patch.object(install.subprocess, "Popen") as execute,
        ):
            install.run_daemon(self.source, self.paths)
        execute.assert_called_once_with(
            [str(self.paths.runtime), str(self.source / "voice.py"), "daemon", "--stdio"],
            start_new_session=True,
        )
        execute.return_value.wait.assert_called_once_with()
        self.assertNotIn("pip", self.system.commands[-1])
        with patch.object(install.shutil, "which", return_value=None):
            self.assertIn("pw-record", install.runtime_error(self.source, self.paths))

    def test_preflight_requires_only_standalone_tools_and_desktop(self):
        with (
            patch.object(install.os, "geteuid", return_value=1000),
            patch.object(install.os, "access", return_value=True),
            patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "test"}),
            patch.object(install.shutil, "which", return_value="/mock/tool") as which,
        ):
            install.preflight(self.paths)
        checked = [call.args[0] for call in which.call_args_list]
        self.assertNotIn("voxtype", checked)
        self.assertNotIn("systemctl", checked)
        self.assertIn("wtype", checked)
        self.assertIn("omarchy-version", checked)

    def test_runtime_version_guard_rejects_old_new_and_reused_environments(self):
        for existing in (False, True):
            for version in ((3, 10), (3, 11)):
                with self.subTest(existing=existing, version=version):
                    self.paths.runtime.unlink(missing_ok=True)
                    if existing:
                        install.atomic_write(self.paths.runtime, "fake interpreter", 0o755)
                    commands = []

                    def run(*args):
                        commands.append(args)
                        if args[1] == "-c":
                            # Execute the actual guard against both sides of its
                            # version boundary, independently of the host Python.
                            subprocess.run(
                                [
                                    sys.executable,
                                    "-c",
                                    f"import sys; sys.version_info={version}; " + args[2],
                                ],
                                check=True,
                            )
                        return ""

                    with patch.object(install, "run", side_effect=run):
                        if version == (3, 10):
                            with self.assertRaisesRegex(RuntimeError, "Python 3.11 or newer"):
                                install.provision_runtime(self.paths)
                        else:
                            install.provision_runtime(self.paths)
                    self.assertEqual(any("venv" in args for args in commands), not existing)
                    self.assertEqual(any("pip" in args for args in commands), version == (3, 11))
                    self.assertFalse((self.paths.state / "installed.json").exists())

    def test_launch_and_credentials_open_terminal_without_setup(self):
        for option in ("--launch", "--credentials"):
            with (
                self.subTest(option=option),
                patch.object(install.Paths, "default", return_value=self.paths),
                patch.object(sys, "argv", ["install.py", option]),
                patch.object(install.os, "isatty", return_value=False),
                patch.object(install, "run", side_effect=self.system.run),
                patch.object(install, "preflight") as preflight,
            ):
                install.main()
                preflight.assert_not_called()
                self.assertEqual(
                    self.system.commands[-1][0],
                    "omarchy-launch-floating-terminal-with-presentation",
                )

        with (
            patch.object(sys, "argv", ["install.py", "--launch"]),
            patch.object(install, "runtime_error", return_value=None),
            patch.object(install, "run") as run,
        ):
            install.main()
        run.assert_not_called()

    def test_key_replacement_is_private_and_does_not_echo_secrets(self):
        install.atomic_write(self.paths.api_key, "previous\n", 0o600)
        output = io.StringIO()
        with redirect_stdout(output):
            install.ensure_api_key(
                self.paths.api_key, "provider", prompt=lambda _: "replacement", replace=True
            )
        self.assertEqual(self.paths.api_key.read_text(), "replacement\n")
        self.assertEqual(self.paths.api_key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.paths.api_key.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(output.getvalue(), "")

    def apply_without_prompt(self):
        def prompt(_):
            raise AssertionError("setup must not ask for a key the environment provides")

        output = io.StringIO()
        with redirect_stdout(output), patch.object(install, "run", side_effect=self.system.run):
            install.apply(self.source, self.paths, prompt=prompt)
        return output.getvalue()

    def ready(self):
        install.atomic_write(self.paths.runtime, "fake runtime", 0o755)
        with (
            patch.object(install.shutil, "which", return_value="/mock/tool"),
            patch.object(install, "run", side_effect=self.system.run),
        ):
            return install.runtime_error(self.source, self.paths)

    def test_environment_keys_skip_prompts_and_key_files(self):
        os.environ["CEREBRAS_API_KEY"] = " env-text-secret\n"
        os.environ["ELEVENLABS_API_KEY"] = "env-recognition-secret"
        output = self.apply_without_prompt()
        self.assertFalse(self.paths.api_key.exists())
        self.assertFalse(self.paths.elevenlabs_api_key.exists())
        self.assertIn("api.cerebras.ai key from $CEREBRAS_API_KEY", output)
        self.assertIn("ElevenLabs recognition key from $ELEVENLABS_API_KEY", output)
        self.assertNotIn("secret", output)
        self.assertIsNone(self.ready())
        os.environ["DICTATION_LLM_API_KEY"] = "generic-secret"
        os.environ["CEREBRAS_API_KEY"] = ""
        self.assertIsNone(self.ready())
        self.assertIn("$DICTATION_LLM_API_KEY", self.apply_without_prompt())
        for blank in ("", "  \n"):
            with self.subTest(blank=blank):
                os.environ["DICTATION_LLM_API_KEY"] = blank
                self.assertIn("Credentials missing", self.ready())
        os.environ["CEREBRAS_API_KEY"] = "env-text-secret"
        os.environ["ELEVENLABS_API_KEY"] = " "
        self.assertIn("Credentials missing", self.ready())

    def test_provider_variable_applies_only_to_its_host(self):
        os.environ["CEREBRAS_API_KEY"] = "cerebras-secret"
        os.environ["ELEVENLABS_API_KEY"] = "recognition-secret"
        install.atomic_write(
            self.paths.settings, json.dumps({"base_url": "https://api.openai.com/v1"})
        )
        with self.assertRaisesRegex(AssertionError, "must not ask"):
            self.apply_without_prompt()
        os.environ["OPENAI_API_KEY"] = "openai-secret"
        self.assertIn("$OPENAI_API_KEY", self.apply_without_prompt())
        self.assertFalse(self.paths.api_key.exists())
        variables = {
            "https://API.Cerebras.ai:443/v1": ("DICTATION_LLM_API_KEY", "CEREBRAS_API_KEY"),
            "https://openrouter.ai/api/v1": ("DICTATION_LLM_API_KEY", "OPENROUTER_API_KEY"),
            "http://localhost:11434/v1": ("DICTATION_LLM_API_KEY",),
            "https://api.cerebras.ai.example/v1": ("DICTATION_LLM_API_KEY",),
        }
        for url, expected in variables.items():
            self.assertEqual(install.text_key_variables(url), expected)

    def test_environment_keys_override_existing_files_without_touching_them(self):
        for path in (self.paths.api_key, self.paths.elevenlabs_api_key):
            install.atomic_write(path, "file-secret\n", 0o644)
        os.environ["CEREBRAS_API_KEY"] = "env-secret"
        os.environ["ELEVENLABS_API_KEY"] = "env-secret"
        self.apply_without_prompt()
        for path in (self.paths.api_key, self.paths.elevenlabs_api_key):
            self.assertEqual(path.read_text(), "file-secret\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        variables = install.text_key_variables(install.DEFAULT_SETTINGS["base_url"])
        self.assertEqual(install.read_key(self.paths.api_key, variables), "env-secret")
        os.environ["DICTATION_LLM_API_KEY"] = "generic-secret"
        self.assertEqual(install.read_key(self.paths.api_key, variables), "generic-secret")
        os.environ["DICTATION_LLM_API_KEY"] = os.environ["CEREBRAS_API_KEY"] = " "
        self.assertEqual(install.read_key(self.paths.api_key, variables), "file-secret")

    def test_explicit_credentials_write_files_and_name_overriding_variables(self):
        os.environ["CEREBRAS_API_KEY"] = "env-secret"
        install.atomic_write(self.paths.state / "installed.json", '{"version":"0.2.0"}\n')
        real_save = install.ensure_api_key

        def save(path, host, replace=False, variables=()):
            real_save(
                path, host, prompt=lambda _: "typed-secret", replace=replace, variables=variables
            )

        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch.object(install.Paths, "default", return_value=self.paths),
            patch.object(sys, "argv", ["install.py", "--credentials"]),
            patch.object(install.os, "isatty", return_value=True),
            patch.object(install, "ensure_api_key", side_effect=save),
        ):
            install.main()
        self.assertEqual(self.paths.api_key.read_text(), "typed-secret\n")
        self.assertEqual(self.paths.elevenlabs_api_key.read_text(), "typed-secret\n")
        self.assertIn("$CEREBRAS_API_KEY is set here and takes precedence", output.getvalue())
        self.assertNotIn("ELEVENLABS_API_KEY", output.getvalue())
        self.assertNotIn("secret", output.getvalue())

    def test_installed_script_launcher_uses_current_checkout(self):
        self.apply()
        (self.source / install.SCRIPT).write_text("printf 'updated source'\n")
        result = subprocess.run(
            [str(self.paths.bin_dir / install.SCRIPT)], check=True, capture_output=True, text=True
        )
        self.assertEqual(result.stdout, "updated source")


class FakeProvider(BaseHTTPRequestHandler):
    """OpenAI-compatible endpoint that records requests and answers from a script."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, self.headers["Authorization"], body))
        status, reply = self.server.replies.pop(0)
        data = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ScriptTests(unittest.TestCase):
    def call_provider(self, settings, replies, host="127.0.0.1"):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
        server.requests, server.replies = [], replies
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as config:
            settings_file = Path(config, "settings.json")
            base_url = f"http://{host}:{server.server_port}/v1/"
            settings_file.write_text(json.dumps({"base_url": base_url, **settings}))
            result = subprocess.run(
                ["bash", str(ROOT / "dictation-llm"), "--mode", "translate"],
                input="你好",
                capture_output=True,
                text=True,
                env={
                    "HOME": "/nonexistent",
                    "PATH": "/usr/bin:/bin",
                    "DICTATION_LLM_API_KEY": "test-key",
                    "DICTATION_LLM_SETTINGS_FILE": str(settings_file),
                    # A dead proxy: local providers must be reached directly.
                    "http_proxy": "http://127.0.0.1:9",
                    "ALL_PROXY": "http://127.0.0.1:9",
                },
                check=True,
            )
        return result, server.requests

    def test_settings_select_the_provider(self):
        answer = {"choices": [{"message": {"content": '{"text": "Hello"}'}}]}
        result, requests = self.call_provider({"model": "local-model"}, [(200, answer)])
        self.assertEqual(result.stdout, "Hello")
        path, auth, body = requests[0]
        self.assertEqual((path, auth), ("/v1/chat/completions", "Bearer test-key"))
        self.assertEqual(body["model"], "local-model")
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertIn("response_format", body)

        result, requests = self.call_provider(
            {"reasoning_effort": None}, [(200, answer)], host="LOCALHOST"
        )
        self.assertEqual(result.stdout, "Hello")
        self.assertEqual(requests[0][2]["model"], install.DEFAULT_SETTINGS["model"])
        self.assertNotIn("reasoning_effort", requests[0][2])

    def test_explicit_invalid_settings_fail_closed(self):
        for settings in (
            {"base_url": None},
            {"model": False},
            {"model": "", "reasoning_effort": None},
            {"base_url": ""},
            {"reasoning_effort": 1},
        ):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as config:
                settings_file = Path(config, "settings.json")
                settings_file.write_text(json.dumps(settings))
                result = subprocess.run(
                    ["bash", str(ROOT / "dictation-llm"), "--mode", "rephrase"],
                    input="hello world",
                    capture_output=True,
                    text=True,
                    env={
                        "HOME": "/nonexistent",
                        "PATH": "/usr/bin:/bin",
                        "DICTATION_LLM_API_KEY": "test-key",
                        "DICTATION_LLM_SETTINGS_FILE": str(settings_file),
                    },
                    check=True,
                )
                self.assertEqual(result.stdout, "hello world")
                self.assertIn("cannot read", result.stderr)

    def test_rejected_request_is_retried_without_optional_fields(self):
        answer = {"choices": [{"message": {"content": '{"text": "Hello"}'}}]}
        result, requests = self.call_provider(
            {}, [(400, {"error": "unsupported parameter"}), (200, answer)]
        )
        self.assertEqual(result.stdout, "Hello")
        self.assertEqual(len(requests), 2)
        minimal = requests[1][2]
        self.assertEqual(set(minimal), {"model", "max_completion_tokens", "messages"})

    def test_script_defaults_match_the_installer(self):
        script = (ROOT / "dictation-llm").read_text()
        defaults = install.DEFAULT_SETTINGS
        self.assertIn(f'DEFAULT_BASE_URL="{defaults["base_url"]}"', script)
        self.assertIn(f'DEFAULT_MODEL="{defaults["model"]}"', script)
        self.assertIn(f'DEFAULT_REASONING_EFFORT="{defaults["reasoning_effort"]}"', script)

    def test_unsafe_base_urls_are_refused(self):
        for url in (
            "http://api.example.com/v1",
            "http://localhost:x@evil.example/v1",
            "http://localhost@evil.example",
            "https://api.example.com/v1#x",
            "https://api.example.com/v1?x=1",
            "ftp://api.example.com/v1",
        ):
            with self.subTest(url=url):
                result = subprocess.run(
                    ["bash", str(ROOT / "dictation-llm"), "--mode", "rephrase"],
                    input="hello world",
                    capture_output=True,
                    text=True,
                    env={
                        "HOME": "/nonexistent",
                        "PATH": "/usr/bin:/bin",
                        "DICTATION_LLM_API_KEY": "test-key",
                        "DICTATION_LLM_BASE_URL": url,
                    },
                    check=True,
                )
                self.assertEqual(result.stdout, "hello world")
                self.assertIn("base_url must be https", result.stderr)

    def test_script_parses_and_passes_input_through_without_key(self):
        script = ROOT / "dictation-llm"
        subprocess.run(["bash", "-n", str(script)], check=True)
        result = subprocess.run(
            ["bash", str(script), "--mode", "rephrase"],
            input="hello world",
            capture_output=True,
            text=True,
            env={
                "HOME": "/nonexistent",
                "PATH": "/usr/bin:/bin",
                "DICTATION_LLM_API_KEY_FILE": "/nonexistent",
            },
            check=True,
        )
        self.assertEqual(result.stdout, "hello world")
        self.assertIn("no API key", result.stderr)
        strict = subprocess.run(
            ["bash", str(script), "--mode", "polish", "--strict"],
            input="hello world",
            capture_output=True,
            text=True,
            env={
                "HOME": "/nonexistent",
                "PATH": "/usr/bin:/bin",
                "DICTATION_LLM_API_KEY_FILE": "/nonexistent",
            },
        )
        self.assertEqual((strict.returncode, strict.stdout), (1, ""))
        subprocess.run(["bash", "-n", str(ROOT / "polish-clipboard")], check=True)

    def test_no_secrets_and_no_home_paths_committed(self):
        for path in ROOT.iterdir():
            if path.is_file() and path.suffix != ".pyc":
                text = path.read_text(errors="replace")
                with self.subTest(path=path.name):
                    self.assertNotRegex(text, r"csk-[A-Za-z0-9]{20,}")
                    self.assertNotIn("/home/", text)

    @unittest.skipUnless(shutil.which("omarchy-plugin-validate"), "Omarchy CLI not installed")
    def test_manifest_passes_omarchy_validation(self):
        subprocess.run(["omarchy-plugin-validate", str(ROOT)], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
