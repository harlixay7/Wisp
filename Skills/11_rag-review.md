---
name: rag-review
aliases:
  - hybrid-rag-retrieval-grounding-engine
version: 4.0.0
description: >-
  Use when reviewing or designing a retrieval-augmented system: ingestion and
  chunking, embedding models and vector indexes, lexical plus dense hybrid
  retrieval and fusion, metadata and access-control filters, reranking,
  freshness and deletion, query rewriting, context assembly, citations and
  abstention. Produces a pipeline map, a retrieval evaluation table on
  labeled queries and findings. Not for general LLM eval harness design or
  prompt regression gates (use ai-eval-review), for agent tool
  loops (use agent-workflow-review), or for retrieval latency
  profiling (use performance-profiling).
brief: |
  Mission: verify that the pipeline retrieves the right evidence, only evidence the requester may see, and that answers are faithful to what was retrieved, measured on labeled queries, not judged from a few answers.
  - Map every stage (loaders through citation) with its code location and parameters.
  - Chunking: boundaries follow document structure (headings, tables, code blocks), chunk length fits the embedding model's input limit (silent truncation is common), source, section, version and ACL metadata travel with every chunk.
  - Embeddings: query and document encoded as the model expects (prefixes or instructions), normalization matches the distance metric, model identity stored per vector, full re-index when the model changes.
  - Hybrid retrieval: fuse lexical and dense results by rank (RRF) or by per-query normalized scores, never raw scores on different scales; retrieve enough candidates per retriever before fusion.
  - Enforce access control and tenant filters inside the retrieval query, not after top-k; propagate updates and deletions to every index and cache.
  - Context assembly: deduplicate, order deliberately, budget tokens with the generator's tokenizer, delimit retrieved text as untrusted data.
  - Evaluate: recall@k at the k actually used, MRR or nDCG on labeled queries, faithfulness of cited claims, correct abstention on unanswerable queries.
  Emit a pipeline map and a retrieval eval table before the findings.
  PASS: measured quality meets target with no leakage. PASS_WITH_FIXES: local defects with clear fixes. BLOCK: cross-user or deleted content is retrievable, query and index embeddings are incompatible, citations point outside the context, or quality claims have no evaluation.
activation_triggers:
  task_modes:
    - RAG_PIPELINE_AUDIT
    - RETRIEVAL_QUALITY_EVALUATION
    - GROUNDING_FAITHFULNESS_CHECK
  keywords:
    - chunking
    - bm25
    - rrf
    - reranker
    - hybrid retrieval
    - vector index
    - embedding model
    - recall@k
    - ndcg
    - citation faithfulness
    - context assembly
    - query rewriting
  do_not_use_when:
    - The task is building a general eval harness, golden set or prompt regression gate (use ai-eval-review).
    - The concern is agent tool schemas, loops or orchestration (use agent-workflow-review).
    - The concern is retrieval or indexing speed rather than relevance (use performance-profiling).
input_contract:
  requires_worktree: true
  required_inputs:
    - The retrieval pipeline code or design, and the question or change under review
  optional_inputs:
    - A labeled query set (query to relevant document or span IDs), or query logs to sample from
    - A corpus sample and the access-control model
    - Quality targets and the context budget used at generation time
output_contract:
  sections:
    - Pipeline map
    - Corpus and chunk statistics
    - Retrieval eval table
  findings: shared format
  verdict: shared verdict block
---

# Hybrid retrieval and grounding

## Mission

Judge a retrieval-augmented system on three properties in this order: it never returns
content the requester may not see, it finds the evidence that answers the query, and
the generated answer says only what that evidence supports. An excellent review backs
each quality judgment with a number from labeled queries and each leak or staleness
claim with a reproduced query. The common failure is reading five generated answers,
finding them plausible, and approving a pipeline whose recall nobody has measured.

## Inputs to establish first

- Pipeline code and configuration: find it with
  `rg -n -i 'embed|vector|faiss|hnsw|qdrant|weaviate|pgvector|chroma|milvus|opensearch|elasticsearch|bm25|tantivy|rerank|cross.?encoder|top_k|similarity|chunk'`.
- The access model: tenants, users, groups, document ACLs, and where identity enters
  the retrieval call.
- The generation-time context budget and the k actually passed to the generator; metrics
  must be computed at that k, not a convenient larger one.
- A labeled query set. If none exists, build a small one: on the order of 50 or more
  queries as a rule of thumb for a directional signal, drawn from real query logs where
  possible, including queries with no answer in the corpus, labeled at document plus
  span level so labels survive re-chunking. State its size and how it was built.
- If nothing can be executed, review configuration and code, mark quality judgments
  medium or low confidence, and deliver the evaluation plan.

## Method

1. **Map.** One row per stage with implementation, parameters and the identity it
   records (model name and version, analyzer, index build parameters). Done when every
   stage has a code location or is recorded as absent.
2. **Inspect the corpus and chunks.** Dump a random sample plus targeted samples (tables,
   code, long sections, PDFs). Compute chunk count, token length p50/p95/max with the
   embedding model's tokenizer, share above its input limit, and near-duplicate share.
   Done when statistics are reported and representative bad chunks are quoted.
3. **Probe safety properties.** Query as a principal without access to a known document
   using its unique phrase; delete or update a test document and query again through
   every path (vector, lexical, caches). Done when access control and deletion each
   have a reproduced pass or fail.
4. **Evaluate retrieval by stage.** Run the labeled set through ablations: lexical only,
   dense only, fused, fused plus reranker, each at the production k. Done when the eval
   table is filled, or the exact commands to fill it are given.
5. **Evaluate grounding.** For a sample of answers, check every factual sentence against
   the cited chunk, every citation ID against the assembled context, and behavior on the
   no-answer queries. Done when faithfulness and abstention are measured on a stated
   sample, with any automated judge spot-checked by hand.
6. **Recommend.** Each change names the failing measurement it targets and the expected
   metric movement. Done when no recommendation lacks a measured motivation.

## Checklist

**Ingestion and chunking**
- Extraction quality: PDF column order, repeated headers and footers, hyphenation,
  ligatures, tables flattened into unreadable runs, OCR noise. Boilerplate (navigation,
  cookie banners, license headers) can dominate similarity.
- Boundaries: split by structure first (headings, list items, paragraphs, functions in
  code), then by length. Mid-sentence and mid-table cuts strand facts from their subject.
- Headings and titles carried into each chunk (or into its metadata used at ranking),
  so a chunk that says "it supports 32 connections" still names what "it" is.
- Overlap justified by measurement; large overlap inflates the index and fills the
  context with near-duplicates.
- Length versus model limit: many sentence-embedding models truncate input at a few
  hundred tokens and ignore the rest without error. Check the model's maximum sequence
  length in its configuration.
- Stable chunk IDs (derived from document ID, version and span) so updates replace
  rather than duplicate.

**Embeddings and indexes**
- Query and passage encoded as the model card specifies: some families require prefixes
  such as `query: ` and `passage: `, others an instruction on the query side only.
  Mismatched encoding quietly lowers recall.
- Distance metric consistent with normalization: cosine on normalized vectors equals dot
  product; dot product on unnormalized vectors favors long vectors.
- One model per index; model name, version and dimension stored with the index, checked
  at query time. A model upgrade requires a full re-embed, not incremental mixing.
- Approximate search parameters (HNSW `ef_search` and `M`, IVF `nprobe`) measured for
  recall loss against exact search on a sample; defaults are tuned for speed.
- Filtered approximate search: post-filtering the top-k can return fewer than k or zero
  results for selective filters; prefer engines with filter-aware search, or raise
  candidate depth and measure.

**Lexical retrieval and fusion**
- Analyzer fit: language, stemming, stopwords; identifiers, error codes, version strings
  and file paths often get split or dropped by default tokenizers, which is exactly where
  lexical search should beat dense.
- Reciprocal rank fusion: score = sum over retrievers of 1 / (k + rank), with k = 60 a
  common default; it ignores score scale, which is its point.
- Weighted score fusion needs per-query normalization (min-max or z-score) per retriever;
  adding raw BM25 scores to cosine similarities lets one retriever dominate arbitrarily.
- Candidate depth: fusing the top 5 from each retriever cannot surface a document ranked
  20th by one and 3rd by the other; fuse from deeper lists than the final k.

**Filters, access control and freshness**
- ACL and tenant constraints applied inside the retrieval request using the caller's
  verified identity, never from a client-supplied field, and never only after ranking.
- Caches (semantic caches, answer caches, reranker caches) keyed by principal or access
  scope; a shared answer cache can leak across users.
- Deletion propagation: vector store, lexical index, caches, derived summaries and
  evaluation snapshots. Erasure requests must leave nothing retrievable.
- Staleness: document version and timestamp in metadata, newer versions superseding
  older ones at ranking time, re-index triggers on source change.

**Query handling, reranking and context assembly**
- Query rewriting or expansion evaluated against the unrewritten query; rewriting can
  drop the rare term that made the query answerable. Multi-turn rewriting must resolve
  references from conversation history correctly.
- Reranker: input truncation (cross-encoders often cap total query plus passage length),
  rerank depth (rerank 50, keep 5, as an example shape), domain fit, and score thresholds
  for abstention calibrated on labeled data rather than guessed.
- Assembly: near-duplicate removal, deterministic ordering, the most relevant material
  placed where the generator uses it best (models can underuse the middle of long
  contexts), token budget counted with the generator's tokenizer, and per-chunk source
  labels the citation step can reference.
- Untrusted content: retrieved text delimited and treated as data; instructions inside
  documents ("ignore previous instructions") must not change behavior. Flag the exposure
  here and route deep analysis to security-review.

**Grounding and evaluation**
- Citations reference chunk IDs present in this request's context; answers cite at the
  claim level, not one citation for a paragraph.
- Abstention path exists and is tested: when retrieval returns nothing relevant, the
  system says so instead of answering from parametric memory.
- Metrics: recall@k (did any relevant item reach the context), MRR (rank of the first
  relevant item), nDCG@k (graded relevance with position discount). Report per query
  segment (keyword-like, natural language, no-answer), not only the average.
- Label hygiene: labels created by inspecting the current retriever's output are biased
  toward it; pool candidates from several retrievers before labeling.
- Automated faithfulness or relevance judges are spot-checked against human labels on a
  sample before their numbers are trusted.

## Evidence standard

A quality claim cites the labeled set (size, source), the configuration, the command
that ran it and the metric at the production k. A leak or staleness claim cites the
principal, the query, and the returned chunk IDs. A chunking claim quotes the offending
chunk text and its token length. Not evidence: a handful of good-looking answers,
vendor benchmark numbers for the embedding model, recall measured at a k larger than
the context actually uses, or faithfulness scores from an unvalidated judge.

## Severity guide

- P0: documents are retrievable by principals without access or by another tenant;
  deleted content (especially erasure requests) remains retrievable; query and index
  use incompatible embedding models or encodings; citations reference content not in
  the context on the primary path.
- P1: most chunks exceed the embedding input limit; raw-score fusion across retrievers;
  post-filtering that empties results for common filters; no re-index on model change;
  no abstention path; quality claims or launch decisions with no retrieval evaluation.
- P2: unmeasured overlap or ANN parameters; missing deduplication; shallow candidate
  depth or rerank depth; boilerplate pollution; labels at chunk level that break on
  re-chunking.
- P3: parameter tuning proposals with small or unmeasured expected gains.

## Skill-specific output

**Pipeline map**: Stage | Implementation (path:line) | Key parameters | Identity recorded
(model/version, analyzer, index params) | Observed issue.

**Corpus and chunk statistics**: document and chunk counts; token length p50, p95 and max
with the tokenizer named; share over the model limit; near-duplicate share; quoted
examples of bad chunks.

**Retrieval eval table**: one row per configuration in the ablation matrix.

| Configuration | Queries (n, of which no-answer) | Recall@k | MRR | nDCG@k | Faithfulness | Correct abstention | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |

Mark cells NOT_RUN with the exact command when a measurement could not be executed.

## Anti-patterns

- **Judging by anecdotes.** Five plausible answers say nothing about recall. Every
  quality judgment cites a labeled-set metric or is marked low confidence.
- **Recommending a new stack.** Proposing a graph store, a new vector database or a
  bigger embedding model without showing which measured failure it fixes.
- **Tuning blind.** Changing chunk size, overlap or k without an eval run before and after.
- **Circular labels.** Labels derived from the current retriever's own results inflate
  its scores; pool candidates from several retrievers before labeling.
- **Unvalidated judges.** Automated faithfulness scores without a human spot check.
- **Ignoring the no-answer case.** A system that always answers has an abstention rate
  of zero; test queries the corpus cannot answer.
- **Trusting defaults.** Vector database and tokenizer defaults are generic; verify each
  against this corpus and model.

## Done when

- [ ] Every pipeline stage is mapped with code location, parameters and recorded identity.
- [ ] Access control and deletion propagation were probed with reproduced queries.
- [ ] Chunk statistics include the share above the embedding input limit.
- [ ] Retrieval metrics are reported at the production k, by configuration, or the exact commands are given.
- [ ] Faithfulness and abstention were checked on a stated sample.
- [ ] Every recommendation names the measurement it is expected to move.
