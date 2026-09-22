# Omarchy Dictation

Push-to-talk dictation for Omarchy/Hyprland using one existing Voxtype daemon:

- **Hold F9:** transcribe speech in its original language.
- **Hold Shift+F9:** transcribe, then translate to English.
- **Release F9:** stop recording and type at the cursor, with Voxtype's clipboard fallback.

Wait for the previous utterance's output to finish and Voxtype to return to idle
before pressing F9 again, with or without Shift. Do not queue recordings while
transcription or translation is pending.

Audio goes to xAI's `grok-voice-transcribe-2.0`; translation uses `grok-4.6` by
default. This is a standalone Voxtype integration, not an Amp plugin. Requires
Python 3.11+, Voxtype with remote Whisper and profiles (tested against 1.0.1),
and the existing Wayland typing/clipboard tools. No Python packages are required.

## Try the adapter

Set `SPACEXAI_API_KEY` or `XAI_API_KEY` in your environment. The former takes
precedence. Never put the key in this repository or Hyprland bindings.

```sh
make test
python3 dictation.py serve
```

In another terminal:

```sh
curl -fsS http://127.0.0.1:8765/health
printf '明天下午三点开会。' | python3 dictation.py translate
curl -fsS http://127.0.0.1:8765/v1/audio/transcriptions -F file=@sample.wav
```

These last two commands send text/audio to xAI and incur API usage charges.
`/health` checks the local process, not API credentials. To change translation
models, set `DICTATION_TRANSLATION_MODEL` before starting the adapter.

## Install and enable

`make install` copies the program and a user service; it does not enable anything
or change Voxtype/Hyprland. Stop the foreground adapter before enabling the service.

```sh
make install
install -d -m 700 ~/.config/omarchy-dictation
(umask 077; printf 'SPACEXAI_API_KEY=%s\n' "${SPACEXAI_API_KEY:-$XAI_API_KEY}" > ~/.config/omarchy-dictation/environment)
systemctl --user daemon-reload
systemctl --user enable --now omarchy-dictation.service
```

The environment file persists your key in plaintext with owner-only permissions.
Systemd does not inherit arbitrary variables from your interactive shell.

Back up `~/.config/voxtype/config.toml` and `~/.config/hypr/bindings.lua` first.
Merge [the Voxtype settings](examples/voxtype.toml) into existing sections rather
than appending duplicate TOML tables. Preserve audio, OSD, and other output
preferences. Remove any global `[output.post_process]` translation command;
translation belongs only in `[profiles.translate]`. The profile command is run
through Voxtype's shell, which expands `~`.

Add [the Lua bindings](examples/bindings.lua) to your personal bindings file after
Omarchy's defaults. For older Hyprland configurations using `.conf`, the equivalent is:

```ini
unbind = , F9
unbind = SHIFT, F9
bind = , F9, exec, voxtype record start
bind = SHIFT, F9, exec, voxtype record start --profile translate
bindrt = , F9, exec, voxtype record stop
bindrt = SHIFT, F9, exec, voxtype record stop
```

Then restart Voxtype with `systemctl --user restart voxtype` and reload Hyprland
with `hyprctl reload`. Logs: `journalctl --user -u omarchy-dictation -u voxtype`.
To roll back, restore your two configuration backups, restart Voxtype/reload
Hyprland, and run `systemctl --user disable --now omarchy-dictation`.

## Behavior and limitations

- The adapter listens only on `127.0.0.1:8765`. It rebuilds Voxtype's multipart
  upload with the explicit xAI model first and the WAV file last. Whisper-only
  fields are dropped; xAI detects the language. Uploads are limited to 8 MiB
  (a 60-second, 16-kHz mono PCM recording is approximately 1.9 MiB).
- Translation reads stdin and prints only translated text. Failures exit nonzero.
  **Voxtype falls back to the original transcript on translation failure**, including
  its 30-second timeout. Shift+F9 therefore does not guarantee English on failure.
- **Voxtype 1.0.1 loses the selected profile when the 60-second audio cap stops a
  recording automatically.** Release F9 before the cap; otherwise Shift+F9 can
  output untranslated text. The adapter cannot recover a profile Voxtype discarded.
- **Use one utterance at a time.** Voxtype 1.0.1 stores the selected profile in a
  shared override file and reads it after transcription. A new Shift+F9 press
  while busy can change the previous utterance's mode even though the new start
  is ignored. Waiting for completed output and idle is the short-term workaround;
  the adapter does not enforce this or maintain a recording queue. A failed
  recording start can also leave a stale profile: waiting alone does not clear it.
  If capture fails, resolve the failure and check profile state before resuming;
  do not assume the next plain F9 recording will be untranslated.
- Both F9 release bindings use `transparent = true` (`t` in legacy config) to
  prevent shadowing when Shift is released while F9 remains held. Verify both
  release orders on your desktop before relying on this. Modifier waiting requires
  readable `/dev/input` devices; Voxtype silently skips it without access. Release
  Shift promptly: with device access, the example waits up to two seconds, then
  falls back to the clipboard if a modifier is still held.
- Audio and translated text are sent to xAI, not processed offline. The adapter
  does not save recordings/transcripts or log request/response bodies. Voxtype and
  clipboard history have their own retention behavior. Local processes can call
  the adapter using your API account; it is not intended for untrusted multiuser
  hosts. Browser-origin requests are rejected and no CORS access is granted.

Before daily use, test Mandarin, English, mixed-language speech, and proper names
in a scratch editor. Test releasing Shift before and after F9, clipboard fallback,
and a failed translation. Measure release-to-output latency on your own audio.
Offline tests cover HTTP/multipart contracts and the translation filter, not
physical keys, microphone quality, OSD rendering, or cursor placement.

API reference: [xAI speech-to-text](https://docs.x.ai/developers/model-capabilities/audio/speech-to-text).
