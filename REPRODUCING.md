# Reproducing the experiment

## Check the source

Run `bash scripts/test_source.sh` from the repository root. It runs 16 tests using synthetic data and needs only Python 3.

## Run on RXE

The recorded experiment used two AArch64 Ubuntu `6.8.0-147-generic` VMs with active `rxe0`. Building `src/churn.c` requires `gcc`, `pkg-config`, and the `libibverbs` development files on the requester VM.

Before a new run, set the endpoint addresses, SSH user and key path, and remote directory in `scripts/deploy.py`, `scripts/run.py`, `scripts/posthoc_diagnostics.py`, and `scripts/make_manifest.py`. Then build and deploy with `python3 scripts/deploy.py`; use `python3 scripts/run.py --help` to inspect the bounded run options. Record your configuration and results as a new experiment. The existing manifest checks expect both pilot and main run records.

## Verify saved results

The dataset is distributed separately. Place its `run-manifest.json` and `results/` directory at the repository root, then run:

```sh
python3 verify.py --strict run-manifest.json
python3 -m unittest discover -s tests -v
```

The manifest checks artifact sizes and SHA-256 hashes. The recorded dataset passes 47 artifact checks and 56 tests. Without the dataset, use `bash scripts/test_source.sh` instead; the full suite will fail because its inputs are absent.
