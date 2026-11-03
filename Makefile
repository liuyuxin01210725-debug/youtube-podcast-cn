PYTHON := $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)

.PHONY: test test-public test-podcast test-tts test-extract-display

test: test-public test-podcast test-tts test-extract-display
	@$(PYTHON) -m unittest tests.test_studio_engine tests.test_studio_server tests.test_studio_inbox tests.test_studio_books tests.test_studio_ads tests.test_studio_progress tests.test_studio_push_iphone tests.test_studio_lenses tests.test_studio_articles tests.test_studio_extract
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

# 播放页「点星开始提炼」与已就绪要点的竞态。无 swiftc 时跳过（Xcode 里用 xcrun swiftc）。
test-extract-display:
	@SWIFTC=$$(command -v swiftc || true); \
	if [ -z "$$SWIFTC" ] && command -v xcrun >/dev/null 2>&1; then \
		SWIFTC=$$(xcrun --find swiftc 2>/dev/null || true); \
	fi; \
	if [ -z "$$SWIFTC" ]; then \
		echo "swiftc not found; skip extract display checks"; \
	else \
		"$$SWIFTC" -parse-as-library -o /tmp/extract-display-checks \
			ios/PrivateListen/PrivateListen/ExtractionModels.swift \
			ios/PrivateListen/PrivateListen/ExtractSchemaMap.swift \
			ios/PrivateListen/PrivateListen/ExtractDisplay.swift \
			ios/PrivateListen/PrivateListenTests/ExtractDisplayChecks.swift && \
		/tmp/extract-display-checks; \
	fi
