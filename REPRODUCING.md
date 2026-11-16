# Reproducing the experiment

## Check the source

Run `bash scripts/test_source.sh` from the repository root. It runs the source-only suite (65 tests at this revision) using synthetic data and needs Python 3.11 or newer. If a newer Python is not the default, set `PYTHON` to its executable.

## Run on RXE

The recorded experiment used two AArch64 Ubuntu `6.8.0-147-generic` VMs with active `rxe0`. Building `src/churn.c` requires `gcc`, `pkg-config`, and the `libibverbs` development files on the requester VM.

Copy `config.example.toml` to `config.local.toml`, then set the endpoint addresses, SSH user and key path, and remote directory. The local file is ignored by Git. The SSH private key stays outside the repository, and strict host-key checking remains enabled.

Build and deploy with `python3 scripts/deploy.py --config config.local.toml`. Use `python3 scripts/run.py --help` to inspect the bounded run options: `--role smoke` is the optional short diagnostic auxiliary (user-facing name: shakedown, at most 4,096 successful owner target registrations) and `--role main` is the primary run (at most 600,000). Across all attempts of one run plan the hard ceiling is 604,096 successful target registrations and 900 s from the first registration through final cleanup. Pass the same config path to `scripts/run.py`, `scripts/posthoc_diagnostics.py`, and `scripts/make_manifest.py`. The manifest checks accept a main-only primary result; the optional smoke run is never merged into the main run's denominators.

After the runs, produce `results/analysis.json` with `analyzer.py`, write `results/report.md`, then build the manifest with `scripts/make_manifest.py --config config.local.toml` and verify it:

```sh
python3 verify.py --strict run-manifest.json
python3 scripts/check_summary.py docs/reproduction-results.md results/analysis.json
python3 -m unittest discover -s tests -v
```

## Verify saved results

The dataset is distributed separately. Its manifest pins the source file hashes, so use the matching source version. For the earlier lab dataset, check out the `original-run` tag before placing its `run-manifest.json` and `results/` directory at the repository root; that frozen revision requires a pilot run and its archived verification is re-run only there. Then run:

```sh
python3 verify.py --strict run-manifest.json
python3 -m unittest discover -s tests -v
```

The manifest checks artifact sizes and SHA-256 hashes. Without the dataset, use `bash scripts/test_source.sh` instead; the full suite will fail because its inputs are absent.
