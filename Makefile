PYTHON := $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)

.PHONY: test test-public test-podcast test-tts

test: test-public test-podcast test-tts
	@bash -n install.sh bin/podcast-cn.sh bin/setup-podcast-model.sh bin/setup-podcast-deepseek.sh bin/setup-podcast-qwen.sh bin/tts-cn.sh
	@echo "All tests passed."

test-public:
	@python3 tests/test_public_package.py

test-podcast:
	@$(PYTHON) tests/test_podcast_captions.py
	@$(PYTHON) tests/test_diarization_core.py
	@$(PYTHON) tests/test_podcast_cn.py

test-tts:
	@$(PYTHON) -m unittest \
		tests.test_tts_cn \
		tests.test_tts_backends \
		tests.test_tts_cache
