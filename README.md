# Omarchy Dictation Cleanup

Hold a key, speak, release: Voxtype transcribes, an LLM tidies the text, and the
result is typed at your cursor. A second key does the same for text you wrote:
copy it, press the key, paste it back as native English.

- **Hold the dictation key:** Whisper transcribes in whatever language you spoke.
  The transcript is rewritten as clean prose in that same language: fillers, false
  starts and punctuation fixed, nothing translated.
- **Hold Right Shift as well:** the same transcript is rendered as native English.
- **Release:** recording stops and Voxtype types the result, with its usual
  clipboard fallback.
- **Press Super + Shift + T:** the text in the clipboard, in any language and with
  any Markdown formatting, is rewritten as native English with the same paragraphs,
  headings, lists, links and code blocks, and put back in the clipboard. A
  notification shows progress and a preview; the clipboard is left untouched if
  the rewrite fails.

Cleanup works with any OpenAI-compatible Chat Completions API: OpenAI, Groq,
OpenRouter, a local Ollama or vLLM server, and so on. The default is Cerebras
(`qwen-3.8-27b`), which adds roughly 150 to 250 ms after transcription; see
[Choose the provider](#choose-the-provider). If the API is unreachable, slow, or
returns anything unusable, the raw transcription is typed instead. Dictation never
silently fails; at worst it is not cleaned up.

The default key is the Copilot key found on recent laptops, which Hyprland sees as
Shift + Meta + F23. See [Change the keys](#change-the-keys) for anything else.

## Requirements

- Omarchy with Lua-based Hyprland configuration and a running Hyprland session.
- Voxtype 1.1 or later with the Whisper engine and profile support. The Vulkan
  build shipped with Omarchy works. `whisper.language = "auto"` is recommended so
  the same key works for every language you speak.
- `curl`, `jq` 1.7 or later, `wl-clipboard` and `notify-send`. All ship with Omarchy.
- An API key for your provider (by default Cerebras, from <https://cloud.cerebras.ai/>).
  Each dictation is one chat completion billed to that account, and the dictated
  text leaves your machine unless the server is local.

## Install

Review this repository, then add and enable the plugin:

```sh
omarchy plugin add https://github.com/krosdai/omarchy-dictation.git --enable
```

The first enable opens a terminal that explains what will change and asks for
confirmation, then for your API key (input hidden). The installer:

1. Copies `voxtype-llm`, its two command names and `polish-clipboard` into
   `~/.local/bin`.
2. Appends a managed block with `[profiles.rephrase]` and `[profiles.translate]` to
   `~/.config/voxtype/config.toml`.
3. Appends a managed block with the dictation and clipboard key bindings to
   `~/.config/hypr/bindings.lua`.
4. Creates `~/.config/voxtype/vocabulary.txt` from the example if you have none.
5. Stores the key in `~/.config/omarchy-dictation/api_key` with owner-only permissions.
6. Restarts Voxtype, reloads Hyprland, checks for configuration errors and confirms
   both profiles are available. If anything fails, every change is reverted.

Both configuration files are backed up under `~/.local/state/omarchy-dictation/`
first. The installer refuses to run if either profile name or any of the chords is
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
  "post_process_timeout_ms": 20000,
  "polish_chord": "SUPER + SHIFT + T",
  "base_url": "https://api.cerebras.ai/v1",
  "model": "qwen-3.8-27b",
  "reasoning_effort": "none"
}
```

`chords` are Hyprland bind chords; several are allowed, and the default lists two
spellings of the Copilot key because Omarchy's default layout exposes the left Meta
key as Alt. `translate_key` is an XKB key name checked with `hl.is_key_down` while
the chord is pressed. `post_process_timeout_ms` is how long Voxtype waits for the
cleanup before typing the raw text. `polish_chord` is the clipboard key; set it to
`null` to leave the clipboard feature out.

## Choose the provider

`base_url`, `model` and `reasoning_effort` in the same `settings.json` select the
LLM. `voxtype-llm` reads them on every call, so changes take effect immediately. Put
the provider's key in `~/.config/omarchy-dictation/api_key` (mode 0600, one line).
When you switch providers, replace the key in that file too; otherwise the old key
is sent to the new provider, which rejects it, and dictation is typed uncleaned.
Re-running the installer validates the settings but prompts for a key only if the
file is missing or empty.

| Provider | `base_url` |
| --- | --- |
| Cerebras (default) | `https://api.cerebras.ai/v1` |
| OpenAI | `https://api.openai.com/v1` |
| Groq | `https://api.groq.com/openai/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Ollama (local) | `http://localhost:11434/v1` |

`model` is any chat model ID the provider lists. Small, fast models suit dictation.
`reasoning_effort` is sent as-is. Use `null` for models that do not reason or that
reject the field. Plain `http` is accepted only for localhost, and the URL may not
carry user info, a query or a fragment. A local server that
needs no key still needs a non-empty key file; any text will do.

The script first asks for a strict JSON-schema reply. If the server rejects the
request (HTTP 400 or 422), it retries once with only the model, messages and token
limit. This works with servers that lack structured outputs, `temperature` or
`reasoning_effort`, at the cost of an extra round trip. Set `reasoning_effort` to
`null` if the log shows the retry on every dictation.

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
printf '## 今天\n\n- 把 clipboard 功能做完了' | voxtype-llm --mode polish
printf 'some draft' | wl-copy && polish-clipboard && wl-paste
```

The last four commands call the API. Logs: `journalctl --user -u voxtype`; the
script reports failures on stderr, prefixed `voxtype-llm:`, and `polish-clipboard`
shows them as notifications.

## Security notes

Dictated and clipboard text is untrusted input to the model. The script sends it
wrapped in `<transcript>` (or `<draft>`) tags with instructions to treat it as data,
neutralises tag-like text inside it, requires a strict JSON reply (`{"text": ...}`)
through the API's `response_format`, and rejects replies that are empty, leak the
delimiter, or grow far beyond the input. Phrases such as "ignore previous
instructions" come back rewritten, not obeyed. This is defence in depth, not a
guarantee; the model still chooses the wording. Whatever is in the clipboard when
you press the polish key is sent to the API, so check it first.

The key is read from a mode-0600 file and never passed on a command line. For
manual use, the script also honours `VOXTYPE_LLM_API_KEY`, `VOXTYPE_LLM_API_KEY_FILE`,
`VOXTYPE_LLM_BASE_URL`, `VOXTYPE_LLM_MODEL`, `VOXTYPE_LLM_SETTINGS_FILE` and
`VOXTYPE_VOCABULARY_FILE`. The systemd user session does not inherit shell
variables, so the files are the default.

## Remove

Disable the plugin, then remove its configuration explicitly:

```sh
omarchy plugin disable krosdai.dictation
/usr/bin/python ~/.config/omarchy/plugins/krosdai.dictation/install.py --uninstall
```

This deletes the managed blocks and the four commands, restarts Voxtype and
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
