# Developer entry points. Users do not need this file: they run ./zfs-salvage from a release bundle.
# See CONTRIBUTING.md for what each target needs.

GITHUB_REPO      ?= simonbuehler/zfs-salvage
FIXTURES_RELEASE ?= $(shell cat tests/fixtures/RELEASE)
FIXTURES_URL     ?= https://github.com/$(GITHUB_REPO)/releases/download/$(FIXTURES_RELEASE)/fixtures.tar
PYTHON           ?= python3

.PHONY: help build portable test fixtures test-full matrix fixtures-regen fixtures-pack bundle clean

help:
	@echo "make build           compile zscan and libzsalvage.so with the local gcc (fast, for development)"
	@echo "make test            unit and integration tests (no root, no Docker; tests that need fixture pools skip)"
	@echo "make fixtures        download the fixture pools ($(FIXTURES_RELEASE), about 30 MB)"
	@echo "make test-full       test + the pipeline on every fixture pool + the end-to-end test through the web API"
	@echo "make matrix          regenerate the capability matrix in README.md from the pipeline results"
	@echo "make fixtures-regen  build the fixture pools again (ZFS kernel module, or the test VM: Docker + KVM)"
	@echo "make portable        build the release binaries for glibc 2.17 (Docker)"
	@echo "make bundle          release tarball with the portable binaries -> build/"

build:
	gcc -O2 -Wall -Wextra -o tools/zscan/zscan tools/zscan/zscan.c
	gcc -O2 -Wall -Wextra -shared -fPIC -o tools/libzsalvage/libzsalvage.so tools/libzsalvage/zsalvage.c

portable:
	tools/build-portable.sh

test:
	$(PYTHON) -W error::ResourceWarning -m unittest discover -s tests

fixtures:
	@if curl -fsI -o /dev/null "$(FIXTURES_URL)"; then tests/fetch-fixtures.sh "$(FIXTURES_URL)"; \
	elif command -v gh >/dev/null; then \
		tmp="$$(mktemp -d)" && gh release download "$(FIXTURES_RELEASE)" --repo "$(GITHUB_REPO)" --pattern fixtures.tar --dir "$$tmp" \
		&& tests/fetch-fixtures.sh "$$tmp/fixtures.tar"; rc=$$?; rm -rf "$$tmp"; exit $$rc; \
	else echo "cannot download $(FIXTURES_URL) (and no gh for a private repository)"; exit 1; fi

test-full: build test
	tests/run.sh
	tests/e2e-local.sh

matrix: build
	tests/run.sh --matrix
	dev/gen-matrix.py --write

fixtures-regen:
	@if [ "$$(id -u)" = 0 ] && modprobe zfs 2>/dev/null; then tests/make-fixtures.sh; \
	else tests/vm/run.sh tests/make-fixtures.sh; fi

fixtures-pack:
	tests/pack-fixtures.sh

bundle: portable
	dev/make-bundle.sh

clean:
	rm -rf build tools/zscan/zscan tools/libzsalvage/libzsalvage.so
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
