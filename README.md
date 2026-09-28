# Omarchy Dictation Cleanup

Hold a key, speak, release: Voxtype transcribes, an LLM tidies the text, and the
result is typed at your cursor.

- **Hold the dictation key:** Whisper transcribes in whatever language you spoke.
  The transcript is rewritten as clean prose in that same language: fillers, false
  starts and punctuation fixed, nothing translated.
- **Hold Right Shift as well:** the same transcript is rendered as native English.
- **Release:** recording stops and Voxtype types the result, with its usual
  clipboard fallback.

Cleanup runs on Cerebras (`qwen-3.8-27b`, OpenAI-compatible Chat Completions) and
adds roughly 150 to 250 ms after transcription. If the API is unreachable, slow, or
returns anything unusable, the raw transcription is typed instead. Dictation never
silently fails; at worst it is not cleaned up.

The default key is the Copilot key found on recent laptops, which Hyprland sees as
Shift + Meta + F23. See [Change the keys](#change-the-keys) for anything else.

## Requirements

- Omarchy with Lua-based Hyprland configuration and a running Hyprland session.
- Voxtype 1.1 or later with the Whisper engine and profile support. The Vulkan
  build shipped with Omarchy works. `whisper.language = "auto"` is recommended so
  the same key works for every language you speak.
- `curl` and `jq` 1.7 or later. Both ship with Omarchy.
- A Cerebras API key from <https://cloud.cerebras.ai/>. Each dictation is one chat
  completion billed to that account, and the dictated text leaves your machine.

## Install

Review this repository, then add and enable the plugin:

```sh
omarchy plugin add https://github.com/krosdai/omarchy-dictation.git --enable
```

The first enable opens a terminal that explains what will change and asks for
confirmation, then for your API key (input hidden). The installer:

1. Copies `voxtype-llm` and its two command names into `~/.local/bin`.
2. Appends a managed block with `[profiles.rephrase]` and `[profiles.translate]` to
   `~/.config/voxtype/config.toml`.
3. Appends a managed block with the key bindings to `~/.config/hypr/bindings.lua`.
4. Creates `~/.config/voxtype/vocabulary.txt` from the example if you have none.
5. Stores the key in `~/.config/cerebras/api_key` with owner-only permissions.
6. Restarts Voxtype, reloads Hyprland, checks for configuration errors and confirms
   both profiles are available. If anything fails, every change is reverted.

Both configuration files are backed up under `~/.local/state/omarchy-dictation/`
first. The installer refuses to run if either profile name or either chord is
already configured by hand; remove those first.

You can also run it directly:

```sh
/usr/bin/python ~/.config/omarchy/plugins/krosdai.dictation/install.py
```

## Change the keys

Create `~/.config/omarchy-dictation/settings.json`, then re-run the installer. It
replaces its managed blocks with the new values.

```json
{
  "chords": ["SUPER + D"],
  "translate_key": "Shift_R",
  "post_process_timeout_ms": 20000
}
```

`chords` are Hyprland bind chords; several are allowed, and the default lists two
spellings of the Copilot key because Omarchy's default layout exposes the left Meta
key as Alt. `translate_key` is an XKB key name checked with `hl.is_key_down` while
the chord is pressed. `post_process_timeout_ms` is how long Voxtype waits for the
cleanup before typing the raw text.

## Vocabulary

`~/.config/voxtype/vocabulary.txt` lists one name or term per line. They are passed
to the model as spellings to prefer whenever the transcript contains something that
sounds like them, so `vox type` becomes `Voxtype`. The file is read on every
dictation; edits take effect immediately.

This fixes spelling after recognition. If Whisper cannot hear a term at all, add the
same list to `whisper.initial_prompt` in the Voxtype config too; that biases
recognition itself and needs `systemctl --user restart voxtype`.

## Verify

```sh
hyprctl binds | grep -B2 -A3 F23           # press and release binds for each chord
voxtype record start --profile __probe__   # "Available profiles: rephrase, translate"
printf 'so um i think we should, we should move it to thursday' | voxtype-rephrase
printf '这个功能挺好用的然后我们下周搞定' | voxtype-translate-en
```

The last two commands call the API. Logs: `journalctl --user -u voxtype`; the
script reports failures on stderr, prefixed `voxtype-llm:`.

## Security notes

Dictated text is untrusted input to the model. The script sends it wrapped in
`<transcript>` tags with instructions to treat it as data, neutralises tag-like
text inside it, requires a strict JSON reply (`{"text": ...}`) through the API's
`response_format`, and rejects replies that are empty, leak the delimiter, or grow
to more than three times the input. Spoken phrases such as "ignore previous
instructions" come back rewritten as speech, not obeyed. This is defence in depth,
not a guarantee; the model still chooses the wording.

The key is read from a mode-0600 file and never passed on a command line. The
script also honours `CEREBRAS_API_KEY`, `CEREBRAS_API_KEY_FILE`, `CEREBRAS_MODEL`,
`CEREBRAS_BASE_URL` and `VOXTYPE_VOCABULARY_FILE` for manual use, but the systemd
user session does not inherit shell variables, which is why the file is the default.

## Remove

Disable the plugin, then remove its configuration explicitly:

```sh
omarchy plugin disable krosdai.dictation
/usr/bin/python ~/.config/omarchy/plugins/krosdai.dictation/install.py --uninstall
```

This deletes the managed blocks and the three commands, restarts Voxtype and
reloads Hyprland. Your other configuration, the API key and the vocabulary file are
left in place. Disabling alone stops the launcher but changes nothing else.

## Develop

```sh
python -m unittest discover -s tests -v
omarchy plugin validate .
```

Tests exercise the installer against temporary directories with the system calls
faked, and the script's syntax and fallback path; nothing touches the desktop or the
API.
