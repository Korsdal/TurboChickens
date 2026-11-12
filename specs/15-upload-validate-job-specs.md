# 15: Upload and validate job specs from the browser

**What to build:** `POST /api/jobs/validate`, and the upload path of `POST /api/runs`, accept a spec bundle — job TOML plus prompt, schema and attachments — as JSON with base64-encoded files, size-capped (~10 MB total), stored under `<data_dir>/uploads/<job>-<spec_hash>/` so it is enqueueable by an in-container path. Validation reuses the existing spec loader, so errors come back field-named exactly like `tc jobs validate` (e.g. `trigger.schedule: …`). The Jobs view gains an upload/validate form: file inputs, per-field errors rendered under the form, and a Run action once valid.

**Design:** `specs/00-design-system.md` — Jobs view upload form spec there (48px fields, visible labels, errors under their fields, one primary "Validate"/"Run job" button).

**Blocked by:** 14 (runs path over HTTP, so an uploaded spec can be enqueued)

**Status:** ready-for-agent (2026-11-12)

- [ ] A valid bundle uploads, validates OK with its spec hash, lands under `uploads/<job>-<hash>/`, and is enqueueable via `POST /api/runs` by that stored path
- [ ] An invalid bundle returns field-named errors rendered under the form; nothing is registered as a job
- [ ] The size cap is enforced with a clear error
- [ ] E2e: create and run a brand-new job entirely over HTTP with the fake vibe binary, result fetched over HTTP
- [ ] Tests: HTTP-level upload/validate/enqueue, cap enforcement, error shapes; no host paths ever enter the UI
