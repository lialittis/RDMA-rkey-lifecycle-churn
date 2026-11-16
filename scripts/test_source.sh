#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHONPATH=tests:. python3 -m unittest -v \
  test_ledger.LedgerModuleApiTests \
  test_analyzer.SyntheticAnalyzerBehaviorTests \
  test_verify.VerifySyntheticManifestTests \
  test_verify.VerifyRealManifestTests.test_verify_help_exits_zero \
  test_verify.VerifyRealManifestTests.test_verify_missing_manifest_exits_nonzero \
  test_analyzer.AnalyzerApiTests.test_module_importable_with_analyze
