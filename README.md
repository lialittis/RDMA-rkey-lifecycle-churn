# RXE rkey churn experiment

Code for a two-endpoint RXE experiment that records MR pool-index reuse, complete rkey recurrence, and sampled RDMA WRITE outcomes after registration and deregistration.

## Check the code

```sh
bash scripts/test_source.sh
```

This runs 16 tests without an RDMA device or experiment data. The full test suite needs a separate dataset; see [Reproducing the experiment](REPRODUCING.md).

## Files

- `src/churn.c`: RDMA experiment program.
- `scripts/deploy.py` and `scripts/run.py`: build, deployment, and run launchers.
- `ledger.py`, `analyzer.py`, and `verify.py`: data recording, analysis, and integrity checks.
- `tests/`: unit and dataset checks.
- `docs/ledger-schema.md`: ledger format.

The run scripts contain settings for the original two-VM lab. Check the endpoint and SSH settings before using them elsewhere. Raw results are not included in this repository.
