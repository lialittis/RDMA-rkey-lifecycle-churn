# RXE churn reproduction results (2026-11-16)

Validation of the source-only reproduction repository at commit ec02037 on
the existing two-VM soft-RoCE lab (owner 192.168.122.112, requester
192.168.122.111, active rxe0, dedicated /tmp/rxe-churn-repro-validation
directory). The main run is the primary result; the shakedown is an
optional diagnostic auxiliary (internal role "smoke") that is never merged
into the main run's denominators and never carries the earlier frozen
revision's diagnostic-run name. Aggregate across all attempts: 604096
successful owner target registrations (the authorized ceiling) and 40.2 s
from the first registration through final cleanup, within the 900 s
budget. In the main run, 75713 MR pool-index recurrences and 332 complete
32-bit rkey recurrences were observed against all earlier target
generations; all 12 predeclared samples (4 index-only, 8 full-key) were
rejected remote access errors with intact memory and accepted positive
controls. Descriptive observations within a finite budget only.

## Results table

| Run | Role | Kernel | RXE | Toolchain | Source SHA-256 | Binary SHA-256 | Reg limit | Regs | Time limit (s) | Wall (s) | Index reuse | Full-key recurrence | Sampled outcomes | Stop reason | Verification | Deviations | Limitations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| main | primary | 6.8.0-147-generic (aarch64) | rxe0 active (soft-RoCE, in-kernel rdma_rxe) | gcc 13.3.0, libibverbs 1.14.50.0 | 41d3e8a16262662054443bef9c5bca374d2fc283768d30e0e0f84519e01799ec | cb142251c50e414317d1694015cb1ac4bcc89598d3020ba884199109e80d9d4b | 600000 | 600000 | 600 | 12.4 | 75713 | 332 | accepted=0 rejected=12 indeterminate=0 anomalous=0 | count-budget | PASS | run plan: diagnostic smoke + main-only primary, caps 600000/4096 (prior archived plan: earlier diagnostic + main under 1048576); source byte-identical to ec02037; same fixed allocation, probe shape, quiescent deregistration and sequential descriptor/ACK controls | finite budget: 604096 successful registrations in 40.2 s; counts are observations in this window only; device-global allocator visibility limit (other device MR users not ruled out); no rate inferred from source alone |
| shakedown | auxiliary | 6.8.0-147-generic (aarch64) | rxe0 active (soft-RoCE, in-kernel rdma_rxe) | gcc 13.3.0, libibverbs 1.14.50.0 | 41d3e8a16262662054443bef9c5bca374d2fc283768d30e0e0f84519e01799ec | cb142251c50e414317d1694015cb1ac4bcc89598d3020ba884199109e80d9d4b | 4096 | 4096 | 60 | 0.1 | 0 | 0 | accepted=0 rejected=0 indeterminate=0 anomalous=0 | count-budget | PASS | diagnostic only; never merged into main denominators | too short for recurrence statements; zero observations is not an eligibility claim |

Verification status: `python3 verify.py --strict run-manifest.json` and
`python3 scripts/check_summary.py docs/reproduction-results.md
results/analysis.json` both pass against the archived raw ledgers,
analysis and manifest under `results/` (git-ignored; archived by the
coordinator under the project's runs/). The frozen earlier revision and
its archived verification stay untouched at the `original-run` git tag.
