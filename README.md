# RXE rkey churn experiment

Code for a two-endpoint RXE experiment that records MR pool-index reuse, complete rkey recurrence, and sampled RDMA WRITE outcomes after registration and deregistration.

## Check the code

```sh
bash scripts/test_source.sh
```

This runs 21 tests without an RDMA device or experiment data. Python 3.11 or newer is required. The full test suite needs a separate dataset; see [Reproducing the experiment](REPRODUCING.md).

## Configure a lab

```sh
cp config.example.toml config.local.toml
```

Set the two VM addresses, SSH user and key path, and remote directory in `config.local.toml`. The file is ignored by Git. The deployment, run, diagnostics, and manifest scripts read the same configuration with `--config config.local.toml`.

## Files

- `src/churn.c`: RDMA experiment program.
- `scripts/deploy.py` and `scripts/run.py`: build, deployment, and run launchers.
- `ledger.py`, `analyzer.py`, and `verify.py`: data recording, analysis, and integrity checks.
- `tests/`: unit and dataset checks.
- `docs/ledger-schema.md`: ledger format.

Raw results are not included in this repository.
