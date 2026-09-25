# Observation Overlay MVP Contract

**Status:** Visual integration implemented; cache acquisition and real-day
validation pending
**Scope:** One symbol, one ET session, historical playback only

## 1. Purpose

The Observation Overlay is a supplemental diagnostic view. It combines the
existing schema-v2 quote-journal timeline with separately cached Schwab
five-minute OHLCV without changing either source. Its purpose is to show what
was causally available at a selected replay instant and which sampling tier
governed the symbol at that time.

The journal audit and deterministic replay remain authoritative. The overlay
is not a producer, selector, signal engine, or acceptance gate.

## 2. MVP inputs

The MVP accepts exactly two immutable inputs:

1. A completed schema-v2 quote-observation journal using
   `nested-uni-focus-hot-v1` membership revisions.
2. A separate `observation-overlay-ohlcv-v1` cache for one symbol and the same
   ET session.

The OHLCV cache records:

- provider (`Schwab` for the MVP);
- source method;
- symbol and ET session date;
- acquisition timestamp;
- request bounds;
- five-minute frequency and extended-hours inclusion;
- SHA-256 of the source payload; and
- normalized OHLCV candles.

The cache is never stored inside, attached to, or written through the journal.
Cache files are immutable: an existing path is not overwritten.

## 3. One replay clock, three visibility rules

All ordering and comparisons use aware UTC timestamps. Human-facing market
times are rendered in `America/New_York`.

| Evidence | Becomes visible | Boundary |
|---|---|---|
| Membership revision | `effective_at_utc` | Inclusive |
| Quote observation | Its complete acquisition's `completed_at_utc` | Inclusive |
| Five-minute OHLCV candle | Candle start plus five minutes | Inclusive |

Consequences:

- A quote scheduled at 09:30:05 but completed at 09:30:05.320 is hidden at
  09:30:05.319 and visible at 09:30:05.320.
- A 09:30--09:35 candle is hidden until exactly 09:35:00.
- Later cache acquisition does not make a candle visible early during replay;
  candle completion controls historical visibility.
- Seeking backward rebuilds the overlay from immutable inputs. It does not
  preserve later state.

## 4. Membership bands

The displayed band is the symbol's highest active tier:

| Band | Color | Meaning |
|---|---|---|
| Outside Uni | Gray `#808080` | Not in the active Uni revision |
| Uni | Cyan `#00bcd4` | In Uni, but not Focus or Hot |
| Focus | Gold `#d4a017` | In Focus, but not Hot |
| Hot | Magenta `#d100d1` | In Hot |

The hierarchy contract guarantees `Hot ⊆ Focus ⊆ Uni`. A complete hierarchy
revision can therefore move the selected symbol through
Outside → Uni → Focus → Hot or back to a lower/outside band. Only actual band
changes create overlay transitions.

When multiple revisions share a timestamp, journal replay order applies. A
same-time `r0` then `r1` can therefore record a zero-duration Uni transition
followed by Focus. This preserves the exact journal history rather than
silently collapsing accepted revisions.

## 5. Quote points

Every selected-symbol outcome is retained at its acquisition completion time,
including non-quote statuses and detail text. Points preserve:

- acquisition ID;
- channel and bound channel revision;
- scheduled and completed timestamps;
- status and detail; and
- the complete normalized values mapping.

If the symbol is sampled by both Uni and Focus, both event streams remain
visible. The preparation layer does not resample, deduplicate, interpolate, or
replace one channel with another.

## 6. Read-only and fail-closed behavior

The preparation layer:

- accepts a causally ordered schema-v2 event stream;
- rejects legacy independent-channel membership events;
- rejects another session, symbol, cache version, provider, frequency, or
  malformed provenance;
- rejects out-of-order events and non-increasing hierarchy revisions;
- never opens the journal for writing;
- never authenticates to Schwab during replay; and
- never affects polling, selection, membership, or publication.

The caller obtains journal events from `QuoteJournalReplayReader`, whose
SQLite store is already opened read-only for replay.

## 7. Explicit exclusions

The MVP does not include:

- live mode;
- multiple symbols, sessions, or days;
- indicators, signals, news, events, or holdings;
- configurable candle periods;
- alternate data providers;
- strategy optimization or backtesting; or
- any write into a production journal.

These remain deferred rather than rejected.

## 8. Acceptance boundaries

Automated coverage must prove:

1. no membership before the first effective revision;
2. exact inclusive membership visibility at `effective_at_utc`;
3. exact inclusive quote visibility at `completed_at_utc`;
4. candle invisibility one microsecond before interval close and visibility at
   interval close;
5. deterministic Uni → Focus → Hot → Outside transitions;
6. immutable cache round-trip with preserved provenance;
7. rejection of legacy journals and causally unordered input; and
8. unchanged existing journal, replay, and dashboard behavior.

The implemented optional dashboard surface renders completed five-minute
candles and volume, numeric quote outcomes, evidence/status counts, and
membership intervals from the same replay clock. Restart and historical seek
rebuild both the main state and overlay projection together. With no cache
argument, dashboard behavior and callback structure remain unchanged.

The next increment must acquire and persist validated cache artifacts, then
exercise the surface against completed September 21, 23, and 24 journals.
