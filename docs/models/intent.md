# Model card — Search intent rules (`intent-rules-1`)
Multi-intent, deterministic. Signals: TR/EN modifier lists (transactional, commercial, informational, navigational; matched on
the Turkish-folded key), brand terms (navigational), SERP composition (shopping/ads/local pack → transactional; snippet/PAA/
video/top stories → informational; knowledge graph → navigational). Intents with score ≥ 50% of max are returned; no signal →
`intents: []`, `ambiguous: true` (no guessing; Claude Code may review ambiguous ones, labelled as AI). Accuracy not yet measured
on a labelled set.
