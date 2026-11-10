PYTHON := $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)

.PHONY: test test-public test-podcast test-tts test-extract-display test-continue-ux test-player-listen-note-ui shengjian-source-gate shengjian-before-claim-ready

test: test-public test-podcast test-tts test-extract-display test-continue-ux test-player-listen-note-ui shengjian-source-gate
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
		if [ "$${SHENGJIAN_REQUIRE_SWIFTC:-}" = 1 ]; then \
			echo "swiftc not found; 装机/宣称可听前不能跳过提炼探针" >&2; \
			exit 1; \
		fi; \
		echo "swiftc not found; skip extract display checks"; \
	else \
		"$$SWIFTC" -parse-as-library -o /tmp/extract-display-checks \
			ios/PrivateListen/PrivateListen/ExtractionModels.swift \
			ios/PrivateListen/PrivateListen/ExtractSchemaMap.swift \
			ios/PrivateListen/PrivateListen/MockExtractor.swift \
			ios/PrivateListen/PrivateListen/ExtractDisplay.swift \
			ios/PrivateListen/PrivateListenTests/ExtractDisplayChecks.swift && \
		/tmp/extract-display-checks; \
	fi

# 资料库「继续播放」与筛选条：短播回库后是刚听的那集、只留一条、左滑、听这段不写 ListenMark。
test-continue-ux:
	@SWIFTC=$$(command -v swiftc || true); \
	if [ -z "$$SWIFTC" ] && command -v xcrun >/dev/null 2>&1; then \
		SWIFTC=$$(xcrun --find swiftc 2>/dev/null || true); \
	fi; \
	if [ -z "$$SWIFTC" ]; then \
		if [ "$${SHENGJIAN_REQUIRE_SWIFTC:-}" = 1 ]; then \
			echo "swiftc not found; 装机/宣称可听前不能跳过继续播放探针" >&2; \
			exit 1; \
		fi; \
		echo "swiftc not found; skip continue ux probe"; \
	else \
		"$$SWIFTC" -parse-as-library -o /tmp/continue-ux-probe \
			ios/PrivateListen/PrivateListen/ContinueListening.swift \
			ios/PrivateListen/PrivateListenTests/ContinueUXProbe.swift && \
		/tmp/continue-ux-probe && \
		"$$SWIFTC" -parse-as-library -o /tmp/library-list-probe \
			ios/PrivateListen/PrivateListen/LibraryStore.swift \
			ios/PrivateListen/PrivateListen/Episode.swift \
			ios/PrivateListen/PrivateListen/DiscoverCatalog.swift \
			ios/PrivateListen/Shared/InboxDrop.swift \
			ios/PrivateListen/PrivateListen/ContinueListening.swift \
			ios/PrivateListen/PrivateListen/LibraryShelf.swift \
			ios/PrivateListen/PrivateListenTests/LibraryListProbe.swift && \
		/tmp/library-list-probe; \
	fi

# 播放到终点再点播放应重头开播；长听后感重开/首焦应落到文末。无 swiftc 时跑 Python 镜像表。
test-player-listen-note-ui:
	@$(PYTHON) -m unittest tests.test_player_listen_note_ui_policy
	@SWIFTC=$$(command -v swiftc || true); \
	if [ -z "$$SWIFTC" ] && command -v xcrun >/dev/null 2>&1; then \
		SWIFTC=$$(xcrun --find swiftc 2>/dev/null || true); \
	fi; \
	if [ -z "$$SWIFTC" ]; then \
		echo "swiftc not found; python mirror already covered player/listen-note UI policy"; \
	else \
		"$$SWIFTC" -parse-as-library -o /tmp/player-playback-policy-probe \
			ios/PrivateListen/PrivateListen/ContinueListening.swift \
			ios/PrivateListen/PrivateListen/PlayerPlaybackPolicy.swift \
			ios/PrivateListen/PrivateListenTests/PlayerPlaybackPolicyProbe.swift && \
		/tmp/player-playback-policy-probe && \
		"$$SWIFTC" -parse-as-library -o /tmp/listen-note-editor-policy-probe \
			ios/PrivateListen/PrivateListen/ListenNoteEditorPolicy.swift \
			ios/PrivateListen/PrivateListenTests/ListenNoteEditorPolicyProbe.swift && \
		/tmp/listen-note-editor-policy-probe; \
	fi

# 声间装机 / 宣称可听之前必须跑（没有手机 UI 自动化时，单测 + 源码/dylib 字符串即门禁）。
# 继续播放短播旅程、播放页听后感仍在提炼前、mock 不得写成 ready。没有 swiftc 则失败，不能跳过。
shengjian-before-claim-ready:
	@SHENGJIAN_REQUIRE_SWIFTC=1 $(MAKE) test-continue-ux test-extract-display test-player-listen-note-ui shengjian-source-gate
	@echo "声间：装机/宣称可听前检查通过（继续播放旅程 + 听后感 + 终点重播/文末追加 + mock 不冒充 ready）。"

shengjian-source-gate:
	@bash attention/shengjian-pre-claim-ready.sh
