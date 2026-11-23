# Quote-response JSON failure diagnostics — 2026-11-23 experiment

**Branch:** `experiment/quote-response-diagnostics-20261123`.
**Base:** `DanArizona/mb_market_data:main`,
`de1e5544f583c66ebd80355ad2cbaa55ef8860a6`.
**Status:** experimental diagnostic change only; **not merged, installed,
or authorized for live collection**.

## Incident motivating this experiment

The November 23 production session under the existing installed
`one-unlock-daily-flow-v1` controller showed:

- At 09:25 ET, opening current-volume selection requested 586 symbols.
  Batch 1 (400) returned 400 `request_error` outcomes; batch 2
  (186) returned 186 valid quotes; retry batch 3 (400) returned 400
  `request_error` outcomes. The selection was correctly reported
  `DEGRADED` and r1 was nonetheless published.
- At 09:30 ET, Uni (09:30:00), Hot (09:30:02) and Focus (09:30:10)
  all showed error observations of the form
  `Unreadable Schwab JSON response: JSONDecodeError: Expecting value:
  line 3 column 9 (char 22)`. This was independent of MYND's subsequent
  automatic Hot admission, effective 09:31:05 ET.
- Follow-up read-only journal queries identified the same decoding
  failure across all three channels. The API heartbeat `AVAILABLE`
  meant a response was received; it did **not** establish valid
  JSON or usable quotes.
- By approximately 09:45 ET, the operator had stopped collection with
  the supported `daily.cmd stop`; Uni, Focus and Hot workers each
  reported `exit 0` and `collection_stop_requested`. The daily
  controller was left alive with post-close preparation armed.
  Missing-slot evidence was retained; **this is not an audit-clean day**.
- `schwabdev` 4.0.0 is installed and is the required minimum of
  `mb_tools.schwab_secure`.
- Separate, explicitly attended diagnostic requests later returned
  HTTP 200, content type application/json, and parseable JSON for
  the three Hot seed symbols and the **exact original 400-symbol**
  batch. That excludes a fixed hard limit or permanent incompatibility;
  it does NOT identify the historical HTTP statuses or response bodies.

The prior handler tried `response.json()` before it inspected
`response.ok`. It could therefore mask a non-2xx response behind
the same JSON error as a malformed HTTP 200 response. Today's
journal did not persist the response status, media type, body length
or fingerprint; **it cannot reconstruct those details retrospectively**.

## Changes in this isolated experiment

Only `src/mb_market_data/schwab_quotes.py` changes quote handling.
The existing `fetch_quotes_batched(client, symbols, fields, batch_size)`
signature, per-symbol status classifications, batching, acquisition
timestamps, rate/authorization control, and journal format are kept.
No extra provider request or automatic retry is introduced.

When an HTTP response is not OK:

1. The HTTP failure is classified **first**, even if its JSON body is
   unreadable. The original structured provider error detail remains
   available when valid JSON is returned.
2. If error JSON is unparseable, use the exception type and generic
   position information only. Never emit the exception string or
   response text verbatim.
3. Append a safe summary in the persisted per-symbol detail:
   `http_status`, validated `content_type` media type only,
   `body_bytes`, `body_sha256` and `batch_symbols`.

When an HTTP-OK response fails JSON decoding, record the same safe
summary and the JSON exception type/position in the existing
`request_error` detail, preserving the request/response timestamps.

**Data minimization:** no response body, response excerpt, arbitrary
HTTP header, bearer token, password, OAuth callback, token database,
or full exception string is logged. Body bytes are accessed only to
calculate byte count and SHA-256; nothing new is written to separate
raw-response files. Missing response metadata is marked unavailable
instead of forcing an additional request.

HTTP-OK valid-JSON quote, invalid-symbol and missing-symbol behavior
remains unchanged. This experiment does **not** alter the independent
`BackoffClient` API-state labels; improving the distinction between
a received HTTP response and a usable quote is a separate potential
work item after evidence review.

## Offline qualification

Use a **new isolated Windows checkout**, separate from the installed
production controller and both Overlay/Membership QA worktrees.
In Anaconda Prompt:

```cmd
git clone --branch experiment/quote-response-diagnostics-20261123 --single-branch https://github.com/DanArizona/mb_market_data "%USERPROFILE%\mb_quote_response_qa_20261123"
```

```cmd
conda activate closeout-qa
```

```cmd
cd /d "%USERPROFILE%\mb_quote_response_qa_20261123"
```

```cmd
call verify_quote_response_diagnostics.cmd
```

The focused verifier runs **15 synthetic tests** (11 pre-existing plus
4 new), blocks network sockets and password inputs, and makes no
real provider calls, production journal writes, or changes to the
installed controller. The new tests cover:

1. HTTP 200 invalid JSON with status, sanitized content type, body
   count/hash, batch size and no sensitive strings;
2. HTTP 503 invalid JSON retaining its true HTTP classification,
   preserving response-completion time and never logging a secret;
3. HTTP 500 valid JSON still preserving the existing structured
   provider error detail;
4. 400 failing symbols followed by 186 successfully parsed quotes,
   with individual batch outcomes preserved.

**Caution:** `verify_offline.cmd` on this isolated experimental
checkout has an immutable integration source fingerprint and a
hard-coded historical 304-test expectation. Because this experiment
*intentionally changes* a fingerprinted module and adds new tests,
that older wrapper cannot be used to qualify the new candidate
unchanged. **Never modify or overwrite the accepted production
fingerprint just to make the wrapper green.** A separate,
explicitly reviewed versioned integration baseline and full Windows
regressions would be required before any production merge/release.
The dedicated verifier qualifies only the specific diagnostic
change; it does **not** qualify installation.

## Open diagnosis and production policy

- Exactly what status/body Schwab returned during the 09:25–09:43
  failures remains unknown; a later successful request is not proof
  of the earlier cause.
- The existing session was stopped intentionally. Do **not** resume
  it, overwrite its journal, rerank Focus r1, or infer full-day audit
  success from a later API probe.
- The daily controller's independent post-close preparation remains
  responsible for next-session r0; inspect its evidence before using
  it for the next opening.
- Do not upgrade `schwabdev`, alter batch size, or change credentials
  on the strength of this single observation.
- Production rollout requires explicit approval, a full integrated
  source/contract review, Windows regression checks, and an
  operationally controlled future live validation. The experiment
  may be revised independently without a `main` merge.
