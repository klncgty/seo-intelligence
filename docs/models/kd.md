# Model card — Keyword Difficulty v1 (`kd-1`)
Own metric (0–100), explainable components, weights renormalized over available inputs:
- authority (0.6): mean domain strength of top-10 organic domains. **Unavailable until the backlink index / domain-strength
  model exists (Phase 8)** → reported in `missing_inputs`, confidence drops to 0.4.
- serp_crowding (0.25): features that push organic results down (AI overview 12, featured snippet 8, local pack 8, shopping 8,
  ads 6, knowledge graph 5, video 4, top stories 4, PAA 3, images 2) ×2, capped at 100.
- concentration (0.15): share of top-10 occupied by repeated domains.
Input: latest SERP snapshot (licensed provider). No snapshot → KD = null. Not validated against own rank tracking yet
(requires Phase 5 data) — accuracy unknown. Deterministic: identical inputs → identical output (tested).
