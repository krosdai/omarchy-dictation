from contextlib import redirect_stderr
from email import policy
from email.parser import BytesParser
import http.client
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import tomllib
import unittest
from unittest.mock import patch
import urllib.error

import dictation


def upload(audio=b"RIFF\x00\xfftest\r\n", extra=b""):
    # Voxtype sends the file FIRST, unlike xAI.
    return (
        b'--input\r\nContent-Disposition: form-data; name="file"; filename="input.wav"'
        b'\r\nContent-Type: audio/wav\r\n\r\n' + audio + b'\r\n'
        b'--input\r\nContent-Disposition: form-data; name="model"\r\n\r\nwhisper-1\r\n'
        b'--input\r\nContent-Disposition: form-data; name="language"\r\n\r\nauto\r\n'
        + extra + b'--input--\r\n'
    )


class ContractTests(unittest.TestCase):
    def test_file_last_and_binary_preserved(self):
        kind, body = dictation.transcription_form("multipart/form-data; boundary=input", upload())
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {kind}\r\n\r\n".encode() + body
        )
        parts = list(message.iter_parts())
        self.assertEqual([p.get_param("name", header="content-disposition") for p in parts], ["model", "file"])
        self.assertEqual(parts[0].get_payload(decode=True), b"grok-voice-transcribe-2.0")
        self.assertEqual(parts[1].get_payload(decode=True), b"RIFF\x00\xfftest\r\n")

    def test_invalid_upload(self):
        for content_type, body in [
            ("application/json", b"{}"),
            ("multipart/form-data; boundary=input", upload(b"")),
            ("multipart/form-data; boundary=input", upload(extra=b'--input\r\nContent-Disposition: form-data; name="file"\r\n\r\nother\r\n')),
        ]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                dictation.transcription_form(content_type, body)

    @patch("dictation.urllib.request.urlopen")
    def test_stt_http_contract(self, open_url):
        open_url.return_value.__enter__.return_value.read.return_value = b'{"text":"source text"}'
        self.assertEqual(dictation.XAI("secret", "unused").transcribe("multipart/form-data; boundary=input", upload()), "source text")
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.x.ai/v1/stt")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(open_url.call_args.kwargs["timeout"], 60)

    @patch("dictation.request_json")
    def test_translation_contract(self, request):
        request.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": " Tomorrow at 3 PM. "}}]}
        self.assertEqual(dictation.XAI("secret", "chosen-model").translate("明天下午三点。"), "Tomorrow at 3 PM.")
        url, body, content_type = request.call_args.args
        self.assertEqual(url, "https://api.x.ai/v1/chat/completions")
        self.assertEqual(content_type, "application/json")
        data = json.loads(body)
        self.assertEqual(data["model"], "chosen-model")
        self.assertEqual(data["messages"][1], {"role": "user", "content": "明天下午三点。"})

    @patch("dictation.request_json")
    def test_empty_and_truncated_translation_fail(self, request):
        for content, reason in [(" ", "stop"), ("partial", "length"), (None, "stop")]:
            request.return_value = {"choices": [{"finish_reason": reason, "message": {"content": content}}]}
            with self.subTest(content=content), self.assertRaises(dictation.UpstreamError):
                dictation.XAI("key", "model").translate("text")

    @patch("dictation.urllib.request.urlopen")
    def test_upstream_failures_do_not_leak_response(self, open_url):
        for error in [urllib.error.HTTPError("url", 401, "secret", {}, io.BytesIO(b"private transcript")), TimeoutError("private")]:
            open_url.side_effect = error
            with self.subTest(error=error), self.assertRaises(dictation.UpstreamError) as caught:
                dictation.request_json("https://api.x.ai/v1/stt", b"x", "text/plain", key="secret")
            self.assertNotIn("secret", str(caught.exception))
            self.assertNotIn("private", str(caught.exception))

    def test_config(self):
        config = tomllib.loads(Path("examples/voxtype.toml").read_text())
        self.assertFalse(config["whisper"]["translate"])
        self.assertNotIn("post_process", config["output"])
        self.assertEqual(config["profiles"]["translate"]["post_process_timeout_ms"], 30000)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = dictation.ThreadingHTTPServer(("127.0.0.1", 0), dictation.Handler)
        cls.server.xai = dictation.XAI("test-key", "test-model")
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def post(self, path, body, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("POST", path, body, headers or {"Content-Type": "application/json"})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    @patch("dictation.request_json", return_value={"text": "中文原文"})
    def test_plain_mode_never_translates(self, request):
        status, body = self.post("/v1/audio/transcriptions", upload(), {"Content-Type": "multipart/form-data; boundary=input"})
        self.assertEqual((status, body), (200, {"text": "中文原文"}))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], "https://api.x.ai/v1/stt")

    @patch("dictation.XAI.translate", return_value="Meet at three.")
    def test_translation_cli(self, translate):
        result = subprocess.run([sys.executable, "dictation.py", "translate", "--port", str(self.server.server_port)], input="三点见。", text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Meet at three.\n")
        translate.assert_called_once_with("三点见。")

    @patch("dictation.XAI.translate", side_effect=dictation.UpstreamError("API returned HTTP 401"))
    def test_cli_failure_has_no_stdout(self, _translate):
        with redirect_stderr(io.StringIO()):
            result = subprocess.run([sys.executable, "dictation.py", "translate", "--port", str(self.server.server_port)], input="text", text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    @patch("dictation.urllib.request.urlopen")
    def test_protocol_errors_return_sanitized_502(self, open_url):
        for error, during_read in [
            (http.client.BadStatusLine("private transcript"), False),
            (http.client.IncompleteRead(b"private transcript", 100), True),
        ]:
            with self.subTest(error=type(error).__name__):
                open_url.side_effect = None if during_read else error
                open_url.return_value.__enter__.return_value.read.side_effect = error if during_read else None
                logs = io.StringIO()
                with redirect_stderr(logs):
                    status, body = self.post("/translate", b'{"text":"hello"}')
                expected = f"API request failed ({type(error).__name__})"
                self.assertEqual((status, body), (502, {"error": expected}))
                self.assertEqual(logs.getvalue(), expected + "\n")

    def test_reject_bad_requests_without_upstream(self):
        with patch("dictation.request_json") as request:
            for path, body, headers, expected in [
                ("/v1/audio/translations", b"x", {}, 404),
                ("/translate", b'{}', {"Origin": "https://evil.example"}, 403),
                ("/translate", b'{}', {"Host": "evil.example"}, 403),
                ("/translate", b'{}', {"Content-Length": str(dictation.MAX_BODY + 1)}, 413),
                ("/translate", b'{"text":42}', None, 400),
                ("/translate", b'[]', None, 400),
                ("/translate", b'not json', None, 400),
            ]:
                with self.subTest(path=path, body=body, headers=headers):
                    self.assertEqual(self.post(path, body, headers)[0], expected)
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
