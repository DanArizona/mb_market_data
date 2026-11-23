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

## November 23 correction — initial verifier startup failure

The initial experimental commit `c8c5f5b` contained an accidentally
corrupted media-type regular expression in `schwab_quotes.py`. The
Windows verifier failed **before loading any tests** with a
`SyntaxError: unterminated string literal` on line 249. This was a
patch-construction error, not another provider failure.

The correction replaces the corrupted regex with the simpler media-type
validator `[a-z0-9][a-z0-9._+-]*/[a-z0-9][a-z0-9._+-]*`, allowing
standard HTTP media types such as `application/json`,
`application/problem+json`, and `text/html` while rejecting
unrecognized types. No polling, scheduling, or request behavior was changed.
The helper's syntax and sanitized metadata output were checked independently.
The initial syntax failure was corrected and the Windows qualification results
are recorded in the sections below.

For the existing isolated Windows checkout, update the experimental branch
with `git pull --ff-only`, then rerun
`call verify_quote_response_diagnostics.cmd`. Do not create another
checkout or change the accepted production installation.

## November 23 Windows qualification — verified

In the isolated checkout
`C:\\Users\\danla\\mb_quote_response_qa_20261123`,
the operator fast-forwarded from `c8c5f5b` to `57b81f0`, then ran:

- Offline `ast.parse` over the quote source and regression test file:
  **SYNTAX PASS**.
- `call verify_quote_response_diagnostics.cmd`:
  **15/15 PASS**, zero errors, zero failures, zero skips.
  The 15 tests comprise 11 unchanged tests and four new cases covering
  safe metadata, HTTP failure classification, structured provider errors,
  and the 400-failure/186-success sequence.
- Result explicitly recorded `real_provider_calls=false`,
  `password_prompts=false`, `production_changes=false`;
  `source_inventory_verified=false` is intentional for this
  experimental change, not a full release gate.

**This is focused quote-handler acceptance, not a full production
regression or live qualification.** The November 23 original failure
remains attributable only to unreadable responses; its HTTP status
cannot be recovered from the saved journal.

## Additional full-core regression gate (experimental runner)

This branch also provides `verify_quote_response_full_offline.cmd`,
which runs the **same core test modules** as the accepted
`verify_offline.py` (304 historical expected tests) plus the four
new quote-response tests, requiring **308/308** and zero skips.

It uses the installed `closeout-qa` dependencies, deliberately
disables sockets and human credential inputs, preserves temporary
journal/process test behavior in the isolated checkout, and writes
a separate result/evidence directory under `output/`.

It does **not** rewrite or disable the existing production-integrity
fingerprint in `verify_offline.py`. It explicitly reports
`source_inventory_verified=false` because this candidate modifies
the accepted quote source. The remote source-tree delta is still
reviewed separately against the recorded main baseline.

From the existing isolated `closeout-qa` checkout:

```cmd
git pull --ff-only origin experiment/quote-response-diagnostics-20261123
```

```cmd
call verify_quote_response_full_offline.cmd
```

### Full Windows result — PASS

On MasterBot, the operator updated the isolated `closeout-qa` checkout from
`57b81f0` to `e571117`, then ran the new runner.

- `ast.parse('verify_quote_response_full_offline.py')`: **SYNTAX PASS**.
- `call verify_quote_response_full_offline.cmd`: **308/308 PASS**, zero
  failures, zero errors and zero skipped tests, completed in **20.003 s**.
- This includes the existing **304** core regression tests plus **4**
  additional quote-response tests; it is a comprehensive **offline**
  behavior regression, not live provider qualification.
- Environment: **Windows (`win32`)**, `closeout-qa`, exchange-calendars
  **4.13.2**.
- Reported **no** real provider calls, password prompts or production
  changes. The intentionally altered source fingerprint was **not
  verified** under the old immutable integration inventory.
- Evidence directory on MasterBot:
  `C:\\Users\\danla\\mb_quote_response_qa_20261123\\output\\quote-response-full-20261123T153715116987Z`.
  The evidence is operator-reported local Windows output, not a
  GitHub Actions result.

**Qualification conclusion:** Focused 15/15 and full-core 308/308 offline
suites both passed. The experimental quote-response diagnostic patch is
qualified for further integration review, **not** for production
deployment. A reviewed source baseline, audit of detailed metadata
behavior in the installed client, approved deployment strategy, and
controlled attended live qualification remain separate gates.

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
