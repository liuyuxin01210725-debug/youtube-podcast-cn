# Offline book packages — bounded first implementation

This is a separate book job and receiving boundary, not a podcast Episode adapter or an automatic production queue. It reuses `studio_books`, the strict translation/checkpoint client, existing single-narrator `TTSBackend`, the shared iOS player and the existing note editor. It does not execute podcast ad removal, diarization, rendering, or the publisher.

## Workflow (operator/assistant, not developer work delegated to the reader)

1. Import a self-authored or explicitly authorized English EPUB/plain text into a **new** job with a lawful-source statement, applicable region and separate external-processing permission. Purchased possession alone is not permission to send text to a service. No DRM removal, library scrape, private upload or automatic book acquisition is provided.
2. Inspect `source.json`: OPF spine order, every manifest XHTML, nonspine note links, resource/DOM/fragment anchors, block inventory, raw source hash and extraction revision. EPUB listening chapters currently follow XHTML resource boundaries, not arbitrary publisher TOC nesting. Navigation-only text is excluded. Images, formulas, CSS/generated/visibility semantics, complex lists and nested layouts are explicitly unresolved/partial; unsafe active/encrypted resources are rejected. A single plain paragraph inside a footnote wrapper is supported. PDF/OCR, rich Markdown and EPUB rendering are not implemented.
3. Verify the already-authorized prepaid account/current model and price ceiling **before** running a real job. Supply a current allowance with provider/model, evidence, expiry, request/character/output-token ceilings, conservative CNY token rates and a total cost cap. The allowance is an operator assertion, not an automatic bank/quota audit or proof that a subscription includes API credit. Never commit its account evidence or secrets. The running service/default model/fees configuration is untouched.
4. `book_pipeline.py translate` translates every source block through the existing strict-ID client/checkpoint, glossary and previous-block source/target context. It does not summarize. Output-token truncation, bad IDs, unknown completion or a lost request stop the job. Request IDs/actual usage/cache hashes are recorded where the provider supplies them. `submitted`, `uncertain` and reconciliation reservations are not silently released or retried; `status` is local status, **not** a provider refund/query guarantee.
5. Compare the actual source and translation block by block. `review` accepts only source/target hashes and exact extraction/translation revisions with reviewer and evidence. `fixture_only` cannot grant the faithful-translation gate. A separate `source-review.json` is needed to pass source completeness; a clean parser inventory alone is insufficient.
6. `synthesize` reuses existing Edge single-narrator synthesis, sequentially and without job-level retry. The paid-TTS book cost adapter is deliberately blocked until separately validated. The Edge path has no paid account/key attached; default zero SDK usage is **not** a measured zero bill or unlimited provider quota. Request/character caps and provider-failure stopping still apply. Translation, narrated text and audio are separate revisions. Footnotes are labelled at chapter end; plain table row/cell separators are retained. Timing is measured fragment timing, not sentence/word alignment.
7. `package` writes an immutable revision directory with an allowlisted asset/hash manifest. All three gates are independent: declared-source coverage, faithful translation and actual listening. Decode/signal checks alone cannot pass listening. A bound `listening-review.json` is required. `declared_scope_verified` concerns the declared file scope; `whole_book_verified` additionally requires explicit edition/publication completeness evidence. Earlier drafts remain available, not overwritten.
8. On iOS, use the existing Library menu **导入书包（离线目录）**. Raw EPUB translation is an isolated Mac-side operation in this slice, not a hidden mobile bill. Books live under `Documents/Books`, with separate `book_state.v1.json` / `book_notes.v1.json`; no Episode/index/old ListenMark rewrite is performed. Shared playback uses a typed book reference rather than a fake Episode. There is one Continue winner across podcasts and books, based on actual listening, not browsing or writing notes. Book and chapter notes retain their original package revision. A newly imported revision does not select itself over the old one or transplant old seconds/notes.

## Commands and tests

Use the existing project Python environment; no additional packages are required. `python scripts/book_pipeline.py --help` lists explicit `import`, `translate`, `review`, `synthesize`, `package`, `cancel`, `status` commands. Import/package/status do not require model generation. Real translate/synthesize commands require current, evidenced allowances and authorized content; examples are intentionally not automatic purchase/call recipes.

`make test-books` runs self-authored structural/adversarial fixtures and, on macOS, native progress and isolated folder-import probes. `make test` includes these plus existing podcast regressions. A native probe Documents directory is not the phone library. The exact signed app can be scanned with `SHENGJIAN_APP=... make shengjian-source-gate`.

Deferred: automatic publisher-TOC nesting, OCR/math interpretation, migration of old text anchors, cross-device book state/notes sync, word/sentence alignment, chapter auto-advance, and book insights. A real phone baseline union incremental check and self-run podcast/book playback/Continue/note journey remain mandatory before any claim of phone readiness. A build/signature or mocked test never substitutes for that journey.


## Bounded preinstall regression (offline)
`make test-books` includes malformed resource, cache-integrity, interrupted publication and
submitted/uncertain reservation tests. Four additional real-response recovery cases run only
with `BOOK_REAL_CACHE_JOB` explicitly pointing to an authorized existing sample job; they copy
it to a temporary directory, patch provider HTTP to fail, and never issue a model/TTS request.
Keep the OS network-deny sandbox too. Missing authorization is not permission to create a new job.

`make test-book-preinstall BOOK_JOURNEY_PACKAGE=<authorized-package-directory>
BOOK_JOURNEY_DRAFT=<independent-extraction-draft-directory>` compiles the actual BookStore and
BookProgress components into a fresh Documents-only probe. It exercises duplicate/racing imports,
prepublication rejection, persisted long notes, revision loss/restoration and typed Continue policy.
The selected unavailable revision must not silently fall back to a different edition. The
receiver checks completeness-claim evidence shape/binding; it cannot authenticate a reviewer or
establish semantic fidelity/listening quality from JSON. Current real sample remains listening-pending.

`BookMediaPreinstallJourney.swift` is an explicit two-process Mac AVPlayer probe: compile alongside
ContinueListening.swift, BookProgress.swift and BookStore.swift; run `seed <package> <fresh-documents>`
then `resume <same-package> <same-documents>`. It plays existing media muted, persists real clocks,
resumes and reaches chapter EOF at 2x, and exercises Continue policy against a self-authored
podcast transport fixture. It is NOT the iOS PlayerModel/LibraryView, Simulator UI, a physical
iPhone journey, subjective listening, or a new translation/synthesis. UI routing, background/remote
controls and the device protection baseline require the separately named device gates.
