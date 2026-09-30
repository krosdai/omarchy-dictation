#!/usr/bin/python
"""Plugin-owned PipeWire capture, Scribe realtime recognition and final output."""

import argparse
import asyncio
import base64
import contextlib
import fcntl
import json
import math
import os
import signal
import stat
import struct
import sys
import tempfile
from pathlib import Path

from websockets.asyncio.client import connect

from install import DEFAULT_SETTINGS, atomic_write, load_settings

ENDPOINT = (
    "wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id=scribe_v2_realtime"
    "&audio_format=pcm_16000&commit_strategy=manual&include_timestamps=false"
)
CHUNK_BYTES = 3200
SEGMENT_BYTES = 30 * 32000
TIMEOUT = 20
MAX_TEXT = 1024 * 1024
MODES = {"rephrase", "translate"}
EDITABLE = {"mode", "microphone", "base_url", "model", "reasoning_effort"}


class VoiceError(Exception):
    """Safe, credential-free operational error."""


def levels(pcm):
    """Forty linear RMS envelope bars from little-endian signed 16-bit PCM."""
    count = len(pcm) // 2
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])
    result = []
    for bar in range(40):
        part = samples[bar * count // 40 : (bar + 1) * count // 40]
        result.append(math.sqrt(sum((s / 32768) ** 2 for s in part) / len(part)) if part else 0.0)
    return result


async def terminate(proc):
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)

    async def discard():
        if proc.stdout:
            while await proc.stdout.read(8192):
                pass

    # A paused stdout pipe can otherwise make Process.wait() hang after SIGKILL.
    async with asyncio.timeout(3):
        await asyncio.gather(discard(), proc.wait())


async def execute(args, data=None, timeout=5, env=None):
    """Bound time, output size and cancellation for every short-lived child."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )

    async def read():
        output = bytearray()
        while chunk := await proc.stdout.read(8192):
            output.extend(chunk)
            if len(output) > MAX_TEXT:
                raise VoiceError("Command output exceeded the safety limit")
        return bytes(output)

    async def write():
        if data:
            proc.stdin.write(data)
            await proc.stdin.drain()
        proc.stdin.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await proc.stdin.wait_closed()

    readers = [asyncio.create_task(read()), asyncio.create_task(write())]
    try:
        async with asyncio.timeout(timeout):
            output, _ = await asyncio.gather(*readers)
            return await proc.wait(), output
    finally:
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await terminate(proc)


async def devices():
    code, output = await execute(["pw-dump"])
    if code:
        raise VoiceError("Cannot inspect PipeWire inputs")
    try:
        nodes = json.loads(output)
        inputs = [{"value": "auto", "label": "Default microphone"}]
        for node in nodes:
            props = node.get("info", {}).get("props", {})
            if props.get("media.class") == "Audio/Source" and props.get("node.name"):
                inputs.append(
                    {
                        "value": props["node.name"],
                        "label": props.get("node.description", props["node.name"]),
                        "id": node["id"],
                        "serial": str(props.get("object.serial", "")),
                    }
                )
        return inputs
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise VoiceError("Invalid PipeWire input information") from error


class Capture:
    def __init__(self, microphone):
        self.microphone = microphone
        self.proc = None
        self.stopping = False
        self.stop_task = None

    async def start(self):
        inputs = await devices()
        target = "@DEFAULT_AUDIO_SOURCE@"
        if len(inputs) == 1:
            raise VoiceError("No microphone input is available")
        if self.microphone != "auto":
            match = next(
                (d for d in inputs if self.microphone in (d["value"], d.get("serial"))), None
            )
            if not match:
                raise VoiceError("The selected microphone is unavailable")
            target = str(match["id"])
        code, volume = await execute(["wpctl", "get-volume", target])
        if code:
            raise VoiceError("Cannot inspect microphone mute state")
        if b"MUTED" in volume:
            raise VoiceError("Microphone is muted; unmute it yourself to record")
        self.proc = await asyncio.create_subprocess_exec(
            "pw-record",
            "--raw",
            "--rate",
            "16000",
            "--channels",
            "1",
            "--format",
            "s16",
            "--target",
            self.microphone,
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

    def stop(self):
        if self.proc and self.proc.returncode is None and self.stop_task is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.send_signal(signal.SIGINT)
                self.stopping = True
                self.stop_task = asyncio.create_task(self._stop_deadline())

    async def _stop_deadline(self):
        try:
            await asyncio.wait_for(self.proc.wait(), 3)
        except TimeoutError:
            # chunks() remains the sole reader until close(); preserve trailing PCM.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)

    async def chunks(self):
        buffer = bytearray()
        while data := await self.proc.stdout.read(CHUNK_BYTES):
            buffer.extend(data)
            while len(buffer) >= CHUNK_BYTES:
                yield bytes(buffer[:CHUNK_BYTES])
                del buffer[:CHUNK_BYTES]
        if len(buffer) % 2:
            raise VoiceError("Microphone returned incomplete PCM samples")
        if buffer:
            yield bytes(buffer)
        code = await self.proc.wait()
        # pw-record 1.6.8 returns 1 on SIGINT: its handler quits without setting
        # the playback-oriented drained flag. Accept it only after our stop signal.
        if not self.stopping or code not in (0, 1, -signal.SIGINT, 128 + signal.SIGINT):
            raise VoiceError("Microphone capture ended unexpectedly")

    async def close(self):
        if self.stop_task:
            self.stop_task.cancel()
            await asyncio.gather(self.stop_task, return_exceptions=True)
        if self.proc:
            await terminate(self.proc)


async def recognize(audio, key, emit, ready, endpoint=ENDPOINT, timeout=TIMEOUT):
    """Only committed transcripts are final; every manual commit needs its own ack."""
    committed = []
    pending = []
    segment = 0
    started = False
    draining = False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    tasks = []
    try:
        async with connect(
            endpoint,
            additional_headers={"xi-api-key": key},
            open_timeout=timeout,
            close_timeout=1,
            max_size=MAX_TEXT,
            max_queue=16,
            proxy=None,
        ) as socket:
            receiver = asyncio.create_task(socket.recv())
            source = None
            tasks.append(receiver)

            async def send(pcm, commit):
                await asyncio.wait_for(
                    socket.send(
                        json.dumps(
                            {
                                "message_type": "input_audio_chunk",
                                "audio_base_64": base64.b64encode(pcm).decode(),
                                "commit": commit,
                                "sample_rate": 16000,
                            }
                        )
                    ),
                    timeout,
                )
                if commit:
                    pending.append(loop.time() + timeout)

            while True:
                limits = ([deadline] if not started or draining else []) + pending[:1]
                if limits and loop.time() >= min(limits):
                    raise VoiceError("Recognition session or final acknowledgement timed out")
                wait = max(0, min(limits) - loop.time()) if limits else None
                active = [receiver] + ([source] if source else [])
                done, _ = await asyncio.wait(
                    active, timeout=wait, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    raise VoiceError("Recognition session or final acknowledgement timed out")
                if receiver in done:
                    value = json.loads(receiver.result())
                    kind = value.get("message_type")
                    if kind == "session_started":
                        if not started:
                            started = True
                            await ready()
                            source = asyncio.create_task(anext(audio, None))
                            tasks.append(source)
                    elif kind in {"partial_transcript", "final_transcript", "committed_transcript"}:
                        text = value.get("text")
                        if not isinstance(text, str):
                            raise VoiceError("Invalid recognition transcript")
                        if kind == "committed_transcript":
                            committed.append(text.strip())
                            if pending:
                                pending.pop(0)
                            preview = " ".join(t for t in committed if t)
                        else:
                            preview = " ".join(t for t in [*committed, text.strip()] if t)
                        if len(preview) > MAX_TEXT:
                            raise VoiceError("Transcript exceeded the safety limit")
                        await emit({"type": "preview", "text": preview})
                    elif kind not in {
                        "warning",
                        "committed_transcript_with_timestamps",
                        "final_transcript_with_timestamps",
                    }:
                        raise VoiceError(
                            "Recognition provider rejected the session; check access and quota"
                        )
                    if draining and not pending:
                        return " ".join(t for t in committed if t)
                    receiver = asyncio.create_task(socket.recv())
                    tasks = [t for t in tasks if not t.done()]
                    tasks.append(receiver)
                if source and source in done:
                    pcm = source.result()
                    source = None
                    if pcm is None:
                        if segment:
                            await send(b"", True)
                        if not pending:
                            return " ".join(t for t in committed if t)
                        draining = True
                        deadline = loop.time() + timeout
                    else:
                        await emit({"type": "levels", "values": levels(pcm)})
                        # Capture yields 100 ms packets plus one final short packet.
                        for offset in range(0, len(pcm), CHUNK_BYTES):
                            chunk = pcm[offset : offset + CHUNK_BYTES]
                            segment += len(chunk)
                            commit = segment >= SEGMENT_BYTES
                            await send(chunk, commit)
                            if commit:
                                segment = 0
                        source = asyncio.create_task(anext(audio, None))
                        tasks = [t for t in tasks if not t.done()]
                        tasks.append(source)
    except asyncio.CancelledError:
        raise
    except VoiceError:
        raise
    except Exception:
        # Never expose provider payloads, headers or exceptions containing keys.
        raise VoiceError("Recognition connection failed or closed before completion") from None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def read_key(path, environment=None):
    value = os.environ.get(environment, "") if environment else ""
    if value.strip():
        return value.strip()
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


class Backend:
    def __init__(self, emit, config=None, capture_factory=Capture):
        self.emit_output = emit
        self.config = (
            config
            or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "omarchy-dictation"
        )
        self.settings = dict(DEFAULT_SETTINGS)
        self.capture_factory = capture_factory
        self.capture = None
        self.task = None
        self.delivering = False
        self.state = {
            "type": "state",
            "phase": "idle",
            "mode": "rephrase",
            "text": "",
            "message": "",
        }
        self.preview = {"type": "preview", "text": ""}
        self.settings_event = None
        self.commands = asyncio.Lock()

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    async def emit(self, event):
        if event["type"] == "preview":
            self.preview = event
        await self.emit_output(event)

    async def phase(self, phase, text="", message=""):
        self.state = {
            "type": "state",
            "phase": phase,
            "mode": self.settings.get("mode", "rephrase"),
            "text": text,
            "message": message,
        }
        await self.emit(self.state)

    async def refresh(self):
        try:
            self.settings = load_settings(self.config / "settings.json")
            try:
                inputs = await devices()
            except (OSError, VoiceError, TimeoutError):
                inputs = [{"value": "auto", "label": "Default microphone"}]
            self.settings_event = {
                "type": "settings",
                "settings": self.settings,
                "devices": [{"value": d["value"], "label": d["label"]} for d in inputs],
                "asr_configured": bool(
                    read_key(self.config / "elevenlabs_api_key", "ELEVENLABS_API_KEY")
                ),
                "llm_configured": bool(read_key(self.config / "api_key")),
            }
            await self.emit(self.settings_event)
            return True
        except (OSError, ValueError):
            await self.emit({"type": "settings_error", "message": "Cannot load valid settings"})
            return False

    async def configure(self, changes):
        if not isinstance(changes, dict) or set(changes) - EDITABLE:
            raise VoiceError("Unsupported settings fields")
        merged = load_settings(self.config / "settings.json") | changes
        self.config.mkdir(parents=True, exist_ok=True)
        # Validate through the installer's source of truth before replacing the real file.
        with tempfile.TemporaryDirectory(dir=self.config) as directory:
            candidate = Path(directory) / "settings.json"
            atomic_write(candidate, json.dumps(merged), mode=0o600)
            load_settings(candidate)
        atomic_write(self.config / "settings.json", json.dumps(merged, indent=2) + "\n", mode=0o600)
        await self.refresh()

    async def command(self, value):
        async with self.commands:
            try:
                if not isinstance(value, dict):
                    raise VoiceError("Command must be a JSON object")
                command = value.get("command")
                if not isinstance(command, str):
                    raise VoiceError("Command name must be a string")
                if command == "status":
                    return {
                        "accepted": True,
                        "state": self.state,
                        "preview": self.preview,
                        "settings": self.settings_event,
                    }
                if command == "cancel" or (command == "test" and self.state["phase"] == "testing"):
                    await self.cancel()
                elif command == "stop":
                    if not self.busy or self.state["phase"] not in {"connecting", "listening"}:
                        raise VoiceError("No active recording to stop")
                    if self.capture:
                        self.capture.stop()
                    await self.phase("finishing", text=self.preview["text"])
                elif command in {"settings", "configure"}:
                    if self.busy:
                        raise VoiceError("Settings cannot change while busy")
                    if command == "configure":
                        await self.configure(value.get("settings"))
                    elif not await self.refresh():
                        raise VoiceError("Cannot load valid settings")
                elif command in {"start", "test"}:
                    if self.busy:
                        if command == "start" and self.state["phase"] in {
                            "connecting",
                            "listening",
                        }:
                            return {"accepted": True, "state": self.state}
                        raise VoiceError("Voice backend is busy")
                    if not await self.refresh():
                        raise VoiceError("Cannot load valid settings")
                    mode = value.get("mode", self.settings.get("mode", "rephrase"))
                    if not isinstance(mode, str) or mode not in MODES:
                        raise VoiceError("Invalid dictation mode")
                    microphone = self.settings["microphone"]
                    if command == "test" and "microphone" in value:
                        microphone = value["microphone"]
                        if not isinstance(microphone, str) or microphone not in {
                            d["value"] for d in self.settings_event["devices"]
                        }:
                            raise VoiceError("The selected microphone is unavailable")
                    self.settings = self.settings | {"mode": mode}
                    if command == "start" and not self.settings_event["asr_configured"]:
                        raise VoiceError("Configure an ElevenLabs API key first")
                    self.preview = {"type": "preview", "text": ""}
                    await self.emit(self.preview)
                    self.capture = self.capture_factory(microphone)
                    await self.phase("testing" if command == "test" else "connecting")
                    self.task = asyncio.create_task(self.run(command == "test"))
                else:
                    raise VoiceError("Unknown command")
                return {"accepted": True, "state": self.state}
            except (VoiceError, ValueError, OSError):
                message = "Invalid command or settings"
                error = sys.exception()
                if isinstance(error, VoiceError):
                    message = str(error)
                if isinstance(value, dict) and value.get("command") == "configure":
                    await self.emit({"type": "settings_error", "message": message})
                return {"accepted": False, "message": message}

    async def cancel(self):
        if self.delivering:
            raise VoiceError("Final output has already begun and cannot be cancelled safely")
        if not self.busy:
            return
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.emit({"type": "preview", "text": ""})
        await self.phase("cancelled")

    async def run(self, testing):
        capture = self.capture
        try:
            await capture.start()
            if self.state["phase"] == "finishing":
                capture.stop()
            if testing:
                async for pcm in capture.chunks():
                    await self.emit({"type": "levels", "values": levels(pcm)})
                return

            async def ready():
                if self.state["phase"] != "finishing":
                    await self.phase("listening")

            raw = await recognize(
                capture.chunks(),
                read_key(self.config / "elevenlabs_api_key", "ELEVENLABS_API_KEY"),
                self.emit,
                ready,
            )
            await capture.close()
            await self.phase("processing", text=raw)
            if not raw:
                await self.phase("done", message="No speech recognized")
                return
            message = ""
            text = raw
            try:
                env = os.environ.copy()
                # The plugin's saved settings own cleanup configuration.
                for suffix in ("API_KEY", "BASE_URL", "MODEL"):
                    env.pop(f"DICTATION_LLM_{suffix}", None)
                env["DICTATION_VOCABULARY_FILE"] = str(self.config / "vocabulary.txt")
                env["DICTATION_LLM_SETTINGS_FILE"] = str(self.config / "settings.json")
                env["DICTATION_LLM_API_KEY_FILE"] = str(self.config / "api_key")
                code, output = await execute(
                    [
                        str(Path(__file__).resolve().with_name("dictation-llm")),
                        "--mode",
                        self.settings["mode"],
                        "--strict",
                    ],
                    raw.encode(),
                    timeout=self.settings["post_process_timeout_ms"] / 1000,
                    env=env,
                )
                if code or not output.strip():
                    raise VoiceError("Cleanup failed")
                text = output.decode().strip()
            except (OSError, VoiceError, TimeoutError, UnicodeError):
                message = "Cleanup failed; using the complete original transcript"
            # Cancellation during recognition/cleanup cannot reach output.
            await self.output(text, message)
        except asyncio.CancelledError:
            raise
        except (VoiceError, OSError, TimeoutError):
            error = sys.exception()
            await self.phase(
                "error",
                message=str(error) if isinstance(error, VoiceError) else "Voice operation failed",
            )
        finally:
            await capture.close()

    async def output(self, text, message):
        # An empty invocation probes protocol support without inserting anything.
        # Never fall back after a text-bearing typing process has started: even
        # an exit status of 1 could mean it typed part of the text.
        try:
            code, _ = await execute(["wtype", "-"], b"")
        except (OSError, VoiceError, TimeoutError):
            code = 1
        # From here output is irreversible. Reject late cancellation rather than
        # interrupt typing and misrepresent a partially inserted result as cancelled.
        self.delivering = True
        try:
            await self._deliver(text, message, code)
        finally:
            self.delivering = False

    async def _deliver(self, text, message, code):
        if code:
            code, _ = await execute(["wl-copy"], text.encode())
            if code:
                raise VoiceError("Final text could not be copied")
            await self.phase(
                "clipboard",
                text=text,
                message="; ".join(m for m in [message, "Copied; paste to insert"] if m),
            )
            return
        try:
            code, _ = await execute(
                ["wtype", "-"], text.encode(), timeout=min(120, max(5, len(text) * 0.01 + 5))
            )
        except (OSError, VoiceError, TimeoutError):
            await self.phase(
                "error",
                text=text,
                message="Typing may be partial; not copying to avoid duplicate insertion",
            )
            return
        if code == 0:
            await self.phase("done", text=text, message=message)
        else:
            await self.phase(
                "error",
                text=text,
                message="Typing failed; not copying to avoid duplicate insertion",
            )


def runtime_directory():
    root = os.environ.get("XDG_RUNTIME_DIR")
    if not root:
        raise VoiceError("XDG_RUNTIME_DIR is required")
    parent = Path(root)
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise VoiceError("Runtime directory must be private and owned by this user")
    directory = parent / "omarchy-dictation"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise VoiceError("Dictation runtime directory is not private")
    return directory


async def daemon():
    directory = runtime_directory()
    descriptor = os.open(directory / "daemon.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    lock_info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(lock_info.st_mode)
        or lock_info.st_uid != os.getuid()
        or lock_info.st_nlink != 1
    ):
        os.close(descriptor)
        raise VoiceError("Unsafe singleton lock")
    os.fchmod(descriptor, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise VoiceError("Voice daemon is already running") from error
    socket_path = directory / "control.sock"
    backend = None
    server = None
    clients = set()
    transport = None
    try:
        if socket_path.is_symlink() or socket_path.exists():
            info = socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise VoiceError("Unsafe control socket path")
            socket_path.unlink()
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout.buffer
        )
        writer = asyncio.StreamWriter(transport, protocol, None, loop)
        output_lock = asyncio.Lock()

        async def emit(event):
            async with output_lock:
                writer.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
                await asyncio.wait_for(writer.drain(), 3)

        backend = Backend(emit)

        async def client(reader, connection):
            task = asyncio.current_task()
            if len(clients) >= 16:
                connection.close()
                return
            clients.add(task)
            try:
                async with asyncio.timeout(5):
                    line = await reader.readline()
                    result = await backend.command(json.loads(line))
                    connection.write((json.dumps(result) + "\n").encode())
                    await connection.drain()
            except (ValueError, OSError, TimeoutError):
                pass
            finally:
                connection.close()
                await connection.wait_closed()
                clients.discard(task)

        # Private parent prevents access during socket creation, before chmod.
        server = await asyncio.start_unix_server(client, str(socket_path), limit=65536)
        socket_path.chmod(0o600)
        reader = asyncio.StreamReader(limit=65536)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        await backend.refresh()
        backend.state["mode"] = backend.settings.get("mode", "rephrase")
        await emit(backend.state)
        await emit(backend.preview)
        while line := await reader.readline():
            try:
                value = json.loads(line)
            except ValueError:
                await emit({"type": "ack", "accepted": False, "message": "Invalid JSON command"})
                continue
            result = await backend.command(value)
            await emit({"type": "ack", **result})
    finally:
        if server:
            server.close()
            await server.wait_closed()
        for task in clients:
            task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)
        if backend and backend.busy:
            backend.task.cancel()
            await asyncio.gather(backend.task, return_exceptions=True)
        if server:
            socket_path.unlink(missing_ok=True)
        if transport:
            transport.close()
        os.close(descriptor)


async def cli(command, mode):
    directory = runtime_directory()
    info = (directory / "control.sock").lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise VoiceError("Unsafe control socket")
    async with asyncio.timeout(8):
        reader, writer = await asyncio.open_unix_connection(str(directory / "control.sock"))
        try:
            value = {"command": command}
            if mode:
                value["mode"] = mode
            writer.write((json.dumps(value) + "\n").encode())
            await writer.drain()
            result = json.loads(await reader.readline())
            print(json.dumps(result))
            return 0 if result.get("accepted") else 1
        finally:
            writer.close()
            await writer.wait_closed()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["daemon", "start", "stop", "cancel", "test", "status"])
    parser.add_argument("--stdio", action="store_true")
    parser.add_argument("--mode", choices=sorted(MODES))
    args = parser.parse_args()
    if args.command == "daemon" and not args.stdio:
        parser.error("daemon requires --stdio")
    try:
        return asyncio.run(daemon() if args.command == "daemon" else cli(args.command, args.mode))
    except (VoiceError, OSError, TimeoutError, ValueError):
        print("Voice backend unavailable or invalid request", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
