# Vrchcab: bounded tournament pagination – offline review, 2026-11-07

**Status:** draft PR #7, NOT merged. No live forecast/POST, no external LLM, no extra Metaculus GET. Main, the V2 secret and `metaculus-publication-state/publication.enc` are unchanged.

## Confirmed root cause
- Locked `forecasting-tools==0.2.92`; in the SDK `get_all_open_questions_from_tournament()` calls `get_questions_matching_filter(ApiFilter(...statuses=['open'], group_question_mode='unpack_subquestions'))` without `num_questions`. Thus `_filter_sequential_strategy(..., None)` retrieves only offset 0.
- The SDK's alternative numbered strategy can also stop when a page contains no *parsed questions*, including a notebook-only page.
- **Server implementation:** `posts/views.py` uses `CountlessLimitOffsetPagination`, and `utils/paginator.py` overrides `get_count` to infinity. Thus a non-null `next` is NOT proof of more records. Public independent measurement confirms `next` continues past exhausted query results.
- `posts/views.py` passes `with_cp` and `include_user_forecasts=True` to `serialize_post_many`; `posts/serializers.py` prefetches own forecasts when authenticated and `with_cp=True`. But account/tier-specific responses may differ. If own forecast history is absent, **fail closed**; never infer `already_forecasted=False` from missing metadata.

## Bounded scanner
- Only projects `33121`, `minibench`, only GET `/api/posts/`, `statuses=open`, `limit=100`, `order_by=-published_time`, `with_cp=true`, `include_conditional_cps=true`; no type filter (retain group posts).
- At most 5 pages per competition, 100 raw posts each; 6 seconds or more between all GETs, including between tournaments, 18 s request timeout, 15 MB max body, no redirect/retry. HTTP 429 and all API/schema anomalies abort the whole scan.
- Because `next` is not terminal, stop on **two consecutive empty RAW pages** (not two unsupported/filtered pages) or explicit null next. A *single* empty page is followed by another offset to avoid false local-negative conclusions. Stop on budget exhaustion without evidence of exhaustion is an error, not an empty result.
- Process raw posts using the **pinned SDK's post-to-question parser**, retaining group subquestions. Validate question IDs, own forecast histories, UTC opening/closing and actual close override. Deduplicate by **question ID**, including cross-tournament overlap. Don't hide previously forecasted questions in scan; existing ForecastBot filter `skip_previously_forecasted_questions=True` removes them before model/publish.
- The production `tournament` path scans **both competitions completely before** even invoking `forecast_questions`. On any incomplete inventory, no model, journal, Metaculus POST or comments occur. Existing Cup and Test Bot behavior untouched.

## Remaining risks / REVIEW GATES
1. Two empty pages is a defensive terminal heuristic grounded in the server's contiguous offset pagination, **not a mathematical proof** of absence under concurrent feed mutation. Track potential offset drift and consider a bounded repeat/consistency check if live observations demand it.
2. Check the **actual authenticated Vrchcab list JSON schema** in an approved minimal read-only verification. Do not print private forecast values, question IDs/titles, credentials, or raw response bodies.
3. The available historical first three offsets (0,100,200) were sparsely populated and next always set. The offline tests exercise that case. An empty-results count alone does not prove absence of eligible unforecasted questions without actual history/time validation.
4. Review group and conditional parser compatibility against the pinned SDK on realistic sanitized fixtures, especially own-forecast history and metadata of conditional parents/children.
5. Assess the fixed limit (5 pages per project) against actual project size; if cap is exceeded, fail closed rather than silently omit questions. Never automatically increase cap or request frequency.
6. All offline PR tests and the original publisher suite must pass on **the exact final PR head SHA**.
7. **No merge** without user approval. A separate explicit approval is required before an actual publisher run, models or restoring the old external cron. First real publish+encrypted checkpoint is still unverified end-to-end.

## References
- SDK main source: https://github.com/Metaculus/forecasting-tools/blob/main/forecasting_tools/helpers/metaculus_client.py
- Server list endpoint: https://github.com/Metaculus/metaculus/blob/main/posts/views.py
- Countless paginator: https://github.com/Metaculus/metaculus/blob/main/utils/paginator.py
- Server forecast prefetch: https://github.com/Metaculus/metaculus/blob/main/posts/serializers.py
- Official public API spec: https://www.metaculus.com/api/
- Independent pagination measurement (2026-08-11): https://github.com/eltociear/metaculus-forecast-bot
