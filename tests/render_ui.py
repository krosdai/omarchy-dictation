"""Render real QML in an isolated Wayland session, never the user's desktop.

Pass a running headless compositor's private XDG_RUNTIME_DIR and WAYLAND_DISPLAY.
Usage: python tests/render_ui.py --artifacts /path/to/artifacts
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def production(env, artifacts):
    """Actual service/installer/backend with fake PCM and private configuration."""
    with tempfile.TemporaryDirectory(prefix="voice-service-") as directory:
        preview = Path(directory)
        shutil.copyfile(ROOT / "tests/service-preview.qml", preview / "shell.qml")
        for name in ("Service.qml", "Panel.qml", "BarWidget.qml", "voice.py", "install.py"):
            shutil.copyfile(ROOT / name, preview / name)
        for name in ("Commons", "Ui"):
            (preview / name).symlink_to(Path("/usr/share/omarchy/shell") / name)
        (preview / "ui").symlink_to(ROOT / "ui")
        config = preview / "config/omarchy-dictation"
        state = preview / "state/omarchy-dictation"
        runtime = preview / "data/omarchy-dictation"
        tools = preview / "tools"
        for path in (config, state, runtime, tools):
            path.mkdir(parents=True)
        (runtime / "venv").symlink_to(Path(sys.prefix))
        for name in ("api_key", "elevenlabs_api_key"):
            (config / name).write_text("fake-never-uploaded\n")
            (config / name).chmod(0o600)
        (config / "settings.json").write_text('{"mode":"translate"}')
        nodes = [
            {
                "id": 9,
                "info": {
                    "props": {
                        "media.class": "Audio/Source",
                        "node.name": "fake-mic",
                        "node.description": "Fixture microphone · no real audio",
                    }
                },
            }
        ]
        scripts = {
            "pw-dump": f"#!/bin/sh\nprintf '%s' '{json.dumps(nodes)}'\n",
            "wpctl": "#!/bin/sh\nprintf 'Volume: 1.00'\n",
            "pw-record": (
                f"#!{sys.executable}\nimport os,time,pathlib,struct\n"
                f"pathlib.Path({str(preview / 'recorder-pid')!r}).write_text(str(os.getpid()))\n"
                "while True:\n"
                " os.write(1,b''.join(struct.pack('<h',150 + (i//40*7%19)*180)"
                " for i in range(1600)))\n"
                " time.sleep(.1)\n"
            ),
            "omarchy-launch-floating-terminal-with-presentation": (
                f"#!/bin/sh\ntouch '{preview / 'unexpected-terminal'}'\n"
            ),
        }
        for name in ("wtype", "wl-copy", "wl-paste", "curl", "jq", "notify-send"):
            scripts[name] = "#!/bin/sh\nexit 0\n"
        for name, script in scripts.items():
            (tools / name).write_text(script)
            (tools / name).chmod(0o755)
        env = env | {
            "XDG_CONFIG_HOME": str(preview / "config"),
            "XDG_STATE_HOME": str(preview / "state"),
            "XDG_DATA_HOME": str(preview / "data"),
            "PATH": str(tools) + os.pathsep + env["PATH"],
            "ELEVENLABS_API_KEY": "",
            "HYPRLAND_INSTANCE_SIGNATURE": "",
        }
        log_path = artifacts / "service-render.log"
        with log_path.open("w") as log:
            process = subprocess.Popen(
                ["quickshell", "-p", str(preview), "--no-color"],
                env=env,
                stdout=log,
                stderr=log,
            )

            def ipc(target, method, *values):
                output = subprocess.run(
                    ["quickshell", "ipc", "-p", str(preview), "call", target, method, *values],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                ).stdout.strip()
                return output

            def wait_phase(phase):
                for _ in range(50):
                    try:
                        report = json.loads(ipc("dictation", "status"))
                        if report["phase"] == phase and report["connected"]:
                            return
                    except (subprocess.CalledProcessError, ValueError):
                        pass
                    if process.poll() is not None:
                        raise RuntimeError("Production service failed; inspect service-render.log")
                    time.sleep(0.1)
                raise AssertionError(f"Production service never reached {phase}")

            try:
                # First-run setup is stubbed, not a real terminal/desktop mutation.
                for _ in range(50):
                    if (preview / "unexpected-terminal").exists():
                        break
                    if process.poll() is not None:
                        raise RuntimeError("Production service failed; inspect service-render.log")
                    time.sleep(0.1)
                assert (preview / "unexpected-terminal").exists()
                (preview / "unexpected-terminal").unlink()
                time.sleep(0.3)
                report = json.loads(ipc("dictation", "status"))
                assert report["phase"] == "error" and not report["connected"]
                # Completing setup must revive the backend without a shell restart.
                (state / "installed.json").write_text('{"version":"0.2.0"}')
                wait_phase("idle")
                ipc("dictation", "cancel")
                wait_phase("idle")
                ipc("integration", "panel")
                assert json.loads(ipc("integration", "report"))["widgetOpen"]
                time.sleep(0.3)
                subprocess.run(
                    ["grim", str(artifacts / "voice-production-settings.png")], env=env, check=True
                )
                subprocess.run(["/usr/bin/wtype", "-s", "200", "-k", "Escape"], env=env, check=True)
                time.sleep(0.1)
                assert not json.loads(ipc("integration", "report"))["panelOpen"]
                ipc("integration", "widget")
                assert json.loads(ipc("integration", "report"))["panelOpen"]
                ipc("dictation", "close")
                ipc("integration", "configure", '{"mode":"rephrase","model":"fixture-model"}')
                for _ in range(50):
                    saved = json.loads((config / "settings.json").read_text())
                    if saved.get("model") == "fixture-model":
                        break
                    time.sleep(0.1)
                assert saved["mode"] == "rephrase" and saved["model"] == "fixture-model"
                ipc("integration", "test")
                wait_phase("testing")
                time.sleep(0.5)
                subprocess.run(
                    ["grim", str(artifacts / "voice-production-microphone.png")],
                    env=env,
                    check=True,
                )
                pid = int((preview / "recorder-pid").read_text())
                ipc("integration", "quit")
                process.wait(timeout=5)
                for _ in range(50):
                    if not Path(f"/proc/{pid}").exists():
                        break
                    time.sleep(0.1)
                assert not Path(f"/proc/{pid}").exists(), "Unloading must stop the recorder"
                assert not (preview / "unexpected-terminal").exists()
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
        log = log_path.read_text()
        for error in ("ERROR", "TypeError", "ReferenceError", "Binding loop", "Unable to assign"):
            assert error not in log, f"Production QML error: {error}"
    print("Production service, own-service panel/widget, settings, PCM and shutdown passed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "QT_QPA_PLATFORM": "wayland", "QT_QUICK_BACKEND": "software"}
    env.pop("WAYLAND_DEBUG", None)
    with tempfile.TemporaryDirectory(prefix="voice-ui-") as directory:
        preview = Path(directory)
        shutil.copyfile(ROOT / "tests/preview.qml", preview / "shell.qml")
        for name in ("Commons", "Ui"):
            (preview / name).symlink_to(Path("/usr/share/omarchy/shell") / name)
        (preview / "ui").symlink_to(ROOT / "ui")
        with (args.artifacts / "ui-render.log").open("w") as log:
            process = subprocess.Popen(
                ["quickshell", "-p", str(preview), "--no-color"],
                env={**env, "WAYLAND_DEBUG": "1"},
                stdout=log,
                stderr=log,
            )

            def ipc(method, *values):
                return subprocess.run(
                    ["quickshell", "ipc", "-p", str(preview), "call", "preview", method, *values],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                ).stdout

            def event(value):
                ipc("event", json.dumps(value, ensure_ascii=False))

            def capture(name):
                time.sleep(0.25)
                subprocess.run(
                    ["grim", str(args.artifacts / f"{name}.png")], env=env, check=True, timeout=5
                )

            try:
                for _ in range(50):
                    if process.poll() is not None:
                        raise RuntimeError("QML preview failed; inspect ui-render.log")
                    try:
                        ipc("report")
                        break
                    except subprocess.CalledProcessError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("QML preview did not start")
                for phase in (
                    "connecting",
                    "listening",
                    "finishing",
                    "processing",
                    "done",
                    "clipboard",
                    "cancelled",
                    "error",
                    "idle",
                ):
                    text = "让语音输入更自然。 Keep the words in the language I speak."
                    message = (
                        "Microphone muted. Unmute it yourself, then try again."
                        if phase == "error"
                        else ""
                    )
                    event(
                        {
                            "type": "state",
                            "phase": phase,
                            "text": text if phase != "idle" else "",
                            "message": message,
                        }
                    )
                    if phase == "listening":
                        event(
                            {
                                "type": "levels",
                                "values": [0.008 + (i * 7 % 19) / 250 for i in range(40)],
                            }
                        )
                    report = json.loads(ipc("report"))
                    assert report["phase"] == phase, report
                    capture(f"voice-{phase}")
                long_text = (
                    "中文和 English 混合输入。 " * 100 + "最后一句必须完整可见。 END OF TRANSCRIPT."
                )
                event(
                    {"type": "state", "phase": "listening", "mode": "translate", "text": long_text}
                )
                assert json.loads(ipc("report"))["text"] == long_text
                capture("voice-long")
                event(
                    {"type": "preview", "text": "A revised hypothesis replaces the previous text."}
                )
                assert (
                    json.loads(ipc("report"))["text"]
                    == "A revised hypothesis replaces the previous text."
                )
                capture("voice-revised")
                event({"type": "state", "phase": "idle"})
                ipc("settings")
                assert json.loads(ipc("report"))["settingsOpen"]
                capture("voice-settings")
                subprocess.run(["wtype", "-s", "200", "-k", "Escape"], env=env, check=True)
                time.sleep(0.1)
                assert not json.loads(ipc("report"))["settingsOpen"], "Escape must close settings"
                event({"type": "state", "phase": "testing"})
                capture("voice-testing")
                event({"type": "state", "phase": "idle"})
                event({"type": "settings", "asr_configured": False, "llm_configured": False})
                ipc("settings")
                capture("voice-unconfigured")
                ipc("quit")
                process.wait(timeout=5)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
    log = (args.artifacts / "ui-render.log").read_text()
    assert "set_keyboard_interactivity(0)" in log
    assert "set_input_region" in log
    for error in ("ERROR", "TypeError", "ReferenceError", "Binding loop", "Unable to assign"):
        assert error not in log, f"QML error: {error}"
    print("UI states, preview replacement, Escape, and Wayland input policies passed.")
    production(env, args.artifacts)


if __name__ == "__main__":
    main()
