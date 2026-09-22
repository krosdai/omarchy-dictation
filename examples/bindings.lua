-- Add to ~/.config/hypr/bindings.lua after Omarchy defaults.
hl.unbind("F9")
hl.unbind("SHIFT + F9")
o.bind("F9", "Dictate original language", "voxtype record start")
o.bind("SHIFT + F9", "Dictate in English", "voxtype record start --profile translate")
-- Keep stop bindings active when Shift is released while F9 remains held.
o.bind("F9", "Stop dictation", "voxtype record stop", { release = true, transparent = true })
o.bind("SHIFT + F9", "Stop dictation", "voxtype record stop", { release = true, transparent = true })
