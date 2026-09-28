#!/usr/bin/python
"""Install or remove the krosdai.dictation setup for the current desktop user.

The plugin adds two Voxtype profiles (`rephrase`, `translate`) backed by the
`voxtype-llm` script, a hold-to-dictate key binding in the user's Hyprland Lua
bindings, and a second binding that rewrites the clipboard text as English through
`polish-clipboard`. Both file edits are managed blocks between BEGIN/END markers;
nothing outside the markers is ever rewritten.
"""

import argparse
import fcntl
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

ID = "krosdai.dictation"
VERSION = "0.1.0"
MIN_VOXTYPE = (1, 1)
PROFILES = ("rephrase", "translate")
COMMANDS = {"rephrase": "voxtype-rephrase", "translate": "voxtype-translate-en"}
SCRIPT = "voxtype-llm"
WRAPPER = "polish-clipboard"

TOML_BEGIN = f"# BEGIN {ID}"
TOML_END = f"# END {ID}"
LUA_BEGIN = f"-- BEGIN {ID}"
LUA_END = f"-- END {ID}"

DEFAULT_SETTINGS = {
    # Hyprland chords that start a recording while held. The Copilot key on many
    # laptops reports Shift + Meta + F23; with Omarchy's default layout the left
    # Meta key is exposed as Alt, so both spellings are bound.
    "chords": ["ALT + SHIFT + F23", "SUPER + SHIFT + F23"],
    # Extra key that, when held together with a chord, selects the translate profile.
    "translate_key": "Shift_R",
    # How long Voxtype waits for the cleanup script before typing the raw text.
    "post_process_timeout_ms": 20000,
    # Chord that rewrites the clipboard text as native English; null disables it.
    "polish_chord": "SUPER + SHIFT + T",
    # Any OpenAI-compatible Chat Completions API; voxtype-llm reads these three on
    # every call and keeps its own copy of the defaults.
    "base_url": "https://api.cerebras.ai/v1",
    "model": "qwen-3.8-27b",
    # Sent as reasoning_effort; null omits it for models that do not accept it.
    "reasoning_effort": "none",
}


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout


@dataclass(frozen=True)
class Paths:
    voxtype_config: Path
    bindings: Path
    settings: Path
    vocabulary: Path
    api_key: Path
    bin_dir: Path
    state: Path

    @classmethod
    def default(cls):
        config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        return cls(
            voxtype_config=config / "voxtype/config.toml",
            bindings=config / "hypr/bindings.lua",
            settings=config / "omarchy-dictation/settings.json",
            vocabulary=config / "voxtype/vocabulary.txt",
            api_key=config / "omarchy-dictation/api_key",
            bin_dir=Path.home() / ".local/bin",
            state=state / "omarchy-dictation",
        )


# ----------------------------------------------------------------- settings
def load_settings(path):
    settings = dict(DEFAULT_SETTINGS)
    if path.exists():
        loaded = json.loads(path.read_text())
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must contain a JSON object")
        unknown = set(loaded) - set(DEFAULT_SETTINGS)
        if unknown:
            raise ValueError(f"{path}: unknown settings {sorted(unknown)}")
        settings.update(loaded)
    chords = settings["chords"]
    if not isinstance(chords, list) or not chords or not all(_is_chord(c) for c in chords):
        raise ValueError("settings.chords must be a non-empty list of Hyprland chords")
    if not isinstance(settings["translate_key"], str) or not re.fullmatch(
        r"[A-Za-z0-9_]+", settings["translate_key"]
    ):
        raise ValueError("settings.translate_key must be an XKB key name such as Shift_R")
    timeout = settings["post_process_timeout_ms"]
    if not isinstance(timeout, int) or not 1000 <= timeout <= 120000:
        raise ValueError(
            "settings.post_process_timeout_ms must be an integer between 1000 and 120000"
        )
    polish = settings["polish_chord"]
    if polish is not None and not _is_chord(polish):
        raise ValueError("settings.polish_chord must be a Hyprland chord or null")
    base_url = settings["base_url"]
    url = urllib.parse.urlsplit(base_url) if isinstance(base_url, str) else None
    if not url or url.scheme not in {"https", "http"} or not url.hostname or url.query:
        raise ValueError(
            "settings.base_url must be an http(s) URL such as https://api.openai.com/v1"
        )
    # Dictated text must not cross the network in the clear.
    if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("settings.base_url may use plain http only for localhost")
    if not isinstance(settings["model"], str) or not re.fullmatch(r"\S+", settings["model"]):
        raise ValueError("settings.model must be a non-empty model name")
    effort = settings["reasoning_effort"]
    if effort is not None and not (isinstance(effort, str) and re.fullmatch(r"[a-z]+", effort)):
        raise ValueError("settings.reasoning_effort must be a word such as none or low, or null")
    return settings


def api_host(settings):
    return urllib.parse.urlsplit(settings["base_url"]).netloc


def _is_chord(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]+( *\+ *[A-Za-z0-9_]+)*", value)


# ----------------------------------------------------------------- managed blocks
def toml_block(bin_dir, timeout_ms):
    lines = [TOML_BEGIN, f"# Managed by the {ID} Omarchy plugin; edit settings.json instead."]
    for profile in PROFILES:
        lines += [
            f"[profiles.{profile}]",
            f'post_process_command = "{bin_dir / COMMANDS[profile]}"',
            f"post_process_timeout_ms = {timeout_ms}",
        ]
    lines.append(TOML_END)
    return "\n" + "\n".join(lines) + "\n"


def lua_block(settings, bin_dir):
    chords = ", ".join(json.dumps(chord) for chord in settings["chords"])
    translate_key = json.dumps(settings["translate_key"])
    polish = ""
    if settings["polish_chord"]:
        chord, wrapper = json.dumps(settings["polish_chord"]), json.dumps(str(bin_dir / WRAPPER))
        polish = f"""
-- Copy a draft, press {settings["polish_chord"]}: the clipboard text is rewritten as
-- native English (Markdown structure kept), then paste it.
o.bind({chord}, "Polish clipboard text into English", {wrapper})
"""
    return f"""
{LUA_BEGIN}
-- Managed by the {ID} Omarchy plugin; edit settings.json instead.
-- Hold a chord to dictate; the transcript is cleaned up in the language spoken.
-- Hold {settings["translate_key"]} as well to render it as English instead.
local dictation_recording = false
for _, chord in ipairs({{ {chords} }}) do
  hl.bind(chord, function()
    if dictation_recording then
      return
    end
    dictation_recording = true
    if hl.is_key_down({translate_key}) then
      hl.exec_cmd("voxtype record start --profile translate")
    else
      hl.exec_cmd("voxtype record start --profile rephrase")
    end
  end, {{ description = "Dictate while held ({settings["translate_key"]}: translate to English)" }})
  hl.bind(chord, function()
    if dictation_recording then
      dictation_recording = false
      hl.exec_cmd("voxtype record stop")
    end
  end, {{ release = true, transparent = true }})
end{polish}
{LUA_END}
"""


def edit_block(original, block, begin, end, remove=False):
    """Own only the region between our markers; never rewrite anything outside it.

    Installing appends the block, or replaces an existing block so settings changes
    take effect. Removing deletes the block and the blank line that preceded it.
    """
    begins, ends = original.count(begin), original.count(end)
    if begins == 0 and ends == 0:
        return original if remove else original + block
    if begins != 1 or ends != 1 or original.index(begin) > original.index(end):
        raise ValueError(
            f"The managed {ID} markers are duplicated or out of order; fix them first."
        )
    start = original.index(begin)
    stop = original.index(end) + len(end)
    if start > 0 and original[start - 1] == "\n":
        start -= 1
    if stop < len(original) and original[stop] == "\n":
        stop += 1
    return original[:start] + ("" if remove else block) + original[stop:]


def check_conflicts(voxtype_original, bindings_original, settings):
    """Refuse to shadow profiles or chords the user configured by hand."""
    for profile in PROFILES:
        if re.search(rf"^\s*\[profiles\.{profile}\]", voxtype_original, re.MULTILINE):
            raise ValueError(
                f"[profiles.{profile}] already exists in the Voxtype config; remove it first."
            )
    for chord in (*settings["chords"], settings["polish_chord"]):
        if chord and chord in bindings_original:
            raise ValueError(f"The chord {chord!r} is already bound in your Hyprland bindings.")


# ----------------------------------------------------------------- files
def atomic_write(path, content, mode=None):
    """Replace one file without exposing a partially written configuration."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(content)
        if mode is not None:
            temporary.chmod(mode)
        elif path.exists():
            shutil.copymode(path, temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def install_files(source, paths):
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    for name in (SCRIPT, WRAPPER):
        target = paths.bin_dir / name
        shutil.copyfile(source / name, target)
        target.chmod(0o755)
    for command in COMMANDS.values():
        link = paths.bin_dir / command
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(SCRIPT)
    if not paths.vocabulary.exists():
        paths.vocabulary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / "vocabulary.example.txt", paths.vocabulary)


def remove_files(paths):
    for name in (SCRIPT, WRAPPER, *COMMANDS.values()):
        (paths.bin_dir / name).unlink(missing_ok=True)


def ensure_api_key(path, host, prompt=getpass.getpass):
    if path.exists() and path.read_text().strip():
        return
    key = prompt(f"API key for {host} (input hidden, stored in {path}): ").strip()
    if not key:
        raise ValueError(
            f"An API key is required for {host}; for a server without keys enter any text."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    atomic_write(path, key + "\n", mode=0o600)


# ----------------------------------------------------------------- system checks
def preflight(paths):
    if os.geteuid() == 0:
        raise RuntimeError("Run as your desktop user, not root or sudo.")
    for tool in (
        "voxtype",
        "curl",
        "jq",
        "hyprctl",
        "systemctl",
        "wl-paste",
        "wl-copy",
        "notify-send",
    ):
        if shutil.which(tool) is None:
            raise RuntimeError(f"Required command not found: {tool}")
    version = re.search(r"(\d+)\.(\d+)", run("voxtype", "--version"))
    if version is None or tuple(int(part) for part in version.groups()) < MIN_VOXTYPE:
        raise RuntimeError("Voxtype %d.%d or later is required." % MIN_VOXTYPE)
    for path in (paths.voxtype_config, paths.bindings):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Expected a regular, non-symlink file: {path}")


def check_errors():
    errors = run("hyprctl", "configerrors").strip()
    if errors:
        raise RuntimeError(f"Hyprland reports configuration errors:\n{errors}")


def restart_services():
    run("systemctl", "--user", "restart", "voxtype.service")
    run("hyprctl", "reload", "config-only")
    check_errors()


def verify_profiles(timeout_s=30):
    """Ask Voxtype which profiles it knows.

    The CLI answers from the daemon, which takes a few seconds to come back after a
    restart and reports "daemon is not running" meanwhile, so keep probing until it
    lists profiles, says there are none, or the time is up.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        probe = subprocess.run(
            ["voxtype", "record", "start", "--profile", f"__{ID}_probe__"],
            capture_output=True,
            text=True,
        )
        output = probe.stderr + probe.stdout
        listed = re.search(r"Available profiles:\s*(.*)", output)
        if listed or "No profiles are configured" in output or time.monotonic() >= deadline:
            break
        time.sleep(1)
    available = {name.strip() for name in listed.group(1).split(",")} if listed else set()
    missing = set(PROFILES) - available
    if missing:
        detail = output.strip().splitlines()[0] if output.strip() else "no output"
        raise RuntimeError(
            f"Voxtype does not list the profiles {sorted(missing)} after restart ({detail})."
        )


# ----------------------------------------------------------------- apply
def apply(source, paths, remove=False, prompt=getpass.getpass):
    settings = load_settings(paths.settings)
    voxtype_original = paths.voxtype_config.read_text()
    bindings_original = paths.bindings.read_text()
    toml = toml_block(paths.bin_dir, settings["post_process_timeout_ms"])
    lua = lua_block(settings, paths.bin_dir)
    if not remove and TOML_BEGIN not in voxtype_original and LUA_BEGIN not in bindings_original:
        check_conflicts(voxtype_original, bindings_original, settings)
    voxtype_updated = edit_block(voxtype_original, toml, TOML_BEGIN, TOML_END, remove)
    bindings_updated = edit_block(bindings_original, lua, LUA_BEGIN, LUA_END, remove)

    paths.state.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="backup-", dir=paths.state))
    shutil.copy2(paths.voxtype_config, backup / paths.voxtype_config.name)
    shutil.copy2(paths.bindings, backup / paths.bindings.name)
    print(f"Backup: {backup}", flush=True)
    marker = paths.state / "installed.json"

    if not remove:
        ensure_api_key(paths.api_key, api_host(settings), prompt)
    try:
        if remove:
            remove_files(paths)
        else:
            install_files(source, paths)
        atomic_write(paths.voxtype_config, voxtype_updated)
        atomic_write(paths.bindings, bindings_updated)
        restart_services()
        if remove:
            marker.unlink(missing_ok=True)
        else:
            verify_profiles()
            atomic_write(
                marker,
                json.dumps({"version": VERSION, "backup": str(backup), "settings": settings})
                + "\n",
            )
    except BaseException as error:
        try:
            atomic_write(paths.voxtype_config, voxtype_original)
            atomic_write(paths.bindings, bindings_original)
            if not remove:
                remove_files(paths)
            restart_services()
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as recovery_error:
            raise RuntimeError(
                f"Setup failed ({error}); recovery failed ({recovery_error}). "
                f"Restore {backup} manually."
            ) from error
        raise


# ----------------------------------------------------------------- entry point
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--launch", action="store_true", help="Open first-run confirmation in a terminal"
    )
    group.add_argument(
        "--uninstall", action="store_true", help="Remove the managed blocks, commands and profiles"
    )
    args = parser.parse_args()
    paths = Paths.default()
    source = Path(__file__).resolve().parent
    if args.launch:
        if not (paths.state / "installed.json").exists():
            command = f"/usr/bin/python {shlex.quote(str(source / 'install.py'))}"
            run("omarchy-launch-floating-terminal-with-presentation", command)
        return
    preflight(paths)
    paths.state.mkdir(parents=True, exist_ok=True)
    with (paths.state / "install.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Dictation setup is already running.")
            return
        settings = load_settings(paths.settings)
        if args.uninstall:
            print(
                "Remove the dictation key bindings, the Voxtype profiles and the cleanup\n"
                "commands, then restart Voxtype and reload Hyprland?\n"
                "Your API key and vocabulary file are kept."
            )
        else:
            print(
                "Set up hold-to-dictate with LLM cleanup?\n"
                f"  Hold {' or '.join(settings['chords'])} to dictate; the text is cleaned up\n"
                f"  in the language you spoke. Hold {settings['translate_key']} too for English.\n"
                + (
                    f"  Press {settings['polish_chord']} to rewrite clipboard text as English.\n"
                    if settings["polish_chord"]
                    else ""
                )
                + f"  Dictated and clipboard text is sent to {api_host(settings)}\n"
                f"  ({settings['model']}), billed to your account there.\n"
                f"  Managed blocks are appended to {paths.voxtype_config} and {paths.bindings};\n"
                "  both files are backed up first. Voxtype is restarted and Hyprland reloaded.\n"
                f"  Change keys or timeouts in {paths.settings} and re-run this installer;\n"
                "  the API provider and model there take effect immediately."
            )
        if input("Continue? [y/N] ").strip().lower() not in {"y", "yes"}:
            return
        apply(source, paths, remove=args.uninstall)
        print("Removed." if args.uninstall else "Ready. Hold the key and speak.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Dictation setup failed: {error}") from error
