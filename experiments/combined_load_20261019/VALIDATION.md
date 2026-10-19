# Combined review local validation

- Full native market-data regression: 530 tests in 15.695s, one Windows-only check skipped. The final focused subset includes 64 checks.
- Actual-clock short review: Uni 2/2, Focus 4/4, Hot 30/30; zero missing slots; audit and identical repeated replay passed.
- Actual-clock five-minute review: Uni 10/10, Focus 20/20, Hot 150/150; zero missing slots. Final hierarchy 555/40/40.
- A Coordinator revision was published during the controlled pending Hot response; the response kept its captured earlier revision. Admission, independent withdrawal, cap, expiry reentry, delayed demotion and session Uni retention passed.
- All collectors retained incremental steady-state checking. Full audit, repeated acquisition-aware replay, request replay and publication reconstruction passed.
- Separate-process stop drill: parent returned successfully in 0.233s after the stop request; all four workers reported stopped.
- First orchestration drill incorrectly treated Uni's normal last-slot exit as premature; corrected and covered by a focused regression test. Native acquisition and Coordinator source were not changed.
- Windows combined acceptance and running alongside actual production remain pending. Synthetic chosen response delays do not measure provider capacity.
- Final report checks reopen acquisition ledgers read-only and do not register unused polling runs; covered by the regression suite.
- Final uniquely named archive is verified separately after extraction with spaces in its path.

See validation/local_validation.json for timings and scope.
