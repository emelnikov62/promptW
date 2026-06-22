# Multi-image task-id recovery & full-set persistence

## Problem
A photo generation with `count=N` runs **N independent KIE tasks** (`generate_image._one`),
but the DB row persists only **one** result and **one** task id:
- `finish_generation_if_pending(gen_id, file_urls[0])` stores only the first URL in
  `result_url`; the other N−1 are returned to the live client but never saved.
- `_task_saver` → `set_generation_task` records only the **first** task's id
  (`on_task=on_task if report else None`, `report = (i == 0)`).

Consequences:
1. **History shows 1 of N** even with no restart (reload re-reads `result_url`).
2. **Restart recovery restores 1 of N** — the reconciler only knows the first task id;
   the user paid `30·N` but recovers a single image after a deploy mid-generation.

## Fix — store the whole set
Persist **all** task ids and **all** result URLs per generation row; recover the full set.

### Schema (`db/database.py`, idempotent ALTERs)
```sql
ALTER TABLE generations ADD COLUMN IF NOT EXISTS provider_task_ids JSONB DEFAULT '[]';
ALTER TABLE generations ADD COLUMN IF NOT EXISTS result_urls       JSONB DEFAULT '[]';
```
Legacy single columns (`provider_task_id`, `result_url`) stay populated with the
**first** element for back-compat (admin views, delete cleanup, old rows).

### Queries (`db/queries.py`)
- `add_generation_task(gen_id, task_id)` — append to `provider_task_ids` (dedup via
  `@>`), and `provider_task_id = COALESCE(provider_task_id, $2)`. Replaces
  `set_generation_task`.
- `get_pending_generations` — also select `provider_task_ids`.
- `finish_generation_if_pending(gen_id, result_url, result_urls=None)` — set both
  `result_url=$2` and `result_urls=$3` (defaults to `[result_url]`). Guarded
  pending→done unchanged.
- `get_user_generations` — also select `result_urls`.
- `delete_generation` — return `result_urls` too so all objects are cleaned up.

### Generation (`api/routes.py`)
- `_task_saver` → `add_generation_task` (every one of the N tasks now reports its id;
  `generate_image` passes `on_task` to **all** `_one(...)`, not just the first).
- image `_build`: `finish_generation_if_pending(gen_id, file_url, file_urls)`.
- video/audio `_build`: `finish_generation_if_pending(gen_id, file_url, [file_url])`.

### Reconciler (`api/routes.py _reconcile_once`)
Per pending row, gather `provider_task_ids` (fall back to `[provider_task_id]`):
- no ids at all → existing no-task giveup path.
- recover each id (`recover_task`):
  - any raises (hard fail) → discard collected paths, `_reconcile_fail` (refund once).
  - any returns None (still processing) → **incomplete**: discard the paths collected
    *this pass* (avoid orphan accumulation), give up only past `_RECONCILE_GIVEUP`,
    else retry next sweep.
  - all return paths → `finish_generation_if_pending(gen_id, urls[0], urls)`; if we
    lost the race, discard all.

Re-downloading already-ready images each sweep until the slowest finishes is accepted
(sweeps are 60 s, images are small, partial downloads are discarded so nothing leaks).

### Frontend (`webapp/static/js/app.js`)
- `loadUserHistory`: expand each row's `result_urls` (fallback `[result_url]`) into
  **separate** gallery items sharing the row `id` (matches the live path, which already
  unshifts one item per `file_urls` entry). Deleting any one deletes the row + all images.
- `api_get_history` (`routes.py`): JSON-decode `result_urls` like `settings`.
- Cache-bump `app.js?v=`.

## Out of scope
- Per-image deletion (one image of a multi set) — they remain one generation/row.
- Backfilling `result_urls` for historical rows (they keep showing their single image).

## Verification
1. `count=4` photo gen → 4 images in the live overlay AND 4 in history after reload.
2. Kill the service mid-4-image gen (before done) → on boot the reconciler recovers all
   4 and the row flips to done with 4 `result_urls`; balance not double-charged/refunded.
3. Single-image photo / video / audio unaffected (1 entry, recovers as before).
4. Delete a multi-image history item → all 4 objects removed from storage, row gone.
