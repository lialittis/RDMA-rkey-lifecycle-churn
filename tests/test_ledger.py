"""Acceptance tests for the bounded binary ledger (ledger.py module, the
documented schema, and the archived per-run ledgers)."""

import os
import shutil
import tempfile
import unittest

import support
from support import (ALLOCATION_BYTES, BUDGETS, MAX_LEDGER_BYTES,
                     CONTROL_EVENT_FIELDS, DEREGISTRATION_FIELDS,
                     LEDGER_SCHEMA_DOC_KEYWORDS, PROBE_FIELDS,
                     REGISTRATION_FIELDS)


class LedgerModuleApiTests(unittest.TestCase):
    """The ledger module must exist with the pinned read/write/verify API."""

    def test_module_api_surface(self):
        import ledger
        self.assertTrue(isinstance(ledger.MAGIC, (bytes, bytearray))
                        and 1 <= len(ledger.MAGIC) <= 16)
        self.assertIsInstance(ledger.VERSION, int)
        self.assertTrue(issubclass(ledger.LedgerIntegrityError, Exception))
        self.assertTrue(callable(ledger.read_ledger))
        self.assertTrue(hasattr(ledger, "LedgerWriter"))

    def test_writer_roundtrip_checkpoint_and_verify(self):
        import ledger
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "roundtrip.ledger")
            record = {"kind": "registration", "generation": 1,
                      "target": True, "rkey": 0x111, "index": 1,
                      "low_byte": 0x11, "address": 0x1000,
                      "length": ALLOCATION_BYTES, "pd_handle": 7,
                      "access": "LOCAL_WRITE|REMOTE_WRITE",
                      "register_rc": 0, "advertise_seq": 3, "mono_ns": 100}
            with ledger.LedgerWriter(path) as writer:
                writer.append(record)
                writer.checkpoint()
            with open(path, "rb") as f:
                self.assertEqual(f.read(len(ledger.MAGIC)),
                                 bytes(ledger.MAGIC))
            parsed = ledger.read_ledger(path)
            self.assertTrue(parsed.verify())
            kinds = [r["kind"] for r in parsed.records]
            self.assertIn("registration", kinds)
            self.assertIn("checkpoint", kinds)
            stored = [r for r in parsed.records
                      if r["kind"] == "registration"][0]
            for key, value in record.items():
                self.assertEqual(stored.get(key), value,
                                 "registration field %r not preserved" % key)

    def test_verify_rejects_truncated_and_tampered_files(self):
        import ledger
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tamper.ledger")
            with ledger.LedgerWriter(path) as writer:
                for gen in (1, 2, 3):
                    writer.append({"kind": "registration",
                                   "generation": gen, "target": True,
                                   "rkey": 0x100 + gen, "index": 1,
                                   "low_byte": gen, "address": 0x1000,
                                   "length": ALLOCATION_BYTES,
                                   "pd_handle": 7,
                                   "access": "LOCAL_WRITE|REMOTE_WRITE",
                                   "register_rc": 0,
                                   "advertise_seq": 10 * gen,
                                   "mono_ns": 100 * gen})
                writer.checkpoint()
            raw = support.read_bytes(path)

            truncated = os.path.join(tmp, "truncated.ledger")
            with open(truncated, "wb") as f:
                f.write(raw[:-8])
            with self.assertRaises(ledger.LedgerIntegrityError):
                ledger.read_ledger(truncated).verify()

            flipped = bytearray(raw)
            flipped[len(raw) // 2] ^= 0xFF
            tampered = os.path.join(tmp, "tampered.ledger")
            with open(tampered, "wb") as f:
                f.write(bytes(flipped))
            with self.assertRaises(ledger.LedgerIntegrityError):
                ledger.read_ledger(tampered).verify()


class LedgerSchemaDocTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.manifest = support.load_manifest()

    def test_schema_doc_documents_required_content(self):
        arts = support.artifacts_by_role(self.manifest, "ledger-schema")
        self.assertTrue(arts, "missing ledger-schema artifact")
        text = support.read_bytes(
            support.artifact_fs_path(support.REPO_ROOT,
                                     arts[0])).decode("utf-8").lower()
        for keyword in LEDGER_SCHEMA_DOC_KEYWORDS:
            self.assertIn(keyword, text,
                          "ledger schema doc must document %r" % keyword)


class ArchivedLedgerTests(unittest.TestCase):
    """Structural and integrity checks over the real per-run ledgers."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = support.load_manifest()
        cls.runs = support.run_ledger_paths(cls.manifest,
                                            support.REPO_ROOT)

    def test_at_least_one_run_with_ledgers(self):
        self.assertGreaterEqual(len(self.runs), 1,
                                "no run ledgers found in the manifest")

    def test_ledgers_are_bounded_binary_with_magic(self):
        import ledger
        for run, owner, requester in self.runs:
            for path in (owner, requester):
                size = os.path.getsize(path)
                self.assertGreater(size, 0)
                self.assertLess(size, MAX_LEDGER_BYTES,
                                "%s exceeds the bounded ledger size" % path)
                with open(path, "rb") as f:
                    self.assertEqual(f.read(len(ledger.MAGIC)),
                                     bytes(ledger.MAGIC),
                                     "%s lacks the ledger magic" % path)

    def test_ledgers_pass_integrity_verification(self):
        import ledger
        for run, owner, requester in self.runs:
            for path in (owner, requester):
                parsed = ledger.read_ledger(path)
                self.assertTrue(parsed.verify(),
                                "%s fails ledger verification" % path)

    def test_archived_ledger_tamper_is_rejected(self):
        # Negative integrity check on a copy: flipping one byte must fail.
        import ledger
        with tempfile.TemporaryDirectory() as tmp:
            for run, owner, requester in self.runs:
                for src in (owner, requester):
                    copy = os.path.join(tmp, os.path.basename(src))
                    shutil.copyfile(src, copy)
                    raw = bytearray(support.read_bytes(copy))
                    raw[len(raw) // 2] ^= 0x01
                    with open(copy, "wb") as f:
                        f.write(bytes(raw))
                    with self.assertRaises(ledger.LedgerIntegrityError):
                        ledger.read_ledger(copy).verify()

    def test_registration_records_complete_and_self_consistent(self):
        import ledger
        for run, owner, requester in self.runs:
            records = ledger.read_ledger(owner).records
            registrations = [r for r in records
                             if r.get("kind") == "registration"]
            self.assertTrue(registrations,
                            "owner ledger for run %r has no registration "
                            "records" % run.get("label"))
            target = [r for r in registrations if r.get("target") is True]
            self.assertTrue(target, "no target registrations recorded")
            for rec in registrations:
                missing = REGISTRATION_FIELDS - set(rec)
                self.assertFalse(missing,
                                 "registration missing fields %r" % missing)
                rkey = rec["rkey"]
                if rkey is not None:
                    self.assertIsInstance(rkey, int)
                    self.assertTrue(0 <= rkey <= 0xFFFFFFFF)
                    self.assertEqual(rec["index"], rkey >> 8,
                                     "index must decode as rkey >> 8")
                    self.assertEqual(rec["low_byte"], rkey & 0xFF,
                                     "low_byte must decode as rkey & 0xFF")
            # target generations contiguous from 1 over attempts, in order
            generations = [r["generation"] for r in target]
            self.assertEqual(generations,
                             list(range(1, len(generations) + 1)),
                             "target registration generations must be "
                             "contiguous 1..N in ledger order")
            # fixed allocation: constant address/length/PD/access
            for field in ("address", "length", "pd_handle", "access"):
                values = {r[field] for r in target}
                self.assertEqual(len(values), 1,
                                 "target registrations vary %r: %r"
                                 % (field, values))
            self.assertEqual(target[0]["length"], ALLOCATION_BYTES)
            # hard count cap (per-run role cap; smoke runs are capped
            # at 4096, main runs at 600000)
            succeeded = [r for r in target if r["register_rc"] == 0]
            self.assertLessEqual(
                len(succeeded),
                max(BUDGETS["main_max_target_registrations"],
                    BUDGETS["smoke_max_target_registrations"]),
                "successful target registrations exceed the hard cap")

    def test_deregistration_records_quiesced_and_matched(self):
        import ledger
        for run, owner, requester in self.runs:
            records = ledger.read_ledger(owner).records
            succeeded = {r["generation"] for r in records
                         if r.get("kind") == "registration"
                         and r.get("target") is True
                         and r.get("register_rc") == 0}
            dregs = [r for r in records
                     if r.get("kind") == "deregistration"]
            self.assertTrue(dregs, "no deregistration records")
            for rec in dregs:
                missing = DEREGISTRATION_FIELDS - set(rec)
                self.assertFalse(missing,
                                 "deregistration missing fields %r" % missing)
                self.assertIsNotNone(rec["quiesce_seq"],
                                     "every deregistration must record the "
                                     "quiesce barrier sequence")
                self.assertIn(rec["generation"], succeeded,
                              "deregistration for non-succeeded generation")

    def test_probe_records_carry_separated_raw_fields(self):
        import ledger
        for run, owner, requester in self.runs:
            records = ledger.read_ledger(owner).records
            probes = [r for r in records if r.get("kind") == "probe"]
            if probes:
                for rec in probes:
                    missing = PROBE_FIELDS - set(rec)
                    self.assertFalse(missing,
                                     "probe %r missing separated fields %r"
                                     % (rec.get("probe_id"), missing))

    def test_checkpoints_present(self):
        import ledger
        for run, owner, requester in self.runs:
            for path in (owner, requester):
                records = ledger.read_ledger(path).records
                data_records = [r for r in records
                                if r.get("kind") != "checkpoint"]
                if len(data_records) >= 2:
                    checkpoints = [r for r in records
                                   if r.get("kind") == "checkpoint"]
                    self.assertTrue(checkpoints,
                                    "%s has no checkpoint records" % path)

    def test_wall_time_within_hard_budget(self):
        # 900 s from the first owner target registration through final
        # cleanup: recomputed from the owner ledger's own monotonic clock.
        import ledger
        limit_ns = int((BUDGETS["max_wall_seconds"] + 0.5) * 1e9)
        for run, owner, requester in self.runs:
            records = ledger.read_ledger(owner).records
            first = min(r["mono_ns"] for r in records
                        if r.get("kind") == "registration"
                        and r.get("target") is True)
            last = max(r["mono_ns"] for r in records
                       if isinstance(r.get("mono_ns"), int))
            self.assertLessEqual(last - first, limit_ns,
                                 "run %r exceeds the 900 s hard budget on "
                                 "the owner monotonic timeline"
                                 % run.get("label"))

    def test_requester_control_events_ordered_and_acked(self):
        import ledger
        for run, owner, requester in self.runs:
            records = ledger.read_ledger(requester).records
            events = [r for r in records if r.get("kind") == "control-event"]
            self.assertTrue(events,
                            "requester ledger for run %r records no "
                            "control-channel events" % run.get("label"))
            seqs = [e["seq"] for e in events]
            self.assertEqual(seqs, sorted(seqs),
                             "control event sequence not increasing")
            self.assertEqual(len(seqs), len(set(seqs)),
                             "duplicate control sequence numbers")
            for event in events:
                missing = CONTROL_EVENT_FIELDS - set(event)
                self.assertFalse(missing,
                                 "control event missing fields %r" % missing)
            descriptor_events = [e for e in events
                                 if "descriptor" in e.get("control_kind",
                                                          "")]
            self.assertTrue(descriptor_events,
                            "no descriptor delivery events recorded")
            for event in descriptor_events:
                self.assertIs(event["acked"], True,
                              "descriptor delivery must be ACK-sequenced")
                self.assertIsInstance(event["descriptor_generation"], int)


if __name__ == "__main__":
    unittest.main()
