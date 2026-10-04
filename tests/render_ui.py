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
SAVED_SETTINGS = {
    "mode": "translate",
    "microphone": "fake-mic",
    "base_url": "https://fixture.invalid/v1",
    "model": "saved-asymmetric-model",
    "reasoning_effort": "low",
}


def production(env, artifacts):
    """Actual service/installer/backend with fake PCM and private configuration."""
    with tempfile.TemporaryDirectory(prefix="voice-service-", dir=artifacts.parent) as directory:
        preview = Path(directory)
        shutil.copyfile(ROOT / "tests/service-preview.qml", preview / "shell.qml")
        for name in ("Service.qml", "Panel.qml", "BarWidget.qml", "voice.py", "install.py"):
            shutil.copyfile(ROOT / name, preview / name)
        # Override recognition only in the disposable copy. Installer spawning,
        # stdio EOF, output delivery, and QML Process destruction remain real.
        shutil.copyfile(ROOT / "voice.py", preview / "fixture_voice.py")
        (preview / "voice.py").write_text(
            "import os,pathlib,fixture_voice as voice\n"
            f"pathlib.Path({str(preview / 'backend-pids')!r}).write_text("
            "f'{os.getpid()} {os.getppid()}')\n"
            "original_run=voice.Backend.run\n"
            "original_execute=voice.execute\n"
            "async def execute(args,data=None,**kwargs):\n"
            " if args[0]=='wtype' and data: kwargs['timeout']=30\n"
            " return await original_execute(args,data,**kwargs)\n"
            "voice.execute=execute\n"
            "async def run(self,testing):\n"
            " if testing: return await original_run(self,testing)\n"
            " await self.output('Complete fixture text.', '')\n"
            "voice.Backend.run=run\n"
            "raise SystemExit(voice.main())\n"
        )
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
        (config / "settings.json").write_text(json.dumps(SAVED_SETTINGS))
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
        scripts["wtype"] = (
            f"#!{sys.executable}\nimport os,sys,time,pathlib\n"
            "text=sys.stdin.buffer.read()\n"
            "if text:\n"
            f" typed=pathlib.Path({str(preview / 'typed-text')!r})\n"
            " typed.write_bytes(text[:5])\n"
            f" pathlib.Path({str(preview / 'typing-pid')!r}).write_text(str(os.getpid()))\n"
            # Hold the singleton collision window until the new service has
            # actually observed it, not for a machine-speed-dependent delay.
            f" release=pathlib.Path({str(preview / 'finish-typing')!r})\n"
            " deadline=time.monotonic()+30\n"
            " while not release.exists():\n"
            "  if time.monotonic()>=deadline: raise SystemExit('Fixture gate timed out')\n"
            "  time.sleep(.01)\n"
            " typed.write_bytes(text)\n"
        )
        for name, script in scripts.items():
            (tools / name).write_text(script)
            (tools / name).chmod(0o755)
        env = env | {
            "XDG_CONFIG_HOME": str(preview / "config"),
            "XDG_STATE_HOME": str(preview / "state"),
            "XDG_DATA_HOME": str(preview / "data"),
            "PATH": str(tools) + os.pathsep + env["PATH"],
            # Environment keys win over files; keep the missing-credentials state real.
            **{name: "" for name in env if name.endswith("_API_KEY")},
            "ELEVENLABS_API_KEY": "",
            "DICTATION_LLM_API_KEY": "",
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
                # Include the backend's five-second automatic retry interval.
                for _ in range(100):
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

            def wait_exit(pids, socket_removed=True):
                for _ in range(50):
                    if all(not Path(f"/proc/{pid}").exists() for pid in pids):
                        break
                    time.sleep(0.1)
                assert all(not Path(f"/proc/{pid}").exists() for pid in pids), pids
                if socket_removed:
                    assert not (
                        Path(env["XDG_RUNTIME_DIR"]) / "omarchy-dictation/control.sock"
                    ).exists()

            typing_pids = []
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
                # A retry before any connection must preserve the setup error
                # and must not launch another setup terminal.
                time.sleep(5.5)
                assert not (preview / "unexpected-terminal").exists()
                ipc("integration", "panel")
                report = json.loads(ipc("integration", "report"))
                assert report["panelOpen"] and not report["canSave"]
                assert report["message"] == (
                    "Setup required; legacy installations must be upgraded in a terminal."
                ), report
                assert report["statusText"] == report["message"], report
                ipc("integration", "save")
                assert json.loads((config / "settings.json").read_text()) == SAVED_SETTINGS
                subprocess.run(
                    ["grim", str(artifacts / "voice-production-offline.png")], env=env, check=True
                )
                # An existing installation with missing keys is still offline.
                marker = state / "installed.json"
                marker_bytes = '{"version":"0.2.0","preserved":"fixture"}'
                marker.write_text(marker_bytes)
                time.sleep(0.5)
                assert not json.loads(ipc("dictation", "status"))["connected"]
                assert not json.loads(ipc("integration", "report"))["canSave"]
                subprocess.run(
                    ["grim", str(artifacts / "voice-production-missing-credentials.png")],
                    env=env,
                    check=True,
                )
                # The detached credentials launcher returns before either key
                # exists. Its exit alone must not be mistaken for recovery.
                ipc("integration", "credentials")
                for _ in range(50):
                    if (preview / "unexpected-terminal").exists():
                        break
                    time.sleep(0.1)
                assert (preview / "unexpected-terminal").exists()
                (preview / "unexpected-terminal").unlink()
                time.sleep(0.2)
                ipc("integration", "panel")
                assert not json.loads(ipc("integration", "report"))["canSave"]
                for _ in range(50):
                    if not json.loads(ipc("integration", "report"))["backendRunning"]:
                        break
                    time.sleep(0.1)
                assert not json.loads(ipc("integration", "report"))["backendRunning"]
                for name in ("api_key", "elevenlabs_api_key"):
                    (config / name).write_text("fake-never-uploaded\n")
                    (config / name).chmod(0o600)
                time.sleep(0.2)
                assert not json.loads(ipc("dictation", "status"))["connected"]
                # Simulate the installer's atomic, unchanged repair notification.
                replacement = state / "marker-replacement"
                replacement.write_text(marker_bytes)
                replacement.replace(marker)
                wait_phase("idle")
                assert marker.read_text() == marker_bytes
                report = json.loads(ipc("integration", "report"))
                assert report["panelOpen"] and report["canSave"]
                assert report["values"] == SAVED_SETTINGS, report
                time.sleep(0.25)
                subprocess.run(
                    ["grim", str(artifacts / "voice-production-recovered.png")], env=env, check=True
                )
                ipc("integration", "save")
                time.sleep(0.3)
                saved = json.loads((config / "settings.json").read_text())
                assert {key: saved[key] for key in SAVED_SETTINGS} == SAVED_SETTINGS
                subprocess.run(
                    ["grim", str(artifacts / "voice-production-saved-settings.png")],
                    env=env,
                    check=True,
                )
                # Edit the open form, then refresh unrelated backend settings.
                edited = SAVED_SETTINGS | {"model": "edited-model", "reasoning_effort": None}
                ipc("integration", "edit", json.dumps(edited))
                ipc("integration", "refresh")
                time.sleep(0.3)
                assert json.loads(ipc("integration", "report"))["values"] == edited
                ipc("integration", "save")
                for _ in range(50):
                    saved = json.loads((config / "settings.json").read_text())
                    if saved.get("model") == "edited-model":
                        break
                    time.sleep(0.1)
                assert {key: saved[key] for key in edited} == edited
                ipc("dictation", "cancel")
                wait_phase("idle")
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
                ipc("integration", "test")
                wait_phase("testing")
                time.sleep(0.5)
                subprocess.run(
                    ["grim", str(artifacts / "voice-production-microphone.png")],
                    env=env,
                    check=True,
                )
                pid = int((preview / "recorder-pid").read_text())
                capture_pids = [pid, *map(int, (preview / "backend-pids").read_text().split())]
                ipc("integration", "quit")
                process.wait(timeout=5)
                wait_exit(capture_pids)
                # Reload the actual service and destroy its QML during insertion.
                process = subprocess.Popen(
                    ["quickshell", "-p", str(preview), "--no-color"],
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                wait_phase("idle")
                ipc("integration", "start")
                for _ in range(100):
                    if (preview / "typing-pid").exists():
                        break
                    time.sleep(0.01)
                assert (preview / "typing-pid").exists(), "Text insertion never started"
                typing_pids = [
                    int((preview / "typing-pid").read_text()),
                    *map(int, (preview / "backend-pids").read_text().split()),
                ]
                assert (preview / "typed-text").read_bytes() == b"Compl"
                ipc("integration", "quit")
                process.wait(timeout=5)
                assert (preview / "typed-text").read_bytes() == b"Compl", (
                    "QML must finish destruction while insertion is still partial"
                )
                # Reload before the detached old daemon releases its singleton
                # lock. Do not touch the panel or installed marker to recover.
                process = subprocess.Popen(
                    ["quickshell", "-p", str(preview), "--no-color"],
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                for _ in range(50):
                    try:
                        report = json.loads(ipc("dictation", "status"))
                        if report["phase"] == "error" and not report["connected"]:
                            break
                    except (subprocess.CalledProcessError, ValueError):
                        pass
                    time.sleep(0.05)
                else:
                    raise AssertionError("Reload did not encounter the old daemon's lock")
                assert (preview / "typed-text").read_bytes() == b"Compl"
                assert not json.loads(ipc("integration", "report"))["backendRunning"]
                (preview / "finish-typing").touch()
                wait_exit(typing_pids, socket_removed=False)
                assert (preview / "typed-text").read_bytes() == b"Complete fixture text."
                wait_phase("idle")
                assert marker.read_text() == marker_bytes
                report = json.loads(ipc("integration", "report"))
                assert not report["panelOpen"] and report["backendRunning"], report
                recovered_pids = list(map(int, (preview / "backend-pids").read_text().split()))
                ipc("integration", "quit")
                process.wait(timeout=5)
                wait_exit(recovered_pids)
                (artifacts / "service-shutdown.json").write_text(
                    json.dumps(
                        {
                            "capture_pids_reaped": capture_pids,
                            "typing_pids_exited": typing_pids,
                            "retry_pids_reaped": recovered_pids,
                            "reload_initially_offline": True,
                            "automatic_retry_connected": True,
                            "marker_unchanged": marker.read_text() == marker_bytes,
                            "partial_at_unload": "Compl",
                            "final_text": (preview / "typed-text").read_text(),
                            "socket_removed": True,
                        },
                        indent=2,
                    )
                    + "\n"
                )
                assert not (preview / "unexpected-terminal").exists()
            finally:
                (preview / "finish-typing").touch()
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                wait_exit(typing_pids, socket_removed=False)
        log = log_path.read_text()
        for error in ("ERROR", "TypeError", "ReferenceError", "Binding loop", "Unable to assign"):
            assert error not in log, f"Production QML error: {error}"
    print(
        "Production service: offline form, missing keys, atomic repair, form persistence, "
        "panel/widget, fixture PCM reaping, complete typing during QML destruction, "
        "and automatic recovery from a pre-connection reload race passed."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "QT_QPA_PLATFORM": "wayland", "QT_QUICK_BACKEND": "software"}
    env.pop("WAYLAND_DEBUG", None)
    with tempfile.TemporaryDirectory(prefix="voice-ui-", dir=args.artifacts.parent) as directory:
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
                ipc("settings")
                assert not json.loads(ipc("report"))["canSave"]
                assert json.loads(ipc("report"))["statusText"] == (
                    "Backend offline · enable the plugin or check its dependencies."
                )
                ipc("save")
                assert json.loads(ipc("report"))["saved"] == {}
                event({"type": "settings", "settings": {}})
                assert json.loads(ipc("report"))["connected"]
                assert not json.loads(ipc("report"))["canSave"]
                ipc("save")
                assert json.loads(ipc("report"))["saved"] == {}
                event({"type": "settings", "settings": SAVED_SETTINGS})
                report = json.loads(ipc("report"))
                assert report["canSave"] and report["values"] == SAVED_SETTINGS
                edited = SAVED_SETTINGS | {"model": "unsaved-local-edit"}
                ipc("edit", json.dumps(edited))
                event({"type": "settings", "settings": SAVED_SETTINGS})
                assert json.loads(ipc("report"))["values"] == edited
                ipc("save")
                assert json.loads(ipc("report"))["saved"] == edited
                ipc("close")
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
                    if phase == "done":
                        assert report["title"] == "Text inserted", report
                    capture(f"voice-{phase}")
                event({"type": "state", "phase": "listening", "text": "Discarded draft"})
                assert json.loads(ipc("report"))["hudVisible"]
                event({"type": "preview", "text": ""})
                event({"type": "state", "phase": "idle"})
                report = json.loads(ipc("report"))
                assert not report["hudVisible"] and report["text"] == "", report
                capture("voice-no-speech")
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
    print(
        "UI settings initialization/edit preservation, states, preview replacement, "
        "Escape, and Wayland input policies passed."
    )
    production(env, args.artifacts)


if __name__ == "__main__":
    main()
