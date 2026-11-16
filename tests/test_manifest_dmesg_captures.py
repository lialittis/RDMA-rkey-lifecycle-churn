"""Focused tests that run-manifest.json indexes every preserved raw
kernel-ring capture (owner/requester dmesg before/after, per run) with
exact path, size and SHA-256, and that strict verification covers them
(2026-11 cross-review issue 2: the eight historical captures were
preserved but absent from the manifest)."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

import support

DMESG_SIDES = ("owner", "requester")
DMESG_PHASES = ("before", "after")


def dmesg_name(side, phase):
    return "%s-dmesg-%s.txt" % (side, phase)


def write_dmesg_captures(root, run_dirs, content_for):
    """Create the eight raw capture files under a built results tree."""
    for dirname in run_dirs:
        for side in DMESG_SIDES:
            for phase in DMESG_PHASES:
                path = os.path.join(root, "results", dirname,
                                    dmesg_name(side, phase))
                with open(path, "wb") as f:
                    f.write(content_for(dirname, side, phase))


def run_make_manifest(root, config_path):
    make_manifest = support.load_script("make_manifest_dmesg",
                                        "make_manifest.py")
    make_manifest.REPO = root
    argv = sys.argv
    sys.argv = ["make_manifest.py", "--config", config_path]
    try:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = make_manifest.main()
    finally:
        sys.argv = argv
    return rc


def load_manifest(root):
    with open(os.path.join(root, "run-manifest.json"),
              encoding="utf-8") as f:
        return json.load(f)


def dmesg_artifacts(manifest):
    return [a for a in manifest["artifacts"]
            if a["role"] == "diagnostics"
            and a["path"].endswith(".txt")]


class ManifestDmesgCaptureTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="churn-mkm-dmesg-")
        # tree A: with the eight raw capture files (non-empty, unique
        # bytes so sizes and hashes are distinctive)
        cls.with_caps = os.path.join(cls.tmp, "with-captures")
        cls.config_path, expected = support.build_results_tree(cls.with_caps)
        cls.run_dirs = expected["run_dirs"]
        write_dmesg_captures(
            cls.with_caps, cls.run_dirs,
            lambda d, s, p: ("capture %s %s %s\n" % (d, s, p)).encode())
        # tree B: no capture files (back-compat: synthetic trees simply
        # omit them)
        cls.without_caps = os.path.join(cls.tmp, "without-captures")
        shutil.copytree(cls.with_caps, cls.without_caps)
        for dirname in cls.run_dirs:
            for side in DMESG_SIDES:
                for phase in DMESG_PHASES:
                    os.remove(os.path.join(cls.without_caps, "results",
                                           dirname,
                                           dmesg_name(side, phase)))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_manifest_indexes_all_eight_captures_exactly(self):
        self.assertEqual(run_make_manifest(self.with_caps, self.config_path),
                         0)
        manifest = load_manifest(self.with_caps)
        arts = dmesg_artifacts(manifest)
        self.assertEqual(len(arts),
                         len(self.run_dirs) * len(DMESG_SIDES)
                         * len(DMESG_PHASES),
                         "expected 8 dmesg capture artifacts, got %d"
                         % len(arts))
        seen = set()
        for art in arts:
            path = art["path"]
            seen.add(path)
            self.assertIn(art.get("run"), ("smoke", "main"))
            self.assertIn(os.path.basename(path),
                          [dmesg_name(s, p) for s in DMESG_SIDES
                           for p in DMESG_PHASES])
            fs_path = os.path.join(self.with_caps, path)
            self.assertTrue(os.path.isfile(fs_path))
            self.assertEqual(art["bytes"], os.path.getsize(fs_path))
            self.assertEqual(art["sha256"], support.sha256_file(fs_path))
        expected_paths = {"results/%s/%s" % (dirname, dmesg_name(side, phase))
                          for dirname in self.run_dirs
                          for side in DMESG_SIDES
                          for phase in DMESG_PHASES}
        self.assertEqual(seen, expected_paths,
                         "every preserved capture must be indexed with "
                         "its exact path")
        # and the manifest verifies
        result = support.run_verify(
            os.path.join(self.with_caps, "run-manifest.json"), strict=True)
        self.assertEqual(result.returncode, 0,
                         result.stderr.decode(errors="replace"))

    def test_strict_verification_covers_the_captures(self):
        # tampering one raw capture after manifest generation must fail
        # strict verification (size+hash pinning reaches the captures)
        root = os.path.join(self.tmp, "tampered")
        shutil.copytree(self.with_caps, root)
        victim = os.path.join(root, "results", self.run_dirs[-1],
                              dmesg_name("owner", "after"))
        with open(victim, "ab") as f:
            f.write(b"tampered")
        result = support.run_verify(os.path.join(root, "run-manifest.json"),
                                    strict=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dmesg-after.txt",
                      result.stderr.decode(errors="replace"))

    def test_trees_without_captures_still_produce_valid_manifests(self):
        self.assertEqual(
            run_make_manifest(self.without_caps, self.config_path), 0)
        manifest = load_manifest(self.without_caps)
        self.assertEqual(dmesg_artifacts(manifest), [])
        result = support.run_verify(
            os.path.join(self.without_caps, "run-manifest.json"),
            strict=True)
        self.assertEqual(result.returncode, 0,
                         result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
