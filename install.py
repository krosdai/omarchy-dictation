#!/usr/bin/python
"""Install or remove standalone Omarchy dictation for the current desktop user."""

import argparse
import fcntl
import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

ID = "krosdai.dictation"
VERSION = "0.2.0"
SCRIPT = "dictation-llm"
WRAPPER = "polish-clipboard"
LAUNCHER = "omarchy-dictation"
COMMANDS = (LAUNCHER, SCRIPT, WRAPPER)
# Exact copied payloads from the v0.1.0 installer at 610ae09; names alone
# cannot establish ownership. That installer used relative voxtype-llm symlinks.
LEGACY_HASHES = {
    "voxtype-llm": "a7690de1c4dbdf170e0d1139c2dd0f9878c49abfb81390da655caa88696789c3",
    WRAPPER: "cfa35eaa42c14686314051eec06bd789a7328d0be7ec660518fadc3ec485e66b",
}
LEGACY_LINKS = ("voxtype-rephrase", "voxtype-translate-en")
REQUIRED_TOOLS = (
    "pw-record",
    "wpctl",
    "pw-dump",
    "wtype",
    "wl-copy",
    "wl-paste",
    "curl",
    "jq",
    "notify-send",
)

TOML_BEGIN = f"# BEGIN {ID}"
TOML_END = f"# END {ID}"
LUA_BEGIN = f"-- BEGIN {ID}"
LUA_END = f"-- END {ID}"

DEFAULT_SETTINGS = {
    # Hyprland chords that start a recording while held. The Copilot key on many
    # laptops reports Shift + Meta + F23; with Omarchy's default layout the left
    # Meta key is exposed as Alt, so both spellings are bound.
    "chords": ["F23", "SHIFT + F23", "ALT + SHIFT + F23", "SUPER + SHIFT + F23"],
    # Extra key that, when held together with a chord, selects the translate profile.
    "translate_key": "Shift_R",
    # Maximum cleanup time before falling back to the raw transcript.
    "post_process_timeout_ms": 20000,
    # Chord that rewrites the clipboard text as native English; null disables it.
    "polish_chord": "SUPER + SHIFT + T",
    # Any OpenAI-compatible Chat Completions API; dictation-llm reads these three on
    # every call and keeps its own copy of the defaults.
    "base_url": "https://api.cerebras.ai/v1",
    "model": "qwen-3.8-27b",
    # Sent as reasoning_effort; null omits it for models that do not accept it.
    "reasoning_effort": "none",
    "mode": "rephrase",
    "microphone": "auto",
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
    elevenlabs_api_key: Path
    runtime: Path

    @property
    def legacy_vocabulary(self):
        return self.voxtype_config.parent / "vocabulary.txt"

    @classmethod
    def default(cls):
        config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        return cls(
            voxtype_config=config / "voxtype/config.toml",
            bindings=config / "hypr/bindings.lua",
            settings=config / "omarchy-dictation/settings.json",
            vocabulary=config / "omarchy-dictation/vocabulary.txt",
            api_key=config / "omarchy-dictation/api_key",
            bin_dir=Path.home() / ".local/bin",
            state=state / "omarchy-dictation",
            elevenlabs_api_key=config / "omarchy-dictation/elevenlabs_api_key",
            runtime=data / "omarchy-dictation/venv/bin/python",
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
    if type(timeout) is not int or not 1000 <= timeout <= 120000:
        raise ValueError(
            "settings.post_process_timeout_ms must be an integer between 1000 and 120000"
        )
    polish = settings["polish_chord"]
    if polish is not None and not _is_chord(polish):
        raise ValueError("settings.polish_chord must be a Hyprland chord or null")
    base_url = settings["base_url"]
    try:
        url = urllib.parse.urlsplit(base_url) if isinstance(base_url, str) else None
        url and url.port  # raises ValueError for a malformed or out-of-range port
    except ValueError:
        url = None
    # Mirror dictation-llm's runtime check: no user info, query or fragment.
    if (
        not url
        or url.scheme not in {"https", "http"}
        or not url.hostname
        or "@" in url.netloc
        or re.search(r"[\s?#\\\x00-\x1f\x7f]", base_url)
    ):
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
    if settings["mode"] not in ("rephrase", "translate"):
        raise ValueError("settings.mode must be rephrase or translate")
    microphone = settings["microphone"]
    if not isinstance(microphone, str) or not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,255}", microphone
    ):
        raise ValueError("settings.microphone must be auto, a PipeWire node name or serial")
    return settings


def api_host(settings):
    return urllib.parse.urlsplit(settings["base_url"]).netloc


def _is_chord(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]+( *\+ *[A-Za-z0-9_]+)*", value)


# ----------------------------------------------------------------- managed blocks
def lua_block(settings, bin_dir):
    chords = ", ".join(json.dumps(chord) for chord in settings["chords"])
    translate_key = json.dumps(settings["translate_key"])
    command = shlex.quote(str(bin_dir / LAUNCHER))
    start = json.dumps(command + " start")
    translate = json.dumps(command + " start --mode translate")
    stop = json.dumps(command + " stop")
    cancel = json.dumps(command + " cancel")
    release_keys = ", ".join(
        json.dumps(key)
        for key in dict.fromkeys(c.split("+")[-1].strip() for c in settings["chords"])
    )
    polish = ""
    if settings["polish_chord"]:
        chord = json.dumps(settings["polish_chord"])
        wrapper = json.dumps(shlex.quote(str(bin_dir / WRAPPER)))
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
local dictation_key, dictation_operation
local dictation_sequence = 0
local dictation_uuid = assert(io.open("/proc/sys/kernel/random/uuid", "r"))
local dictation_session = dictation_uuid:read("*l")
dictation_uuid:close()
for _, chord in ipairs({{ {chords} }}) do
  local physical_key = chord:match("([^+ ]+)$")
  hl.bind(chord, function()
    if dictation_key then return end
    dictation_sequence = dictation_sequence + 1
    dictation_key = physical_key
    dictation_operation = dictation_session .. "-" .. tostring(dictation_sequence)
    if hl.is_key_down({translate_key}) then
      hl.exec_cmd({translate} .. " --operation " .. dictation_operation)
    else
      hl.exec_cmd({start} .. " --operation " .. dictation_operation)
    end
  end, {{ description = "Dictate while held ({settings["translate_key"]}: translate to English)" }})
end
-- Release by physical key with any modifiers: modifiers can change while held.
for _, key in ipairs({{ {release_keys} }}) do
  hl.bind(key, function()
    if dictation_key ~= key then return end
    local operation = dictation_operation
    dictation_key, dictation_operation = nil, nil
    hl.exec_cmd({stop} .. " --operation " .. operation)
  end,
    {{ release = true, transparent = true, ignore_mods = true }})
end
-- No native predicate binding is required: cancel is a backend no-op when idle.
-- Non-consuming fallback always preserves the application's Escape handling.
hl.bind("Escape", function() hl.exec_cmd({cancel}) end,
  {{ transparent = true, non_consuming = true }})
{polish}
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


def check_conflicts(bindings_original, settings):
    """Refuse to shadow chords the user configured by hand."""
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
        with os.fdopen(descriptor, "wb" if isinstance(content, bytes) else "w") as output:
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
        atomic_write(
            paths.bin_dir / name,
            "#!/bin/sh\nexec /usr/bin/bash " + shlex.quote(str(source / name)) + ' "$@"\n',
            0o755,
        )
    atomic_write(
        paths.bin_dir / LAUNCHER,
        "#!/bin/sh\nexec "
        + shlex.quote(str(paths.runtime))
        + " "
        + shlex.quote(str(source / "voice.py"))
        + ' "$@"\n',
        0o755,
    )
    if not paths.settings.exists():
        atomic_write(paths.settings, json.dumps(DEFAULT_SETTINGS, indent=2) + "\n", 0o600)
    if not paths.vocabulary.exists():
        vocabulary = paths.legacy_vocabulary
        if not vocabulary.is_file():
            vocabulary = source / "vocabulary.example.txt"
        atomic_write(paths.vocabulary, vocabulary.read_text(), 0o600)


def owned_commands(paths, marker):
    """Recognize intact recorded launchers and exact historical copied files."""
    owned = set()
    commands = marker.get("commands", {})
    if not isinstance(commands, dict):
        commands = {}
    for name, installed in commands.items():
        if name not in COMMANDS:
            continue
        path = paths.bin_dir / name
        if (
            isinstance(installed, str)
            and not path.is_symlink()
            and path.is_file()
            and path.read_bytes() == installed.encode()
        ):
            owned.add(name)
    for name, digest in LEGACY_HASHES.items():
        path = paths.bin_dir / name
        if (
            not path.is_symlink()
            and path.is_file()
            and hashlib.sha256(path.read_bytes()).hexdigest() == digest
        ):
            owned.add(name)
    if "voxtype-llm" in owned:
        for name in LEGACY_LINKS:
            path = paths.bin_dir / name
            if path.is_symlink() and os.readlink(path) == "voxtype-llm":
                owned.add(name)
    return owned


def check_command_collisions(paths):
    owned = owned_commands(paths, read_marker(paths))
    for name in COMMANDS:
        path = paths.bin_dir / name
        if (path.exists() or path.is_symlink()) and name not in owned:
            raise ValueError(f"Refusing to overwrite unowned command: {path}")
    return owned


def remove_files(paths, marker):
    for name in owned_commands(paths, marker):
        (paths.bin_dir / name).unlink()


def ensure_api_key(path, host, prompt=getpass.getpass, replace=False):
    if path.is_symlink():
        raise ValueError("Private credential files must not be symlinks")
    if not replace and path.exists() and path.read_text().strip():
        path.parent.chmod(0o700)
        path.chmod(0o600)
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
def preflight(paths, remove=False):
    if os.geteuid() == 0:
        raise RuntimeError("Run as your desktop user, not root or sudo.")
    tools = ("hyprctl", "omarchy-version")
    if not remove:
        tools += REQUIRED_TOOLS
    for tool in tools:
        if shutil.which(tool) is None:
            raise RuntimeError(f"Required command not found: {tool}")
    if not os.access("/usr/bin/python", os.X_OK):
        raise RuntimeError("Required interpreter not found: /usr/bin/python")
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        raise RuntimeError("Run setup in your Omarchy Hyprland desktop session.")
    if paths.bindings.is_symlink() or not paths.bindings.is_file():
        raise ValueError(f"Expected a regular, non-symlink file: {paths.bindings}")


def check_errors():
    errors = run("hyprctl", "configerrors").strip()
    if errors:
        raise RuntimeError(f"Hyprland reports configuration errors:\n{errors}")


def restart_services():
    run("hyprctl", "reload", "config-only")
    check_errors()


def provision_runtime(paths):
    if not paths.runtime.exists():
        run("/usr/bin/python", "-m", "venv", str(paths.runtime.parent.parent))
    run(str(paths.runtime), "-m", "pip", "install", "websockets==15.0.1")


def read_marker(paths):
    try:
        value = json.loads((paths.state / "installed.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def runtime_error(source, paths):
    """Offline readiness check; never install dependencies or reveal secrets."""
    if read_marker(paths).get("version") != VERSION:
        return "Setup required; legacy installations must be upgraded in a terminal."
    if not paths.runtime.is_file() or not os.access(paths.runtime, os.X_OK):
        return "Private Python runtime is missing; run setup again."
    if not (source / "voice.py").is_file():
        return "Standalone backend is missing from the plugin checkout."
    for tool in REQUIRED_TOOLS:
        if shutil.which(tool) is None:
            return f"Required command not found: {tool}"
    try:
        load_settings(paths.settings)
        for key in (paths.api_key, paths.elevenlabs_api_key):
            if not key.read_text().strip():
                return "Credentials missing; open credential setup in a terminal."
        run(
            str(paths.runtime), "-c", "import websockets; assert websockets.__version__ == '15.0.1'"
        )
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError):
        return "Settings, credentials or runtime dependencies need setup in a terminal."
    return None


def run_daemon(source, paths):
    error = runtime_error(source, paths)
    if not error:
        try:
            # Quickshell kills its direct child on QML destruction. Keep a
            # wait-only parent so the backend receives stdin EOF instead and
            # can reap capture or finish bounded irreversible text delivery.
            process = subprocess.Popen(
                [str(paths.runtime), str(source / "voice.py"), "daemon", "--stdio"],
                start_new_session=True,
            )
            process.wait()
            return
        except OSError:
            error = "Cannot launch the private runtime; run setup again."
    if error:
        print(
            json.dumps(
                {
                    "type": "state",
                    "phase": "error",
                    "mode": "rephrase",
                    "text": "",
                    "message": error,
                }
            ),
            flush=True,
        )


# ----------------------------------------------------------------- apply
def apply(source, paths, remove=False, prompt=getpass.getpass):
    # Uninstall must remain possible even with invalid user settings.
    previous_marker = read_marker(paths)
    owned = owned_commands(paths, previous_marker) if remove else check_command_collisions(paths)
    settings = dict(DEFAULT_SETTINGS) if remove else load_settings(paths.settings)
    source = source.resolve()
    bindings_original = paths.bindings.read_text()
    lua = lua_block(settings, paths.bin_dir)
    if not remove:
        personal = edit_block(bindings_original, "", LUA_BEGIN, LUA_END, remove=True)
        check_conflicts(personal, settings)
    bindings_updated = edit_block(bindings_original, lua, LUA_BEGIN, LUA_END, remove)
    legacy_original = None
    legacy_updated = None
    if paths.voxtype_config.exists():
        legacy_original = paths.voxtype_config.read_text()
        legacy_updated = edit_block(legacy_original, "", TOML_BEGIN, TOML_END, remove=True)
        if legacy_updated != legacy_original and paths.voxtype_config.is_symlink():
            raise ValueError("Refusing to edit a symlinked legacy config")
    if not remove:
        for name in ("voice.py", SCRIPT, WRAPPER, "vocabulary.example.txt"):
            if (source / name).is_symlink() or not (source / name).is_file():
                raise ValueError(f"Expected a regular source file: {source / name}")
        ensure_api_key(paths.api_key, api_host(settings), prompt)
        ensure_api_key(paths.elevenlabs_api_key, "ElevenLabs recognition", prompt)
        provision_runtime(paths)
    paths.state.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="backup-", dir=paths.state))
    shutil.copy2(paths.bindings, backup / paths.bindings.name)
    if legacy_original is not None:
        atomic_write(backup / "config.toml", legacy_original)
    print(f"Backup: {backup}", flush=True)
    marker = paths.state / "installed.json"
    # Save regular files and symlink targets, including previously owned commands.
    snapshots = {}
    for path in [
        *(paths.bin_dir / name for name in sorted(owned | (set() if remove else set(COMMANDS)))),
        marker,
        paths.settings,
        paths.vocabulary,
    ]:
        if path.is_symlink():
            snapshots[path] = ("symlink", os.readlink(path), None)
            shutil.copy2(path, backup / ("command-" + path.name), follow_symlinks=False)
        elif path.exists():
            if not path.is_file():
                raise ValueError(f"Expected a file, not a directory: {path}")
            snapshots[path] = ("file", path.read_bytes(), path.stat().st_mode & 0o777)
            shutil.copy2(path, backup / ("command-" + path.name))
        else:
            snapshots[path] = ("missing", None, None)
    try:
        remove_files(paths, previous_marker)
        if not remove:
            install_files(source, paths)
        if legacy_updated != legacy_original:
            atomic_write(paths.voxtype_config, legacy_updated)
        atomic_write(paths.bindings, bindings_updated)
        restart_services()
        if remove:
            marker.unlink(missing_ok=True)
        else:
            atomic_write(
                marker,
                json.dumps(
                    {
                        "version": VERSION,
                        "backup": str(backup),
                        "settings": settings,
                        "source": str(source),
                        "commands": {name: (paths.bin_dir / name).read_text() for name in COMMANDS},
                    }
                )
                + "\n",
            )
    except BaseException as error:
        try:
            if legacy_updated != legacy_original:
                atomic_write(paths.voxtype_config, legacy_original)
            atomic_write(paths.bindings, bindings_original)
            for path, (kind, content, mode) in snapshots.items():
                if kind == "file":
                    atomic_write(path, content, mode)
                elif kind == "symlink":
                    path.unlink(missing_ok=True)
                    path.symlink_to(content)
                else:
                    path.unlink(missing_ok=True)
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
        "--uninstall", action="store_true", help="Remove owned bindings and commands"
    )
    group.add_argument("--run-daemon", action="store_true", help="Run the configured backend")
    group.add_argument("--credentials", action="store_true", help="Update keys in a terminal")
    args = parser.parse_args()
    paths = Paths.default()
    source = Path(__file__).resolve().parent
    if args.run_daemon:
        run_daemon(source, paths)
        return
    if args.launch:
        if runtime_error(source, paths):
            command = f"/usr/bin/python {shlex.quote(str(source / 'install.py'))}"
            run("omarchy-launch-floating-terminal-with-presentation", command)
        return
    if args.credentials:
        if not os.isatty(0):
            command = f"/usr/bin/python {shlex.quote(str(source / 'install.py'))} --credentials"
            run("omarchy-launch-floating-terminal-with-presentation", command)
            return
        settings = load_settings(paths.settings)
        ensure_api_key(paths.api_key, api_host(settings), replace=True)
        ensure_api_key(paths.elevenlabs_api_key, "ElevenLabs recognition", replace=True)
        marker = paths.state / "installed.json"
        if marker.is_file() and not marker.is_symlink():
            atomic_write(marker, marker.read_bytes())
        print("Private credentials updated.")
        return
    if not args.uninstall:
        check_command_collisions(paths)
    preflight(paths, remove=args.uninstall)
    paths.state.mkdir(parents=True, exist_ok=True)
    with (paths.state / "install.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Dictation setup is already running.")
            return
        settings = dict(DEFAULT_SETTINGS) if args.uninstall else load_settings(paths.settings)
        if args.uninstall:
            print(
                "Remove owned dictation bindings and commands, then reload Hyprland?\n"
                "Your credentials, settings and vocabulary are kept."
            )
        else:
            print(
                "Set up standalone hold-to-dictate with LLM cleanup?\n"
                f"  Hold {' or '.join(settings['chords'])} to dictate; the text is cleaned up\n"
                f"  in the language you spoke. Hold {settings['translate_key']} too for English.\n"
                + (
                    f"  Press {settings['polish_chord']} to rewrite clipboard text as English.\n"
                    if settings["polish_chord"]
                    else ""
                )
                + "  Audio is sent to ElevenLabs for recognition.\n"
                + f"  Dictated and clipboard text is sent to {api_host(settings)}\n"
                f"  ({settings['model']}), billed to your account there.\n"
                f"  Managed bindings are added to {paths.bindings} after backup.\n"
                "  A private Python venv and websockets==15.0.1 are installed; no sudo.\n"
                "  Hyprland is reloaded and checked for configuration errors.\n"
                "  Legacy managed profiles are removed, but Voxtype is NOT stopped.\n"
                "  Manually remove conflicting legacy hotkeys or disable your old service\n"
                "  if it captures the same keys. Unrelated Voxtype settings are untouched.\n"
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
