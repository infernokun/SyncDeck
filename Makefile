.PHONY: test lint build zip clean deploy

DECK_HOST ?= deck@steamdeck
PLUGIN_DIR ?= ~/homebrew/plugins/SyncDeck

test:
	python3 -m unittest discover -s tests -v

lint:
	python3 -m compileall -q main.py py_modules scripts

# npm works with the checked-in lockfile; pnpm is fine too if you prefer it.
build:
	npm install --no-audit --no-fund && npm run build

# Decky's "Install from ZIP" expects a single top-level folder named after the plugin.
zip: build
	rm -rf out/pkg out/SyncDeck.zip && mkdir -p out/pkg/SyncDeck
	cp -r plugin.json package.json main.py LICENSE README.md dist py_modules out/pkg/SyncDeck/
	find out/pkg -name __pycache__ -type d -exec rm -rf {} +
	cd out/pkg && python3 -m zipfile -c ../SyncDeck.zip SyncDeck
	rm -rf out/pkg

clean:
	rm -rf dist node_modules out
	find . -name __pycache__ -type d -exec rm -rf {} +

# Copy the plugin to a Deck over SSH. Requires SSH enabled and a plugins dir
# writable by the deck user (it is root-owned by default; prefer `make zip`).
deploy: build
	rsync -av --delete \
		--exclude node_modules --exclude .git --exclude tests --exclude .github \
		./ $(DECK_HOST):$(PLUGIN_DIR)/
	ssh $(DECK_HOST) 'sudo systemctl restart plugin_loader'
