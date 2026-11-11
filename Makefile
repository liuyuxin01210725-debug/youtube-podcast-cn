PYTHON := $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)

.PHONY: test test-public test-podcast test-tts test-extract-display test-continue-ux test-point-listen test-player-listen-note-ui test-books shengjian-source-gate shengjian-before-claim-ready

test: test-public test-podcast test-tts test-extract-display test-continue-ux test-point-listen test-player-listen-note-ui test-books shengjian-source-gate
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

test-player-listen-note-ui:
	@$(PYTHON) -m unittest discover -s tests -p test_player_listen_note_ui_policy.py
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


# 点段跳听：实际本地音频绑定 + PL-D* + seek/播放完成边界；AVFoundation 验证需要 macOS。
test-point-listen:
	@if [ "$$(uname -s)" != Darwin ] || ! command -v swiftc >/dev/null 2>&1; then \
		if [ "$${SHENGJIAN_REQUIRE_SWIFTC:-}" = 1 ]; then \
			echo "point-listen checks require macOS + swiftc; 装机门禁不能跳过" >&2; exit 1; \
		fi; \
		echo "skip point-listen audio checks (requires macOS + swiftc)"; \
	else \
		swiftc -parse-as-library -o /tmp/point-listen-checks \
		ios/PrivateListen/PrivateListen/ExtractionModels.swift \
		ios/PrivateListen/PrivateListen/ExtractSchemaMap.swift \
		ios/PrivateListen/PrivateListen/MockExtractor.swift \
		ios/PrivateListen/PrivateListen/ExtractDisplay.swift \
		ios/PrivateListen/PrivateListen/ContinueListening.swift \
		ios/PrivateListen/PrivateListen/PointListening.swift \
		ios/PrivateListen/PrivateListenTests/PointListenChecks.swift && \
		/tmp/point-listen-checks; \
	fi

# 声间装机 / 宣称可听前必须跑。没有平台/编译器则失败，不能跳过；真机 UI 四连另验。
shengjian-before-claim-ready:
	@SHENGJIAN_REQUIRE_SWIFTC=1 $(MAKE) test-continue-ux test-extract-display test-point-listen test-player-listen-note-ui test-books shengjian-source-gate
	@echo "声间：装机/宣称可听前检查通过（继续播放旅程 + 听后感 + mock 不冒充 ready）。"

shengjian-source-gate:
	@bash attention/shengjian-pre-claim-ready.sh

# Isolated, no-provider book tests; native IO runs only against a self-authored draft package.
test-books:
	@$(PYTHON) tests/test_book_integration.py
	@if [ "$$(uname -s)" != Darwin ] || ! command -v swiftc >/dev/null 2>&1; then \
		if [ "$${SHENGJIAN_REQUIRE_SWIFTC:-}" = 1 ]; then echo "book native probes require macOS + swiftc" >&2; exit 1; fi; \
		echo "skip book native probes (requires macOS + swiftc)"; \
	else \
		tmp=$$(mktemp -d "$${TMPDIR:-/tmp}/shengjian-book-probe.XXXXXX") && \
		package=$$(PYTHONPATH=scripts $(PYTHON) tests/book_import_fixture.py "$$tmp") && \
		swiftc -parse-as-library ios/PrivateListen/PrivateListen/BookProgress.swift ios/PrivateListen/PrivateListenTests/BookProgressProbe.swift -o "$$tmp/progress" && "$$tmp/progress" && \
		swiftc -parse-as-library ios/PrivateListen/PrivateListen/BookProgress.swift ios/PrivateListen/PrivateListen/BookStore.swift ios/PrivateListen/PrivateListenTests/BookImportProbe.swift -o "$$tmp/import" && "$$tmp/import" "$$package" "$$tmp/documents"; \
	fi
