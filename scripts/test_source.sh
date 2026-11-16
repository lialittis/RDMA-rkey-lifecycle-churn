#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON_BIN="${PYTHON:-python3}"
"$PYTHON_BIN" -c 'import tomllib' >/dev/null 2>&1 || {
  echo "Python 3.11 or newer is required; set PYTHON to its executable" >&2
  exit 2
}
PYTHONPATH=tests:. "$PYTHON_BIN" -m unittest -v \
  test_ledger.LedgerModuleApiTests \
  test_analyzer.SyntheticAnalyzerBehaviorTests \
  test_config.LabConfigTests \
  test_verify.VerifySyntheticManifestTests \
  test_verify.VerifyRealManifestTests.test_verify_help_exits_zero \
  test_verify.VerifyRealManifestTests.test_verify_missing_manifest_exits_nonzero \
  test_analyzer.AnalyzerApiTests.test_module_importable_with_analyze
