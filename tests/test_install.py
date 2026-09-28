"""Installer and script checks that run without Voxtype, Hyprland or the API."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import install  # noqa: E402

PROBE_OUTPUT = "Error: Profile '__x__' not found.\n\nAvailable profiles: rephrase, translate\n"


class FakeSystem:
    """Stands in for subprocess calls; records them and answers the probe."""

    def __init__(self):
        self.commands = []

    def run(self, *args, **kwargs):
        self.commands.append(args)
        return ""

    def probe(self, args, **kwargs):
        self.commands.append(tuple(args))
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=PROBE_OUTPUT)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = install.Paths(
            voxtype_config=root / "voxtype/config.toml",
            bindings=root / "hypr/bindings.lua",
            settings=root / "omarchy-dictation/settings.json",
            vocabulary=root / "voxtype/vocabulary.txt",
            api_key=root / "cerebras/api_key",
            bin_dir=root / "bin",
            state=root / "state",
        )
        self.voxtype_original = '[whisper]\nmodel = "large-v3"\nlanguage = "auto"\n'
        self.bindings_original = (
            'hl.bind("SUPER + RETURN", function() hl.exec_cmd("ghostty") end)\n'
        )
        self.paths.voxtype_config.parent.mkdir(parents=True)
        self.paths.voxtype_config.write_text(self.voxtype_original)
        self.paths.bindings.parent.mkdir(parents=True)
        self.paths.bindings.write_text(self.bindings_original)
        self.system = FakeSystem()

    def apply(self, **kwargs):
        with (
            patch.object(install, "run", side_effect=self.system.run),
            patch.object(install.subprocess, "run", side_effect=self.system.probe),
        ):
            install.apply(ROOT, self.paths, prompt=lambda _: "csk-test-key", **kwargs)

    def test_install_is_idempotent_and_uninstall_preserves_edits(self):
        self.apply()
        installed_voxtype = self.paths.voxtype_config.read_text()
        installed_bindings = self.paths.bindings.read_text()
        self.assertTrue(installed_voxtype.startswith(self.voxtype_original))
        self.assertIn("[profiles.rephrase]", installed_voxtype)
        self.assertIn("[profiles.translate]", installed_voxtype)
        self.assertIn(f'"{self.paths.bin_dir}/voxtype-translate-en"', installed_voxtype)
        self.assertIn('"ALT + SHIFT + F23", "SUPER + SHIFT + F23"', installed_bindings)
        self.assertIn('hl.is_key_down("Shift_R")', installed_bindings)
        self.assertTrue((self.paths.bin_dir / "voxtype-llm").stat().st_mode & 0o111)
        self.assertTrue((self.paths.bin_dir / "polish-clipboard").stat().st_mode & 0o111)
        self.assertIn(
            f'o.bind("SUPER + SHIFT + T", "Polish clipboard text into English", '
            f'"{self.paths.bin_dir}/polish-clipboard")',
            installed_bindings,
        )
        self.assertEqual(os.readlink(self.paths.bin_dir / "voxtype-rephrase"), "voxtype-llm")
        self.assertEqual(os.readlink(self.paths.bin_dir / "voxtype-translate-en"), "voxtype-llm")
        self.assertTrue(self.paths.vocabulary.exists())
        self.assertEqual(self.paths.api_key.read_text(), "csk-test-key\n")
        self.assertEqual(self.paths.api_key.stat().st_mode & 0o777, 0o600)
        marker = json.loads((self.paths.state / "installed.json").read_text())
        self.assertEqual(marker["version"], install.VERSION)
        self.assertEqual(Path(marker["backup"], "config.toml").read_text(), self.voxtype_original)
        self.assertIn(("systemctl", "--user", "restart", "voxtype.service"), self.system.commands)
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
        self.assertFalse((self.paths.bin_dir / "voxtype-llm").exists())
        self.assertFalse((self.paths.bin_dir / "polish-clipboard").exists())
        self.assertFalse((self.paths.bin_dir / "voxtype-rephrase").is_symlink())
        self.assertFalse((self.paths.state / "installed.json").exists())
        self.assertTrue(self.paths.api_key.exists(), "uninstall keeps the key")
        self.assertTrue(self.paths.vocabulary.exists(), "uninstall keeps the vocabulary")

    def test_settings_change_replaces_the_managed_block(self):
        self.apply()
        self.paths.settings.parent.mkdir(parents=True)
        self.paths.settings.write_text(
            json.dumps({"chords": ["SUPER + D"], "translate_key": "Alt_R", "polish_chord": None})
        )
        self.apply()
        bindings = self.paths.bindings.read_text()
        self.assertEqual(bindings.count(install.LUA_BEGIN), 1)
        self.assertIn('{ "SUPER + D" }', bindings)
        self.assertNotIn("F23", bindings)
        self.assertIn('hl.is_key_down("Alt_R")', bindings)
        self.assertNotIn("polish-clipboard", bindings)

    def test_refuses_to_shadow_existing_profiles_or_chords(self):
        self.paths.voxtype_config.write_text(
            self.voxtype_original + '[profiles.translate]\npost_process_command = "mine"\n'
        )
        with self.assertRaisesRegex(ValueError, r"\[profiles\.translate\] already exists"):
            self.apply()
        self.paths.voxtype_config.write_text(self.voxtype_original)
        self.paths.bindings.write_text('hl.bind("ALT + SHIFT + F23", function() end)\n')
        with self.assertRaisesRegex(ValueError, "already bound"):
            self.apply()
        self.paths.bindings.write_text('o.bind("SUPER + SHIFT + T", "Mine", "true")\n')
        with self.assertRaisesRegex(ValueError, "SUPER \\+ SHIFT \\+ T.*already bound"):
            self.apply()
        self.assertFalse((self.paths.bin_dir / "voxtype-llm").exists())

    def test_verification_waits_for_the_daemon_to_come_back(self):
        answers = iter(
            [
                "Error: Voxtype daemon is not running (stale PID file removed).\n",
                "Error: Voxtype daemon is not running.\n",
                PROBE_OUTPUT,
            ]
        )

        def slow_probe(args, **kwargs):
            self.system.commands.append(tuple(args))
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=next(answers))

        with (
            patch.object(install, "run", side_effect=self.system.run),
            patch.object(install.subprocess, "run", side_effect=slow_probe),
            patch.object(install.time, "sleep") as sleep,
        ):
            install.apply(ROOT, self.paths, prompt=lambda _: "csk-test-key")
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue((self.paths.state / "installed.json").exists())

    def test_failed_verification_rolls_everything_back(self):
        def bad_probe(args, **kwargs):
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="Available profiles: other\n"
            )

        with (
            patch.object(install, "run", side_effect=self.system.run),
            patch.object(install.subprocess, "run", side_effect=bad_probe),
            patch.object(install.time, "sleep"),
            self.assertRaisesRegex(RuntimeError, "does not list the profiles"),
        ):
            install.apply(ROOT, self.paths, prompt=lambda _: "csk-test-key")
        self.assertEqual(self.paths.voxtype_config.read_text(), self.voxtype_original)
        self.assertEqual(self.paths.bindings.read_text(), self.bindings_original)
        self.assertFalse((self.paths.bin_dir / "voxtype-llm").exists())
        self.assertFalse((self.paths.state / "installed.json").exists())

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
        self.paths.settings.parent.mkdir(parents=True)
        for bad in (
            {"chords": []},
            {"chords": ["SUPER + ; rm -rf"]},
            {"translate_key": "Shift_R; os.execute"},
            {"post_process_timeout_ms": "20000"},
            {"post_process_timeout_ms": 10},
            {"polish_chord": "SUPER + T; rm"},
            {"polish_chord": ["SUPER + T"]},
            {"unknown": 1},
        ):
            with self.subTest(bad=bad):
                self.paths.settings.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    install.load_settings(self.paths.settings)
        self.paths.settings.write_text(json.dumps({"post_process_timeout_ms": 5000}))
        settings = install.load_settings(self.paths.settings)
        self.assertEqual(settings["post_process_timeout_ms"], 5000)
        self.assertEqual(settings["chords"], install.DEFAULT_SETTINGS["chords"])

    def test_missing_api_key_aborts_before_touching_files(self):
        with (
            patch.object(install, "run", side_effect=self.system.run),
            self.assertRaisesRegex(ValueError, "API key is required"),
        ):
            install.apply(ROOT, self.paths, prompt=lambda _: "   ")
        self.assertEqual(self.paths.voxtype_config.read_text(), self.voxtype_original)
        self.assertFalse((self.paths.bin_dir / "voxtype-llm").exists())


class ScriptTests(unittest.TestCase):
    def test_script_parses_and_passes_input_through_without_key(self):
        script = ROOT / "voxtype-llm"
        subprocess.run(["bash", "-n", str(script)], check=True)
        result = subprocess.run(
            ["bash", str(script), "--mode", "rephrase"],
            input="hello world",
            capture_output=True,
            text=True,
            env={
                "HOME": "/nonexistent",
                "PATH": "/usr/bin:/bin",
                "CEREBRAS_API_KEY_FILE": "/nonexistent",
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
                "CEREBRAS_API_KEY_FILE": "/nonexistent",
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
