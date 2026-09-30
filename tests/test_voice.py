import asyncio
import base64
import io
import json
import os
import stat
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

import voice
from install import DEFAULT_SETTINGS, load_settings


async def packets(*chunks):
    for chunk in chunks:
        yield chunk


async def event(socket, kind, text=""):
    await socket.send(json.dumps({"message_type": kind, "text": text}))


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def session(self, handler, chunks, timeout=1):
        events = []
        ready = AsyncMock()
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            result = await voice.recognize(
                chunks,
                "test-secret",
                AsyncMock(side_effect=events.append),
                ready,
                endpoint=f"ws://127.0.0.1:{port}",
                timeout=timeout,
            )
        return result, events, ready

    async def test_earlier_ack_does_not_complete_final_tail(self):
        messages = []

        async def handler(socket):
            self.assertEqual(socket.request.headers["xi-api-key"], "test-secret")
            await event(socket, "session_started")
            for _ in range(302):
                messages.append(json.loads(await socket.recv()))
            await event(socket, "committed_transcript", "Earlier.")
            await event(socket, "committed_transcript_with_timestamps", "Earlier.")
            await asyncio.sleep(0.05)
            await event(socket, "final_transcript", "tail draft")
            await event(socket, "committed_transcript", "Last words.")
            await socket.wait_closed()

        result, events, _ = await self.session(
            handler,
            packets(
                b"\0" * voice.SEGMENT_BYTES,
                b"\0\x40" * 80,
            ),
        )
        self.assertEqual(result, "Earlier. Last words.")
        self.assertTrue(messages[299]["commit"])
        self.assertFalse(messages[300]["commit"])
        self.assertEqual(base64.b64decode(messages[300]["audio_base_64"]), b"\0\x40" * 80)
        self.assertEqual(messages[-1]["audio_base_64"], "")
        self.assertTrue(messages[-1]["commit"])
        self.assertIn({"type": "preview", "text": "Earlier. tail draft"}, events)

    async def test_exact_boundary_has_no_empty_commit(self):
        async def handler(socket):
            await event(socket, "session_started")
            for index in range(300):
                message = json.loads(await socket.recv())
                self.assertEqual(message["commit"], index == 299)
            await event(socket, "committed_transcript", "Boundary.")
            with self.assertRaises(ConnectionClosed):
                await socket.recv()

        result, _, _ = await self.session(handler, packets(b"\0" * voice.SEGMENT_BYTES))
        self.assertEqual(result, "Boundary.")

    async def test_preview_is_revisable_and_only_commit_is_final(self):
        async def handler(socket):
            await event(socket, "session_started")
            await socket.recv()
            for text in ["hello wor", "hello world", "hello"]:
                await event(socket, "partial_transcript", text)
            await event(socket, "final_transcript", "not final")
            await socket.recv()
            await event(socket, "committed_transcript", "Hello.")
            await socket.wait_closed()

        result, events, ready = await self.session(handler, packets(b"\0" * 3200))
        self.assertEqual(result, "Hello.")
        self.assertEqual(
            [e["text"] for e in events if e["type"] == "preview"],
            ["hello wor", "hello world", "hello", "not final", "Hello."],
        )
        ready.assert_awaited_once()

    async def test_errors_close_and_timeouts_never_finalize(self):
        for failure in ["close", "error", "start_timeout", "drain_timeout", "bad_json"]:
            with self.subTest(failure=failure):

                async def handler(socket):
                    if failure == "start_timeout":
                        await socket.wait_closed()
                        return
                    await event(socket, "session_started")
                    await socket.recv()
                    await event(socket, "partial_transcript", "never insert")
                    await socket.recv()
                    if failure == "close":
                        await socket.close()
                    elif failure == "error":
                        await event(socket, "rate_limited", "test-secret")
                    elif failure == "bad_json":
                        await socket.send("invalid-test-secret")
                    else:
                        await socket.wait_closed()

                with self.assertRaises(voice.VoiceError) as error:
                    await self.session(handler, packets(b"\0" * 160), timeout=0.1)
                self.assertNotIn("test-secret", str(error.exception))

    async def test_cancel_does_not_send_final_commit(self):
        seen = asyncio.Event()
        messages = []

        async def handler(socket):
            await event(socket, "session_started")
            messages.append(json.loads(await socket.recv()))
            await event(socket, "partial_transcript", "discard")
            seen.set()
            try:
                messages.append(json.loads(await socket.recv()))
            except ConnectionClosed:
                pass

        async def audio():
            yield b"\0" * 3200
            await asyncio.Future()

        task = asyncio.create_task(self.session(handler, audio()))
        await asyncio.wait_for(seen.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(messages), 1)
        self.assertFalse(messages[0]["commit"])


class FakeCapture:
    instances = []

    def __init__(self, microphone):
        self.microphone = microphone
        self.closed = False
        self.stopped = asyncio.Event()
        self.instances.append(self)

    async def start(self):
        pass

    def stop(self):
        self.stopped.set()

    async def chunks(self):
        yield b"\0\x40" * 1600
        await self.stopped.wait()
        yield b"\0\x20" * 7

    async def close(self):
        self.closed = True


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name)
        self.events = []
        self.backend = voice.Backend(
            AsyncMock(side_effect=self.events.append), self.config, FakeCapture
        )
        self.device_patch = patch(
            "voice.devices",
            AsyncMock(
                return_value=[
                    {"value": "auto", "label": "Default"},
                    {"value": "mic", "label": "Mic", "id": 9, "serial": "42"},
                ]
            ),
        )
        self.device_patch.start()
        self.env_patch = patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""})
        self.env_patch.start()

    async def asyncTearDown(self):
        if self.backend.busy:
            await self.backend.cancel()
        self.env_patch.stop()
        self.device_patch.stop()
        self.temp.cleanup()

    async def test_merged_settings_preserve_chords_and_reject_invalid(self):
        original = DEFAULT_SETTINGS | {"chords": ["SUPER + F8"], "translate_key": "Alt_R"}
        path = self.config / "settings.json"
        path.write_text(json.dumps(original))
        result = await self.backend.command(
            {
                "command": "configure",
                "settings": {
                    "mode": "translate",
                    "microphone": "mic",
                    "model": "other-model",
                },
            }
        )
        self.assertTrue(result["accepted"])
        settings = load_settings(path)
        self.assertEqual(settings["chords"], ["SUPER + F8"])
        self.assertEqual(settings["translate_key"], "Alt_R")
        self.assertEqual(settings["mode"], "translate")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        before = path.read_bytes()
        for changes in [
            {"mode": "bad"},
            {"base_url": "http://unsafe.example"},
            {"microphone": "bad\nnode"},
            {"chords": ["F9"]},
        ]:
            result = await self.backend.command({"command": "configure", "settings": changes})
            self.assertFalse(result["accepted"])
            self.assertEqual(before, path.read_bytes())
        self.assertTrue(any(e["type"] == "settings_error" for e in self.events))

    async def test_test_mode_never_connects_and_busy_settings_rejected(self):
        with patch("voice.recognize", AsyncMock()) as recognizer:
            result = await self.backend.command({"command": "test", "microphone": "mic"})
            self.assertTrue(result["accepted"])
            self.assertEqual(self.backend.capture.microphone, "mic")
            self.assertEqual(self.backend.settings["microphone"], "auto")
            await asyncio.sleep(0.01)
            self.assertTrue(any(e["type"] == "levels" for e in self.events))
            result = await self.backend.command(
                {"command": "configure", "settings": {"mode": "translate"}}
            )
            self.assertFalse(result["accepted"])
            await self.backend.command({"command": "test"})
            recognizer.assert_not_awaited()
        self.assertEqual(self.backend.state["phase"], "cancelled")
        self.assertTrue(FakeCapture.instances[-1].closed)

    async def test_saved_microphone_serial_works_in_panel_test(self):
        await self.backend.configure({"microphone": "42"})
        self.assertEqual(self.backend.settings_event["devices"][1]["serial"], "42")
        for missing in ("43", ""):
            self.assertFalse(
                (await self.backend.command({"command": "test", "microphone": missing}))["accepted"]
            )
        with patch("voice.recognize", AsyncMock()) as recognizer:
            result = await self.backend.command({"command": "test", "microphone": "42"})
            self.assertTrue(result["accepted"])
            self.assertEqual(self.backend.capture.microphone, "42")
            await asyncio.sleep(0.01)
            self.assertTrue(any(e["type"] == "levels" for e in self.events))
            await self.backend.command({"command": "test"})
            recognizer.assert_not_awaited()
        self.assertEqual(load_settings(self.config / "settings.json")["microphone"], "42")

    async def test_missing_key_and_invalid_commands_are_rejected(self):
        for command in [
            {"command": "start"},
            {"command": "stop"},
            {"command": "what"},
            {"command": []},
            {"command": "test", "mode": {}},
            {"command": "test", "microphone": []},
            {"command": "test", "microphone": "unavailable"},
            [],
        ]:
            self.assertFalse((await self.backend.command(command))["accepted"])

    async def test_cleanup_failure_uses_complete_raw_once(self):
        (self.config / "elevenlabs_api_key").write_text("secret-not-for-events")
        with (
            patch("voice.recognize", AsyncMock(return_value="Complete raw.")),
            patch("voice.execute", AsyncMock(return_value=(1, b""))) as cleanup,
            patch.dict(
                os.environ,
                {"DICTATION_LLM_API_KEY": "wrong-key", "DICTATION_LLM_MODEL": "wrong-model"},
            ),
            patch.object(self.backend, "output", AsyncMock()) as output,
        ):
            await self.backend.command({"command": "start", "mode": "translate"})
            await self.backend.task
            output.assert_awaited_once_with(
                "Complete raw.", "Cleanup failed; using the complete original transcript"
            )
            env = cleanup.await_args.kwargs["env"]
            self.assertNotIn("DICTATION_LLM_API_KEY", env)
            self.assertNotIn("DICTATION_LLM_MODEL", env)
            self.assertEqual(env["DICTATION_VOCABULARY_FILE"], str(self.config / "vocabulary.txt"))
            self.assertEqual(env["DICTATION_LLM_API_KEY_FILE"], str(self.config / "api_key"))
        self.assertNotIn("secret-not-for-events", json.dumps(self.events))

    async def test_cancel_cleanup_never_outputs(self):
        (self.config / "elevenlabs_api_key").write_text("fake")
        entered = asyncio.Event()

        async def cleanup(*args, **kwargs):
            entered.set()
            await asyncio.Future()

        with (
            patch("voice.recognize", AsyncMock(return_value="Complete raw.")),
            patch("voice.execute", side_effect=cleanup),
            patch.object(self.backend, "output", AsyncMock()) as output,
        ):
            await self.backend.command({"command": "start"})
            await asyncio.wait_for(entered.wait(), 2)
            await self.backend.cancel()
            output.assert_not_awaited()
        self.assertEqual(self.backend.preview["text"], "")

    async def test_asr_failure_never_outputs_partial(self):
        (self.config / "elevenlabs_api_key").write_text("fake")
        with (
            patch("voice.recognize", AsyncMock(side_effect=voice.VoiceError("failed"))),
            patch.object(self.backend, "output", AsyncMock()) as output,
        ):
            await self.backend.command({"command": "start"})
            await self.backend.task
            output.assert_not_awaited()
        self.assertEqual(self.backend.state["phase"], "error")

    async def test_output_probe_fallback_and_partial_failure(self):
        with patch("voice.execute", AsyncMock(side_effect=[(1, b""), (0, b"")])) as run:
            await self.backend.output("Final", "")
            self.assertEqual(self.backend.state["phase"], "clipboard")
            self.assertEqual(run.await_args_list[0].args, (["wtype", "-"], b""))
            self.assertEqual(run.await_args_list[1].args, (["wl-copy"], b"Final"))
        with patch("voice.execute", AsyncMock(side_effect=[(0, b""), (1, b"")])) as run:
            await self.backend.output("Final", "")
            self.assertEqual(self.backend.state["phase"], "error")
            self.assertEqual(run.await_count, 2)

    async def test_late_cancel_is_rejected_not_partial(self):
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def run(args, data, **kwargs):
            if data:
                entered.set()
                await finish.wait()
            return 0, b""

        with patch("voice.execute", side_effect=run):
            task = asyncio.create_task(self.backend.output("Final", ""))
            await asyncio.wait_for(entered.wait(), 1)
            result = await self.backend.command({"command": "cancel"})
            self.assertFalse(result["accepted"])
            finish.set()
            await task
        self.assertEqual(self.backend.state["phase"], "done")

    async def test_stop_streams_final_capture_tail_then_one_output(self):
        (self.config / "elevenlabs_api_key").write_text("fake")
        chunks = []
        listening = asyncio.Event()

        async def recognize(audio, key, emit, ready):
            await ready()
            listening.set()
            async for pcm in audio:
                chunks.append(pcm)
            return "Complete."

        with (
            patch("voice.recognize", side_effect=recognize),
            patch("voice.execute", AsyncMock(return_value=(0, b"Cleaned."))),
            patch.object(self.backend, "output", AsyncMock()) as output,
        ):
            await self.backend.command({"command": "start"})
            await asyncio.wait_for(listening.wait(), 1)
            task = self.backend.task
            self.assertTrue(
                (await self.backend.command({"command": "start", "mode": "translate"}))["accepted"]
            )
            self.assertIs(self.backend.task, task)
            self.assertEqual(self.backend.settings["mode"], "rephrase")
            await self.backend.emit({"type": "preview", "text": "Last draft."})
            self.assertTrue((await self.backend.command({"command": "stop"}))["accepted"])
            self.assertEqual(self.backend.state["text"], "Last draft.")
            await self.backend.task
            output.assert_awaited_once_with("Cleaned.", "")
        self.assertEqual(chunks[-1], b"\0\x20" * 7)

    async def test_idle_cancel_is_silent_and_saved_mode_is_used(self):
        self.assertTrue((await self.backend.command({"command": "cancel"}))["accepted"])
        self.assertEqual(self.events, [])
        (self.config / "elevenlabs_api_key").write_text("fake")
        await self.backend.configure({"mode": "translate"})
        with (
            patch("voice.recognize", AsyncMock(return_value="Raw.")),
            patch("voice.execute", AsyncMock(return_value=(0, b"English."))) as cleanup,
            patch.object(self.backend, "output", AsyncMock()),
        ):
            await self.backend.command({"command": "start"})
            await self.backend.task
        self.assertEqual(self.backend.state["mode"], "translate")
        self.assertIn("translate", cleanup.await_args.args[0])

    async def test_early_release_suppresses_only_its_own_delayed_start(self):
        (self.config / "elevenlabs_api_key").write_text("fake")
        for command in ("stop", "start", "start"):
            result = await self.backend.command({"command": command, "operation": "released-1"})
            self.assertTrue(result["accepted"])
        self.assertEqual(self.events, [])
        self.assertIsNone(self.backend.capture)
        self.assertIsNone(self.backend.task)
        with patch("voice.recognize", AsyncMock(side_effect=voice.VoiceError("fixture"))):
            self.assertTrue(
                (await self.backend.command({"command": "start", "operation": "next-2"}))[
                    "accepted"
                ]
            )
            await self.backend.task
        self.assertIsNotNone(self.backend.capture)

    async def test_operation_release_cannot_stop_another_press(self):
        (self.config / "elevenlabs_api_key").write_text("fake")
        listening = asyncio.Event()

        async def recognize(audio, key, emit, ready):
            await ready()
            listening.set()
            async for _ in audio:
                pass
            return "Complete."

        with (
            patch("voice.recognize", side_effect=recognize),
            patch("voice.execute", AsyncMock(return_value=(0, b"Cleaned."))),
            patch.object(self.backend, "output", AsyncMock()) as output,
        ):
            await self.backend.command({"command": "start", "operation": "active-1"})
            await asyncio.wait_for(listening.wait(), 1)
            task = self.backend.task
            self.assertTrue(
                (await self.backend.command({"command": "start", "operation": "active-1"}))[
                    "accepted"
                ]
            )
            self.assertFalse(
                (await self.backend.command({"command": "start", "operation": "other-2"}))[
                    "accepted"
                ]
            )
            self.assertTrue(
                (await self.backend.command({"command": "stop", "operation": "other-2"}))[
                    "accepted"
                ]
            )
            self.assertIs(self.backend.task, task)
            self.assertEqual(self.backend.state["phase"], "listening")
            self.assertFalse(self.backend.capture.stopped.is_set())
            await self.backend.command({"command": "stop", "operation": "active-1"})
            await task
            output.assert_awaited_once_with("Cleaned.", "")

    async def test_invalid_operation_is_rejected_without_side_effects(self):
        for operation in ("", [], "a b", "x" * 129, "中文"):
            self.assertFalse(
                (await self.backend.command({"command": "stop", "operation": operation}))[
                    "accepted"
                ]
            )
        self.assertEqual(self.events, [])
        self.assertEqual(len(self.backend.stopped_operations), 0)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_pcm_level_math(self):
        self.assertEqual(voice.levels(b"\0" * 3200), [0.0] * 40)
        self.assertEqual(voice.levels(struct.pack("<h", -32768) * 1600), [1.0] * 40)
        self.assertEqual(voice.levels(struct.pack("<h", 16384) * 1600), [0.5] * 40)
        alternating = struct.pack("<hh", -16384, 16384) * 800
        self.assertEqual(voice.levels(alternating), [0.5] * 40)

    async def test_muted_or_unavailable_never_starts_recorder(self):
        for inputs, result in [
            ([{"value": "auto"}], (0, b"")),
            ([{"value": "auto"}, {"value": "mic", "id": 2}], (0, b"[MUTED]")),
        ]:
            with (
                patch("voice.devices", AsyncMock(return_value=inputs)),
                patch("voice.execute", AsyncMock(return_value=result)),
                patch("voice.asyncio.create_subprocess_exec", AsyncMock()) as spawn,
            ):
                with self.assertRaises(voice.VoiceError):
                    await voice.Capture("auto").start()
                spawn.assert_not_awaited()

    async def test_microphone_serial_checks_matching_node_mute(self):
        inputs = [{"value": "auto"}, {"value": "mic", "id": 9, "serial": "42"}]
        with (
            patch("voice.devices", AsyncMock(return_value=inputs)),
            patch("voice.execute", AsyncMock(return_value=(0, b"Volume: 1"))) as inspect,
            patch("voice.asyncio.create_subprocess_exec", AsyncMock()) as spawn,
        ):
            await voice.Capture("42").start()
        inspect.assert_awaited_once_with(["wpctl", "get-volume", "9"])
        self.assertEqual(spawn.await_args.args[-3:], ("--target", "42", "-"))

    async def test_recorder_stop_drains_final_bytes(self):
        for code in (0, 1):
            with self.subTest(exit_code=code):
                capture = voice.Capture("auto")
                capture.proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    "import os,signal,time; "
                    "signal.signal(signal.SIGINT, lambda *a: "
                    f"(os.write(1,b'\\0\\x40'*7),exit({code}))); "
                    "os.write(1,b'\\0'*3200); time.sleep(10)",
                    stdout=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    chunks = capture.chunks()
                    self.assertEqual(len(await anext(chunks)), 3200)
                    capture.stop()
                    self.assertEqual(await anext(chunks), b"\0\x40" * 7)
                    with self.assertRaises(StopAsyncIteration):
                        await anext(chunks)
                    self.assertEqual(capture.proc.returncode, code)
                finally:
                    await capture.close()

    async def test_stop_after_recorder_already_exited_does_not_hide_failure(self):
        capture = voice.Capture("auto")
        capture.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "exit(1)",
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            await capture.proc.wait()
            capture.stop()
            self.assertFalse(capture.stopping)
            with self.assertRaisesRegex(voice.VoiceError, "ended unexpectedly"):
                await anext(capture.chunks())
        finally:
            await capture.close()

    async def test_stop_rejects_error_exit_and_forced_kill(self):
        for handler, expected in (("lambda *a: exit(2)", 2), ("signal.SIG_IGN", -9)):
            with self.subTest(exit_code=expected):
                capture = voice.Capture("auto")
                capture.proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    f"import os,signal,time; signal.signal(signal.SIGINT,{handler}); "
                    "os.write(1,b'\\0'*3200); time.sleep(10)",
                    stdout=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    chunks = capture.chunks()
                    await anext(chunks)
                    capture.stop()
                    with self.assertRaisesRegex(voice.VoiceError, "ended unexpectedly"):
                        await anext(chunks)
                    self.assertEqual(capture.proc.returncode, expected)
                finally:
                    await capture.close()

    async def test_subprocess_timeout_and_cancel_reap_child(self):
        with self.assertRaises(TimeoutError):
            await voice.execute([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)

    async def test_close_discards_full_capture_pipe_without_hanging(self):
        capture = voice.Capture("auto")
        capture.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import os; os.write(1,b'x'*1000000)",
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        await asyncio.sleep(0.05)
        await asyncio.wait_for(capture.close(), 2)
        self.assertIsNotNone(capture.proc.returncode)

    async def test_cancel_subprocess_reaps_it(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / "pid"
            task = asyncio.create_task(
                voice.execute(
                    [
                        sys.executable,
                        "-c",
                        "import os,time,pathlib; "
                        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
                        "time.sleep(10)",
                    ]
                )
            )
            for _ in range(100):
                if pidfile.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(pidfile.exists())
            pid = int(pidfile.read_text())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_reads_large_unicode_response_and_forwards_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                socket_path = voice.runtime_directory() / "control.sock"
                response = {
                    "accepted": True,
                    "state": {"text": "混合🗣️text" * 20000},
                    "preview": {"text": "Different preview." * 10000},
                }
                received = []

                async def client(reader, writer):
                    received.append(json.loads(await reader.readline()))
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()

                server = await asyncio.start_unix_server(client, str(socket_path))
                socket_path.chmod(0o600)
                async with server:
                    for command, mode, operation in (
                        ("status", None, None),
                        ("start", "translate", "session-23"),
                        ("stop", None, "session-23"),
                    ):
                        output = io.StringIO()
                        with redirect_stdout(output):
                            self.assertEqual(await voice.cli(command, mode, operation), 0)
                        self.assertEqual(json.loads(output.getvalue()), response)
                self.assertEqual(
                    received,
                    [
                        {"command": "status"},
                        {"command": "start", "mode": "translate", "operation": "session-23"},
                        {"command": "stop", "operation": "session-23"},
                    ],
                )

    async def test_daemon_eof_finishes_in_progress_typing(self):
        for kill_parent in (False, True):
            with self.subTest(kill_parent=kill_parent):
                await self.typing_shutdown(kill_parent)

    async def typing_shutdown(self, kill_parent):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            config = root / "config/omarchy-dictation"
            config.mkdir(parents=True)
            (config / "elevenlabs_api_key").write_text("fake")
            tools = root / "tools"
            tools.mkdir()
            typed = root / "typed"
            pidfile = root / "typing-pid"
            (tools / "wtype").write_text(
                f"#!{sys.executable}\nimport os,sys,time,pathlib\n"
                "data=sys.stdin.buffer.read()\n"
                "if data:\n"
                f" pathlib.Path({str(typed)!r}).write_bytes(data[:5])\n"
                f" pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
                " time.sleep(.3)\n"
                f" pathlib.Path({str(typed)!r}).write_bytes(data)\n"
            )
            (tools / "wtype").chmod(0o755)
            env = os.environ | {
                "XDG_RUNTIME_DIR": str(runtime),
                "XDG_CONFIG_HOME": str(root / "config"),
                "PATH": str(tools),
                "ELEVENLABS_API_KEY": "",
            }
            # Bypass only recognition to exercise real daemon teardown and wtype.
            source = (
                "import asyncio,voice\n"
                "async def run(self,testing):\n"
                " await self.output('Complete fixture text.', '')\n"
                "voice.Backend.run=run\n"
                "asyncio.run(voice.daemon())\n"
            )
            if kill_parent:
                source = (
                    "import subprocess,sys,install\n"
                    f"command={source!r}\n"
                    "install.runtime_error=lambda *args: None\n"
                    "spawn=subprocess.Popen\n"
                    "def child(args,**kwargs):\n"
                    " return spawn([sys.executable,'-c',command],**kwargs)\n"
                    "install.subprocess.Popen=child\n"
                    "install.run_daemon(install.Path.cwd(),install.Paths.default())\n"
                )
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                source,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                for _ in range(3):
                    await asyncio.wait_for(proc.stdout.readline(), 3)
                proc.stdin.write(b'{"command":"start"}\n')
                await proc.stdin.drain()
                for _ in range(100):
                    if pidfile.exists():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(pidfile.exists())
                self.assertEqual(typed.read_bytes(), b"Compl")
                pid = int(pidfile.read_text())
                proc.stdin.close()
                if kill_parent:
                    # Model Quickshell Process destruction: kill only its direct
                    # parent, while the inherited command stream reaches EOF.
                    proc.kill()
                output, error = await asyncio.wait_for(proc.communicate(), 3)
                self.assertEqual(proc.returncode, -9 if kill_parent else 0, error.decode())
                self.assertEqual(typed.read_bytes(), b"Complete fixture text.")
                events = [json.loads(line) for line in output.splitlines()]
                self.assertTrue(any(e.get("phase") == "done" for e in events))
                self.assertFalse((runtime / "omarchy-dictation/control.sock").exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

    async def test_stdio_cli_singleton_permissions_and_eof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            config = root / "config"
            tools = root / "tools"
            tools.mkdir()
            # Only fake desktop utilities are visible; never touch the real session.
            nodes = [
                {
                    "id": 9,
                    "info": {
                        "props": {
                            "media.class": "Audio/Source",
                            "node.name": "fake-mic",
                        }
                    },
                }
            ]
            (tools / "pw-dump").write_text(f"#!/bin/sh\nprintf '%s' '{json.dumps(nodes)}'\n")
            (tools / "pw-dump").chmod(0o755)
            (tools / "wpctl").write_text("#!/bin/sh\nprintf 'Volume: 1.00'\n")
            (tools / "wpctl").chmod(0o755)
            pidfile = root / "recorder-pid"
            (tools / "pw-record").write_text(
                f"#!{sys.executable}\nimport os,time,pathlib\n"
                f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
                "os.write(1,b'\\0'*3200)\ntime.sleep(30)\n"
            )
            (tools / "pw-record").chmod(0o755)
            env = os.environ | {
                "XDG_RUNTIME_DIR": str(runtime),
                "XDG_CONFIG_HOME": str(config),
                "PATH": str(tools),
                "ELEVENLABS_API_KEY": "",
            }
            args = [sys.executable, str(Path(voice.__file__)), "daemon", "--stdio"]
            proc = await asyncio.create_subprocess_exec(
                *args,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                initial = [
                    json.loads(await asyncio.wait_for(proc.stdout.readline(), 3)) for _ in range(3)
                ]
                self.assertEqual([e["type"] for e in initial], ["settings", "state", "preview"])
                socket_path = runtime / "omarchy-dictation/control.sock"
                self.assertEqual(stat.S_IMODE(socket_path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(socket_path.parent.stat().st_mode), 0o700)
                self.assertEqual(
                    stat.S_IMODE((socket_path.parent / "daemon.lock").stat().st_mode), 0o600
                )
                other = await asyncio.create_subprocess_exec(
                    *args,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(other.communicate(), 3)
                self.assertEqual(other.returncode, 1)
                cli = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(Path(voice.__file__)),
                    "status",
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                )
                output, _ = await asyncio.wait_for(cli.communicate(), 3)
                self.assertTrue(json.loads(output)["accepted"])
                self.assertEqual(json.loads(output)["state"]["phase"], "idle")
                proc.stdin.write(b'{"command":"stop"}\n')
                await proc.stdin.drain()
                ack = json.loads(await asyncio.wait_for(proc.stdout.readline(), 3))
                self.assertEqual(ack["type"], "ack")
                self.assertFalse(ack["accepted"])
                proc.stdin.write(b'{"command":"test"}\n')
                await proc.stdin.drain()
                while True:
                    message = json.loads(await asyncio.wait_for(proc.stdout.readline(), 3))
                    if message["type"] == "levels":
                        break
                pid = int(pidfile.read_text())
                proc.stdin.close()
                await asyncio.wait_for(proc.wait(), 3)
                self.assertEqual(proc.returncode, 0, (await proc.stderr.read()).decode())
                self.assertFalse(socket_path.exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

    async def test_runtime_rejects_public_or_symlink_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                root.chmod(0o755)
                with self.assertRaises(voice.VoiceError):
                    voice.runtime_directory()
                root.chmod(0o700)
                (root / "other").mkdir()
                (root / "omarchy-dictation").symlink_to(root / "other")
                with self.assertRaises(voice.VoiceError):
                    voice.runtime_directory()


if __name__ == "__main__":
    unittest.main()
