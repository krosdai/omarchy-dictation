.PHONY: test install

test:
	python3 -m unittest discover -s tests -v

install:
	install -Dm644 dictation.py "$(HOME)/.local/share/omarchy-dictation/dictation.py"
	install -Dm644 examples/omarchy-dictation.service "$(HOME)/.config/systemd/user/omarchy-dictation.service"
	@echo "Files installed; configure the environment and Voxtype before enabling the service."
