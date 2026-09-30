# Omarchy Voice Input

Standalone hold-to-dictate for Omarchy. PipeWire captures your microphone,
ElevenLabs Scribe v2 Realtime transcribes it, and an OpenAI-compatible text model
cleans up the result before it is typed at your cursor. **Voxtype is not required.**

- Hold the dictation key to speak; release it to finish and insert the result.
- Keep the spoken language, or hold Right Shift to render this utterance as English.
- A passive, click-through Quickshell HUD shows the microphone waveform and live
  subtitles without stealing keyboard focus. Interim subtitles can be revised;
  only committed recognition results are used for final output.
- Click the microphone bar widget to open settings: default mode, microphone,
  a local microphone test, text provider, model, and credential setup.
- Copy a draft, press Super + Shift + T, then paste: clipboard polishing rewrites
  it as natural English while preserving Markdown structure.
- Vocabulary correction uses your preferred spellings during text cleanup.

Voice conversation and TTS are **not implemented yet**. The settings panel marks
them as unavailable; no synthetic waveform or pretend AI reply is shown.

## Requirements

- Omarchy with Quickshell and Lua-based Hyprland configuration (developed against
  Omarchy 4.0.4, Quickshell 0.3.1 and Qt 6.11).
- Python 3.11+ with venv/pip. Setup provisions a private venv with
  `websockets==15.0.1`; it does not modify system Python packages.
- PipeWire tools: `pw-record`, `pw-dump`, `wpctl`.
- `wtype`, `wl-copy`, `wl-paste`, `curl`, `jq` 1.7+, and `notify-send`.
- An ElevenLabs API key with Scribe Realtime access, plus a key for your text
  provider (Cerebras by default).

Audio is uploaded to ElevenLabs. Dictated and clipboard text goes to your selected
text provider, unless it is local. Both services may bill your account. The
microphone test only reads local PCM and never connects to either provider.

## Install

After this version is published, review the code and add its GitHub source:

```sh
omarchy plugin add https://github.com/krosdai/omarchy-dictation.git --enable
```

For an unpublished build, copy the plugin files into a new
`~/.config/omarchy/plugins/krosdai.dictation/` directory, rescan with
`omarchy-shell shell rescanPlugins`, then enable with
`omarchy plugin enable krosdai.dictation`. Do not overwrite an existing plugin;
use the isolated development checks below until you are ready to migrate.
`plugin add` clones Git history and does not include uncommitted local edits.

Enabling starts the plugin service and, if setup is needed, opens a terminal asking
for confirmation. Setup creates private credentials, a Python runtime and command
launchers, then backs up and adds managed bindings to `~/.config/hypr/bindings.lua`.
It reloads Hyprland and checks for configuration errors. A failed reload restores
the previous bindings and launchers. Credentials and the runtime are retained.
Setup refuses to overwrite a personal command or a modified plugin launcher;
move the conflicting command yourself before retrying.

The default dictation key is the Copilot key (`F23`); modifier spellings are included
for different keyboard layouts. If you use another key, edit the settings below.
The bar widget is optional; use Omarchy's bar customization to add
`krosdai.dictation`. Settings can also be opened through shell IPC:

```sh
omarchy-shell dictation open
```

To run setup directly, use the `install.py` in your registered checkout:

```sh
/usr/bin/python /absolute/path/to/omarchy-dictation/install.py
```

### Upgrading from the Voxtype-based version

Setup removes this plugin's old managed TOML profiles and unchanged legacy commands,
and copies your old Voxtype vocabulary if the new vocabulary does not exist.
Modified legacy scripts and unproven symlinks are preserved. It does **not** stop,
uninstall or reconfigure Voxtype beyond those owned profiles. Disable any legacy
dictation service or remove overlapping personal hotkeys before using the new
plugin; two recorders must not own the same key. The installer never starts or
queries Voxtype.

## Settings and credentials

Files live in `~/.config/omarchy-dictation/` (or your `XDG_CONFIG_HOME`):

| File | Purpose |
| --- | --- |
| `settings.json` | Mode, microphone, keys, timeout and text provider |
| `elevenlabs_api_key` | Recognition key; owner-only permissions |
| `api_key` | Text-provider key; owner-only permissions |
| `vocabulary.txt` | Preferred spellings, one term per line |

The settings panel saves mode, microphone and provider options. Key changes require
editing the file and re-running setup, which replaces only its managed bindings.

```json
{
  "chords": ["SUPER + D"],
  "translate_key": "Shift_R",
  "polish_chord": "SUPER + SHIFT + T",
  "post_process_timeout_ms": 20000,
  "mode": "rephrase",
  "microphone": "auto",
  "base_url": "https://api.cerebras.ai/v1",
  "model": "qwen-3.8-27b",
  "reasoning_effort": "none"
}
```

`mode` is `rephrase` or `translate`. `microphone` is `auto`, a PipeWire input node
name or an object serial. The panel lists available input nodes. A muted microphone
is rejected; the plugin never unmutes it for you. Release handling ignores modifier
changes while the key is held. Escape cancels an active operation and is also passed
to the focused application; it does nothing to the plugin when idle.

Provider and model changes apply to the next operation. **When changing providers,
update the key too**: otherwise the existing key is sent to the new endpoint.
Use the panel's secure-terminal button or:

```sh
/usr/bin/python /absolute/path/to/omarchy-dictation/install.py --credentials
```

Any OpenAI-compatible Chat Completions endpoint can be used. Examples:

| Provider | `base_url` |
| --- | --- |
| Cerebras | `https://api.cerebras.ai/v1` |
| OpenAI | `https://api.openai.com/v1` |
| Groq | `https://api.groq.com/openai/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Ollama | `http://localhost:11434/v1` |

Select a model available to your account. Set `reasoning_effort` to `null` to omit
it. Plain HTTP is accepted only for localhost; credentials, query strings and
fragments are rejected in the URL. A keyless local endpoint still requires a
nonempty key file; any placeholder will do. Servers rejecting structured-output
options with HTTP 400/422 are retried once without optional request fields.

Vocabulary edits apply immediately during cleanup, not during audio recognition.
The waveform is measured from the actual captured PCM; display gain does not
change the audio sent to the recognizer.

## Failure behavior and privacy

- Recognition failure, timeout or cancellation never inserts a partial transcript.
- Cleanup failure falls back to the **complete committed** original transcript,
  with a visible warning. Translation failure can therefore insert the original
  language, not English.
- If an empty `wtype` capability probe fails, the result is copied for manual paste.
  Failure after typing starts never retries or copies automatically: insertion may
  already be partial, and retrying could duplicate it.
- Clipboard-polish failure leaves the clipboard unchanged.
- Disabling/unloading the service closes its command stream, stops capture and
  reaps its child processes. Text delivery already in progress finishes within
  its timeout rather than being interrupted. Hotkey launchers remain until uninstall.
- Audio is streamed, not saved locally. Transcripts remain in memory/UI, not in
  a local history file. Provider-side retention is controlled by your providers.
- Keys never enter QML, IPC messages or command arguments. Cleanup treats dictated
  text as data and validates the structured reply; model wording is not guaranteed.

The backend has one private Unix socket under
`$XDG_RUNTIME_DIR/omarchy-dictation/`. Multiple service instances cannot record
concurrently. For troubleshooting, inspect the Quickshell log and:

```sh
omarchy-dictation status
omarchy-dictation test       # local-only microphone test; run again to stop
omarchy-dictation cancel
printf 'so um we should move it to thursday' | dictation-llm --mode rephrase
printf '这个功能下周完成' | dictation-llm --mode translate
```

The last two commands contact the configured text provider. Manual script use
supports `DICTATION_LLM_API_KEY`, `DICTATION_LLM_API_KEY_FILE`,
`DICTATION_LLM_BASE_URL`, `DICTATION_LLM_MODEL`, `DICTATION_LLM_SETTINGS_FILE` and
`DICTATION_VOCABULARY_FILE`. Normal plugin use takes configuration from its files.

## Remove

```sh
omarchy plugin disable krosdai.dictation
/usr/bin/python /absolute/path/to/omarchy-dictation/install.py --uninstall
```

Uninstall removes owned bindings and unchanged command launchers, then reloads
Hyprland. Personal edits, settings, keys, vocabulary, runtime and backups remain.

## Develop and verify

```sh
python -m venv .venv
.venv/bin/pip install websockets==15.0.1 ruff
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
omarchy plugin validate .
```

Tests use temporary configuration, fake desktop tools and local WebSocket/HTTP
servers. They do not record your microphone, change desktop settings or contact
cloud providers. `tests/render_ui.py` additionally renders actual QML states and
the production service in an isolated Wayland compositor:

```sh
XDG_RUNTIME_DIR=/path/to/private/runtime WAYLAND_DISPLAY=wayland-1 \
  .venv/bin/python tests/render_ui.py --artifacts /path/to/screenshots
```

Run that only against a disposable headless compositor with `grim` and `wtype`;
it deliberately sends an Escape key to that isolated session. A final real-device
and provider acceptance test is still needed before treating a build as released.
