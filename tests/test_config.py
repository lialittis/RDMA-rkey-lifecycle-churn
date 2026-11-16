"""Offline checks for the shared lab configuration."""

from pathlib import Path
import tempfile
import unittest

from lab_config import ConfigError, load_config


EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


class LabConfigTests(unittest.TestCase):
    def test_example_config_and_ssh_arguments(self):
        config = load_config(EXAMPLE)
        self.assertEqual(config.owner_ip, "192.0.2.12")
        self.assertEqual(config.requester_ip, "192.0.2.11")
        self.assertEqual(config.target(config.owner_ip), "ubuntu@192.0.2.12")
        args = config.ssh_args()
        self.assertEqual(args[0], "ssh")
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn("BatchMode=yes", args)
        self.assertIn(config.ssh_identity_file, args)
        self.assertEqual(config.ssh_args("scp")[0], "scp")

    def test_rejects_equal_hosts_and_shell_metacharacters(self):
        source = EXAMPLE.read_text()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(source.replace('owner_ip = "192.0.2.12"',
                                           'owner_ip = "192.0.2.11"'))
            with self.assertRaises(ConfigError):
                load_config(path)
            path.write_text(source.replace('/tmp/rxe-churn', '/tmp/x;rm'))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_missing_file_has_actionable_error(self):
        with self.assertRaisesRegex(ConfigError, "cannot load"):
            load_config("/no/such/rxe-churn-config.toml")
