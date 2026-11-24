# USAspending v2 API Contracts (verified by live probe, Oct 2026)

Base: `https://api.usaspending.gov/api/v2/`. All rows below were exercised live (not inferred).

| Operation | Method + canonical path | Key request fields | Response notes / limits |
|---|---|---|---|
| Award search | POST `search/spending_by_award/` | `filters{time_period[], award_type_codes[], keywords[]/recipient_search_text[]}`, `fields[]`, `sort`, `limit` (≤100), `page` | `results[]`, `page_metadata{hasNext}` (**no `total`** — parity anchors on `spending_by_award_count/` with identical filters). Sort field must be in `fields` (violation → **400**, not 422). **award_type_codes are mandatory** — keyword search without them → 422 (verified live 2026-11-01). Contract and IDV code groups must be queried separately (mixed sets 422). `generated_internal_id` present per row; `generated_unique_award_id` (GID string) is NOT a search row field — resolve via detail or transaction rows. |
| Recipient children | GET `recipient/children/{UEI}/` | parent UEI; trailing slash | children list for corporate parents; **leaf UEIs 400 `Recipient is not listed as a parent`** — fail soft per UEI (verified live 2026-11-01: JZXZSN3BBL98 → list; CE8MGXAS9KJ6 → 400). |
| Award count | POST `search/spending_by_award_count/` | same filters | `{contracts, idvs, grants…}` counts; count and search populations verified equivalent per filter set. |
| Category search | POST `search/spending_by_category/{awarding_agency,awarding_subagency,funding_agency}/` | same filters, `limit` 100 | Agency aggregation rows. |
| Transaction search | POST `search/spending_by_transaction/` | same + `award_ids[]` supported; `fields[]` incl. `Transaction Amount`, `Action Date`, `Mod`, `Action Type`, `Funding/Awarding Agency`, `generated_internal_id` | limit 100/page. PIID-as-keyword + GID attribution filter is the verified pattern. |
| Award detail | GET `awards/{gid}/` (**trailing slash mandatory; slashless 301s**) | GID path param | `piid`, `description`, `recipient{…uei}`, awarding/funding agencies, `psc_hierarchy`, `naics_hierarchy`, `period_of_performance`, `total_obligation`, `parent_award{gid}`. |
| IDV children | POST `idvs/awards/` (**trailing slash**) | `award_id`, `type: "child_awards"`, `limit` ≤100, `page` | Child rows carry NO recipient fields (verified) — use `idvs/activity/` for awardee identity. `"child_contracts"` 400s; `"child_awards"` is the verified value. |
| IDV activity | POST `idvs/activity/` (**trailing slash**) | `award_id`, `limit`, `page` | Rows carry `recipient_id` (stable hash) + `recipient_name`; used for homogeneity. |
| IDV amounts | GET `idvs/amounts/{gid}/` | GID path param | `child_award_count`, `child_award_total_obligation` (parity source; expendable). |
| Award funding | POST `awards/funding/` | `award_id` | File C rows (enrichment only). |
| Recipient children | GET `recipient/children/{UEI}/` (**trailing slash**) | UEI path param | Hierarchy records (shape varies; harvest best-effort). |
| Subawards | POST `subawards/` (**trailing slash**) | `award_id` (prime GID), `limit` 100, `page` | Per-prime ledger. Sweep variant: `search/spending_by_award/` with `subawards:true` REQUIRES `award_type_codes` (else 422) + `Sub-Award …` fields + `Sub-Award Amount` sort; `award_ids[]` batching is silently ignored by the API (verified) — per-PIID pattern retained. |

Preserved invariants: separate contract/IDV searches; trailing slashes on all IDV POSTs (301s drop POST bodies);
never join on bare PIID (verified CDC `75D30124F00014` collision: JCI vs ICE vehicles); always run
recipient + keyword vectors. Egress throttle: per-IP 429/503/504 bursts from Apps egress — handled by
client-side Retry-After honor + 60s cooldown + jitter + direct-hydration fallback for deep hydration.

Date semantics: `time_period` filters transaction/action dates in transaction search; award search window
filters are award-date based — older awards with in-window mods are included via transaction attribution
(GID-attribute pattern), never via award-start-date substitution.
