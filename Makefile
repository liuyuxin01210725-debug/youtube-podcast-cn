PYTHON := $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)

.PHONY: test test-public test-podcast test-tts

test: test-public test-podcast test-tts
	@$(PYTHON) -m unittest tests.test_studio_engine tests.test_studio_server tests.test_studio_inbox tests.test_studio_books tests.test_studio_progress tests.test_studio_push_iphone
	@bash -n install.sh bin/podcast-cn.sh bin/setup-podcast-diarization.sh bin/setup-podcast-model.sh bin/setup-podcast-deepseek.sh bin/setup-podcast-qwen.sh bin/tts-cn.sh bin/setup-studio.sh bin/中文播客工作台.command
	@echo "All tests passed."

test-public:
	@python3 tests/test_public_package.py

test-podcast:
	@$(PYTHON) tests/test_podcast_captions.py
	@$(PYTHON) tests/test_diarization_core.py
	@$(PYTHON) tests/test_diarization_cache.py
	@$(PYTHON) tests/test_diarization_runtime.py
	@$(PYTHON) tests/test_diarization_regression.py
	@$(PYTHON) tests/test_temporary_audio.py
	@$(PYTHON) tests/test_podcast_cn.py

test-tts:
	@$(PYTHON) -m unittest \
		tests.test_tts_cn \
		tests.test_tts_backends \
		tests.test_tts_cache
