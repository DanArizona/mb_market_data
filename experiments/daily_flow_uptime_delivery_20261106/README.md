# Alternate delivery for the unchanged November 6 uptime candidate

Chat attachment downloads failed repeatedly in the operator's original browser
and Microsoft Edge. This experimental branch supplies a Git-based alternative.
It does not merge into main, install a runtime, or activate collection.

## What it reconstructs

`restore_uptime.py` reads the original extracted `daily_flow_candidate_20261106`
package and six compressed-data parts. It verifies all required original file
hashes, reconstructs the target entirely in memory, verifies every target file,
then writes a NEW `daily_flow_uptime_20261106` directory. Existing destinations
are refused. The original directory and its output are never modified or copied
as operational data. The reconstruction program makes no network request, reads
no credentials, and does not execute the reconstructed candidate.

The six payload parts contain Base64-encoded XZ-compressed JSON with file copy,
line-copy, and literal-text records. These are data, not pickle or executable
instructions. SHA-256 checks bind both the payload and complete target inventory.
No executable code in the payload is evaluated during reconstruction.

All **129 reconstructed files are byte-for-byte identical** to the original
uptime ZIP contents, including source, tests, documentation, and retained local
evidence. The correction itself has not been changed to solve this delivery issue.

Original baseline ZIP SHA-256:
`a9f459b037d6d62d8fb1c5e2b5af024fffbb3a27c3b4265b514264ade69614ef`

Original uptime ZIP SHA-256:
`f9ef15ce08736d582a81e9902300798d29fe0d1e0dde0ac365cd944fc90c712b`

Decoded payload SHA-256:
`32e4420dc16127c4d7eec2944c3b04f791d92b28a1d5c894c880a1dd22169b72`

Canonical target path-to-SHA256 manifest digest:
`002ebdfc86fa470baa5a39eab86e0d0ca74f548a89e0f1075e143ae6943e3d7b`

## Windows delivery and verification

Use the existing closeout-qa environment, not a new installation. From any CMD
working directory, clone this branch into a NEW folder:

```cmd
conda activate closeout-qa
git clone --depth 1 --single-branch --branch experiment/daily-flow-uptime-delivery-20261106 https://github.com/DanArizona/mb_market_data.git "%USERPROFILE%\uptime_delivery_20261106"
```

Only after cloning succeeds:

```cmd
python -B "%USERPROFILE%\uptime_delivery_20261106\experiments\daily_flow_uptime_delivery_20261106\restore_uptime.py" --base "%USERPROFILE%\dailyflow_qa\daily_flow_candidate_20261106" --destination "%USERPROFILE%\dailyflow_uptime_qa\daily_flow_uptime_20261106" && call "%USERPROFILE%\dailyflow_uptime_qa\daily_flow_uptime_20261106\verify_offline.cmd"
```

Expected reconstruction confirmation:
`DELIVERY VERIFIED: 129/129 exact candidate files`

The existing offline verifier then runs the 177-test suite. No Schwab password,
live Nasdaq/Schwab requests, environment changes, or production writes are
requested. Original November 6 evidence and the already-prepared November 9 r0
remain in the original dailyflow_qa directory. Do not delete or overwrite that
folder. A destination-exists or checksum error means stop and review; it is not
permission to remove an existing directory.

## Delivery validation (not new runtime qualification)

Six local delivery checks passed: exact match of all 129 target files while
preserving original/output sentinel bytes; changed-baseline refusal before
writes; existing-destination preservation; nested-destination refusal; corrupt
payload refusal before writes; Windows CRLF transport-line-ending compatibility.
These ran on Linux. They establish the transport/reconstruction mechanism, not
Windows collector uptime. GitHub payload blob IDs were checked against the
local files after publication.

The preserved 177-test local runtime acceptance remains scoped as originally
recorded: synthetic credentials/provider and schedule clocks, actual owned child
process tests, Windows verification pending, and no live uptime claim. The old
one-read heartbeat weakness was reproduced; the exact trigger of each historic
November 6 stop remains unproved.
