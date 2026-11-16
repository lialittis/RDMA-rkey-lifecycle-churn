#!/usr/bin/env python3
"""Bounded binary ledger for the rkey registration-churn experiment.

Format (little-endian throughout), version 1.  See docs/ledger-schema.md
for the full normative specification; src/churn.c implements the identical
writer on the endpoints.

Header (16 bytes):
    8s   magic  b"RKEYCHN1"
    u32  version (1)
    u32  flags   (0)

Record frame:
    u16  payload_len
    u8   kind
    payload (exactly payload_len bytes, fixed per kind)
    u64  record_checksum = first 8 bytes of SHA-256(kind_byte || payload)

Integrity chain:
    c0   = SHA-256(magic || version_le || flags_le)[:8]
    c(i) = SHA-256(c(i-1)_le || record_checksum(i)_le)[:8]
    Checkpoint records (kind 254) and the mandatory trailing Trailer record
    (kind 255) store the chain value accumulated over all records BEFORE
    their own frame.  Truncation therefore fails (missing trailer), a
    dropped record fails (chain mismatch), and a flipped byte fails
    (record checksum mismatch).

Kinds and payload sizes:
    1 registration (92B)   2 deregistration (24B)   3 control-event (36B)
    4 probe (316B)         5 run-meta (80B)
    254 checkpoint (16B)   255 trailer (16B)
"""

import hashlib
import os
import struct

MAGIC = b"RKEYCHN1"
VERSION = 1

K_REGISTRATION = 1
K_DEREGISTRATION = 2
K_CONTROL_EVENT = 3
K_PROBE = 4
K_RUN_META = 5
K_CHECKPOINT = 254
K_TRAILER = 255

REG_PAYLOAD = struct.Struct("<IIIIQIQ32siIQB7x")                  # 92
DEREG_PAYLOAD = struct.Struct("<IiIQ4x")                            # 24
CONTROL_PAYLOAD = struct.Struct("<IIIIBB2xQ8x")                     # 32
PROBE_PAYLOAD = struct.Struct("<24sIB3xiiBBiBBIQIQ32s4x64s64s64sB7xQ")  # 320
META_PAYLOAD = struct.Struct("<B31s8s24sB7xQ")                      # 80
CHAIN_PAYLOAD = struct.Struct("<QII")                               # 16
FRAME_HEAD = struct.Struct("<HB")

PHASE_NAMES = {1: "positive-control", 2: "index-only-probe",
               3: "full-key-live", 4: "full-key-post-deregistration",
               5: "selftest-live", 6: "selftest-post-deregistration"}
PHASE_CODES = {v: k for k, v in PHASE_NAMES.items()}

CONTROL_KIND_NAMES = {1: "run-ready", 2: "descriptor-batch",
                      3: "batch-ack", 4: "quiesce", 5: "probe-request",
                      6: "probe-result", 7: "sample-begin", 8: "run-end",
                      9: "descriptor-received"}
CONTROL_KIND_CODES = {v: k for k, v in CONTROL_KIND_NAMES.items()}

QP_ROLE_NAMES = {0: "control", 1: "fresh", 2: "original"}
QP_ROLE_CODES = {v: k for k, v in QP_ROLE_NAMES.items()}

KIND_NAMES = {K_REGISTRATION: "registration", K_DEREGISTRATION:
              "deregistration", K_CONTROL_EVENT: "control-event",
              K_PROBE: "probe", K_RUN_META: "run-meta",
              K_CHECKPOINT: "checkpoint", K_TRAILER: "trailer"}
KIND_CODES = {v: k for k, v in KIND_NAMES.items()}

_ZERO_SHA = b"0" * 64
_SINGLE = [bytes((i,)) for i in range(256)]


class LedgerIntegrityError(Exception):
    pass


def _sha8(data):
    return hashlib.sha256(data).digest()[:8]


def _chain_step(chain, checksum):
    """chain is an int (u64); checksum is the 8-byte record checksum."""
    return int.from_bytes(_sha8(chain.to_bytes(8, "little") + checksum),
                          "little")


def _chain_init():
    return int.from_bytes(
        _sha8(MAGIC + struct.pack("<II", VERSION, 0)), "little")


def _pack_str(value, width):
    raw = value.encode("ascii") if isinstance(value, str) else bytes(value)
    if len(raw) > width:
        raise ValueError("string %r longer than %d bytes" % (value, width))
    return raw.ljust(width, b"\0")


def _unpack_str(raw):
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def _pack_sha(value):
    if value is None:
        return _ZERO_SHA
    value = value.strip()
    if len(value) > 64 or any(c not in "0123456789abcdefABCDEF"
                              for c in value):
        raise ValueError("bad sha string %r" % value)
    return value.encode("ascii").ljust(64, b"\0")


def _unpack_sha(raw):
    stripped = raw.rstrip(b"\0")
    if not stripped:
        return None
    if len(stripped) == 64 and stripped == b"0" * 64:
        return None
    return stripped.decode("ascii")


def _pack_bool3(value):
    return 2 if value is None else (1 if value else 0)


def _unpack_bool3(code):
    return True if code == 1 else (False if code == 0 else None)


def _pack_registration(rec):
    rkey = rec["rkey"]
    index = rec["index"] if rec.get("index") is not None else \
        (rkey >> 8 if rkey is not None else 0)
    low = rec["low_byte"] if rec.get("low_byte") is not None else \
        (rkey & 0xFF if rkey is not None else 0)
    return REG_PAYLOAD.pack(
        rec["generation"], rkey if rkey is not None else 0,
        index, low, rec["address"], rec["length"], rec["pd_handle"],
        _pack_str(rec["access"], 32), rec["register_rc"], rec["advertise_seq"],
        rec["mono_ns"], 1 if rec.get("target") else 0)


def _pack_deregistration(rec):
    return DEREG_PAYLOAD.pack(rec["generation"], rec["deregister_rc"],
                              rec["quiesce_seq"] if rec.get("quiesce_seq")
                              is not None else 0, rec["mono_ns"])


def _pack_control(rec):
    kind = rec.get("control_kind", "")
    code = CONTROL_KIND_CODES.get(kind, kind if isinstance(kind, int)
                                  else 0)
    lo = rec.get("descriptor_generation")
    hi = rec.get("descriptor_generation_hi", lo)
    return CONTROL_PAYLOAD.pack(rec["seq"], code, lo or 0, hi or 0,
                                1 if rec.get("acked") else 0,
                                0 if rec.get("side", "owner") == "owner"
                                else 1, rec["mono_ns"])


def _pack_probe(rec):
    phase = rec.get("phase", 0)
    phase_code = PHASE_CODES.get(phase, phase if isinstance(phase, int)
                                 else 0)
    wc = rec.get("wc_status")
    qp_state = rec.get("qp_state")
    return PROBE_PAYLOAD.pack(
        _pack_str(rec["probe_id"], 24),
        rec["generation"],
        phase_code,
        rec.get("posting_rc") or 0,
        -1 if wc is None else wc,
        1 if rec.get("timed_out") else 0,
        QP_ROLE_CODES.get(rec.get("qp_role", "control"), 0),
        -1 if qp_state is None else qp_state,
        1 if rec.get("qp_retired_after") else 0,
        0,
        rec.get("rkey") or 0,
        rec["address"], rec["length"], rec["pd_handle"],
        _pack_str(rec.get("access"), 32),
        _pack_sha(rec.get("owner_bytes_sha256_before")),
        _pack_sha(rec.get("owner_bytes_sha256_after")),
        _pack_sha(rec.get("expected_owner_bytes_sha256")),
        _pack_bool3(rec.get("guards_intact")),
        rec["mono_ns"])


def _pack_run_meta(rec):
    return META_PAYLOAD.pack(
        0 if rec.get("endpoint", "owner") == "owner" else 1,
        _pack_str(rec.get("label", ""), 31),
        _pack_str(rec.get("run_role", ""), 8),
        _pack_str(rec.get("stop_reason") or "", 24),
        _pack_bool3(rec.get("allocation_outlived_all_qps")),
        rec["mono_ns"])


_PACKERS = {K_REGISTRATION: _pack_registration,
            K_DEREGISTRATION: _pack_deregistration,
            K_CONTROL_EVENT: _pack_control,
            K_PROBE: _pack_probe,
            K_RUN_META: _pack_run_meta}

_SIZES = {K_REGISTRATION: 92, K_DEREGISTRATION: 24, K_CONTROL_EVENT: 36,
          K_PROBE: 316, K_RUN_META: 80, K_CHECKPOINT: 16, K_TRAILER: 16}

for _code, _st in ((K_REGISTRATION, REG_PAYLOAD), (K_DEREGISTRATION,
        DEREG_PAYLOAD), (K_CONTROL_EVENT, CONTROL_PAYLOAD), (K_PROBE,
        PROBE_PAYLOAD), (K_RUN_META, META_PAYLOAD), (K_CHECKPOINT,
        CHAIN_PAYLOAD), (K_TRAILER, CHAIN_PAYLOAD)):
    assert _st.size == _SIZES[_code], (_code, _st.size, _SIZES[_code])
del _code, _st


class LedgerWriter(object):
    """Append-only binary ledger writer (mirrors the C implementation)."""

    def __init__(self, path, checkpoint_every=None):
        self.path = path
        self.checkpoint_every = checkpoint_every
        self._since_checkpoint = 0
        self._record_count = 0
        self._chain = _chain_init()
        self._closed = False
        self._fh = open(path, "wb")
        self._fh.write(MAGIC)
        self._fh.write(struct.pack("<II", VERSION, 0))

    def append(self, rec):
        if self._closed:
            raise LedgerIntegrityError("ledger already closed")
        code = KIND_CODES.get(rec.get("kind"))
        if code is None or code not in _PACKERS:
            raise ValueError("unknown record kind %r" % (rec.get("kind"),))
        payload = _PACKERS[code](rec)
        if len(payload) != _SIZES[code]:
            raise ValueError("payload size mismatch for %r"
                             % (rec.get("kind"),))
        self._append_frame(code, payload)
        if self.checkpoint_every:
            self._since_checkpoint += 1
            if self._since_checkpoint >= self.checkpoint_every:
                self.checkpoint()

    def checkpoint(self):
        if self._closed:
            raise LedgerIntegrityError("ledger already closed")
        self._append_frame(K_CHECKPOINT,
                           CHAIN_PAYLOAD.pack(self._chain,
                                              self._record_count, 0))
        self._since_checkpoint = 0

    def _append_frame(self, code, payload):
        checksum = _sha8(bytes([code]) + payload)
        self._fh.write(FRAME_HEAD.pack(len(payload), code))
        self._fh.write(payload)
        self._fh.write(checksum)
        self._chain = _chain_step(self._chain, checksum)
        self._record_count += 1

    def close(self):
        if self._closed:
            return
        self._append_frame(K_TRAILER,
                           CHAIN_PAYLOAD.pack(self._chain,
                                              self._record_count, 0))
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._closed:
            if exc_type is None:
                self.close()
            else:
                self._fh.flush()
                self._fh.close()
                self._closed = True
        return False


def _decode(code, payload):
    if code == K_REGISTRATION:
        f = REG_PAYLOAD.unpack(payload)
        return {"kind": "registration", "generation": f[0], "rkey": f[1],
                "index": f[2], "low_byte": f[3], "address": f[4],
                "length": f[5], "pd_handle": f[6],
                "access": _unpack_str(f[7]),
                "register_rc": f[8], "advertise_seq": f[9],
                "mono_ns": f[10], "target": bool(f[11])}
    if code == K_DEREGISTRATION:
        f = DEREG_PAYLOAD.unpack(payload)
        return {"kind": "deregistration", "generation": f[0],
                "deregister_rc": f[1], "quiesce_seq": f[2],
                "mono_ns": f[3]}
    if code == K_CONTROL_EVENT:
        f = CONTROL_PAYLOAD.unpack(payload)
        return {"kind": "control-event", "seq": f[0],
                "control_kind": CONTROL_KIND_NAMES.get(f[1], f[1]),
                "descriptor_generation": f[2] or None,
                "descriptor_generation_hi": f[3] or None,
                "acked": bool(f[4]),
                "side": "requester" if f[5] else "owner",
                "mono_ns": f[6]}
    if code == K_PROBE:
        f = PROBE_PAYLOAD.unpack(payload)
        return {"kind": "probe", "probe_id": _unpack_str(f[0]),
                "generation": f[1],
                "phase": PHASE_NAMES.get(f[2], f[2]),
                "posting_rc": f[3],
                "wc_status": None if f[4] < 0 else f[4],
                "timed_out": bool(f[5]),
                "qp_role": QP_ROLE_NAMES.get(f[6], f[6]),
                "qp_state": None if f[7] < 0 else f[7],
                "qp_retired_after": bool(f[8]), "rkey": f[10],
                "address": f[11], "length": f[12], "pd_handle": f[13],
                "access": _unpack_str(f[14]),
                "owner_bytes_sha256_before": _unpack_sha(f[15]),
                "owner_bytes_sha256_after": _unpack_sha(f[16]),
                "expected_owner_bytes_sha256": _unpack_sha(f[17]),
                "guards_intact": _unpack_bool3(f[18]),
                "mono_ns": f[19]}
    if code == K_RUN_META:
        f = META_PAYLOAD.unpack(payload)
        stop = _unpack_str(f[3])
        return {"kind": "run-meta", "endpoint":
                "requester" if f[0] else "owner",
                "label": _unpack_str(f[1]), "run_role": _unpack_str(f[2]),
                "stop_reason": stop or None,
                "allocation_outlived_all_qps": _unpack_bool3(f[4]),
                "mono_ns": f[5]}
    if code in (K_CHECKPOINT, K_TRAILER):
        f = CHAIN_PAYLOAD.unpack(payload)
        return {"kind": KIND_NAMES[code], "chain": f[0],
                "record_count": f[1]}
    raise LedgerIntegrityError("unknown record kind %d" % code)


class Ledger(object):

    def __init__(self, path):
        self.path = path
        self.records = []
        self.checkpoints = []
        with open(path, "rb") as f:
            self._raw = f.read()
        self._validated = False
        self._parse(self._raw, self.records, self.checkpoints)
        self._validated = True

    @staticmethod
    def _parse(raw, records, checkpoints):
        """Single fused pass: structural framing, every record checksum,
        the checkpoint/trailer chain values and the trailer record count
        are all validated while decoding (identical checks to a separate
        verify pass; fused so a multi-million-record ledger parses once).
        The hot loop avoids per-record byte copies via memoryview slices
        and struct.unpack_from."""
        n = len(raw)
        if n < 16 or raw[:8] != MAGIC:
            raise LedgerIntegrityError("bad header/magic")
        version, _flags = struct.unpack_from("<II", raw, 8)
        if version != VERSION:
            raise LedgerIntegrityError("unsupported version %d" % version)
        sha = hashlib.sha256
        single = _SINGLE
        sizes = _SIZES
        chain = sha(MAGIC + raw[8:16]).digest()[:8]
        offset = 16
        seen_trailer = False
        count = 0
        trailer_records = -1
        append = records.append
        reg_unpack_from = REG_PAYLOAD.unpack_from
        dereg_unpack_from = DEREG_PAYLOAD.unpack_from
        control_unpack_from = CONTROL_PAYLOAD.unpack_from
        count_unpack_from = struct.Struct("<I").unpack_from
        ck_names = CONTROL_KIND_NAMES
        while offset < n:
            if seen_trailer:
                raise LedgerIntegrityError(
                    "data after trailer at offset %d" % offset)
            if offset + 3 > n:
                raise LedgerIntegrityError("truncated frame header")
            plen = raw[offset] | (raw[offset + 1] << 8)
            code = raw[offset + 2]
            end = offset + 11 + plen
            if end > n or sizes.get(code) != plen:
                raise LedgerIntegrityError(
                    "bad frame length at offset %d" % offset)
            poff = offset + 3
            payload = raw[poff:poff + plen]
            stored = raw[poff + plen:end]
            digest8 = sha(single[code] + payload).digest()[:8]
            if digest8 != stored:
                raise LedgerIntegrityError(
                    "record checksum mismatch at offset %d" % offset)
            if code >= K_CHECKPOINT:
                if payload[:8] != chain:
                    raise LedgerIntegrityError(
                        "chain mismatch in %s at offset %d"
                        % (KIND_NAMES[code], offset))
                meta_count = count_unpack_from(payload, 8)[0]
                if code == K_TRAILER:
                    seen_trailer = True
                    trailer_records = meta_count
                else:
                    rec = {"kind": "checkpoint",
                           "chain": int.from_bytes(payload[:8], "little"),
                           "record_count": meta_count}
                    append(rec)
                    checkpoints.append(rec)
            elif code == K_REGISTRATION:
                g, rk, ix, lb, addr, ln, pdh, acc, rc, adv, mono, \
                    tgt = reg_unpack_from(raw, poff)
                append({"kind": "registration", "generation": g,
                        "rkey": rk, "index": ix, "low_byte": lb,
                        "address": addr, "length": ln,
                        "pd_handle": pdh,
                        "access": acc.rstrip(b"\0").decode(
                            "ascii", "replace"),
                        "register_rc": rc, "advertise_seq": adv,
                        "mono_ns": mono, "target": bool(tgt)})
            elif code == K_DEREGISTRATION:
                g, rc, qs, mono = dereg_unpack_from(raw, poff)
                append({"kind": "deregistration", "generation": g,
                        "deregister_rc": rc, "quiesce_seq": qs,
                        "mono_ns": mono})
            elif code == K_CONTROL_EVENT:
                s, ck, lo, hi, acked, side, mono = control_unpack_from(
                    raw, poff)
                append({"kind": "control-event", "seq": s,
                        "control_kind": ck_names.get(ck, ck),
                        "descriptor_generation": lo or None,
                        "descriptor_generation_hi": hi or None,
                        "acked": bool(acked),
                        "side": "requester" if side else "owner",
                        "mono_ns": mono})
            else:
                append(_decode(code, payload))
            chain = sha(chain + stored).digest()[:8]
            count += 1
            offset = end
        if not seen_trailer:
            raise LedgerIntegrityError(
                "missing trailer (truncated or unclosed ledger)")
        if trailer_records != count - 1:
            raise LedgerIntegrityError("trailer record count mismatch")

    def verify(self):
        """Validate the whole file. Parsing already validated every
        record checksum, each checkpoint/trailer chain value and the
        trailer record count in one pass over the retained raw bytes;
        this confirms that validation happened and the bytes are held."""
        if self._raw is None or not self._validated:
            raise LedgerIntegrityError("ledger not validated")
        return True


_PARSE_CACHE = {}


def read_ledger(path):
    """Parse (and cache) a ledger file.

    Caching is keyed by path, size and mtime so repeated reads within one
    process (verification passes, analyzer, recounts) share a single
    parse; any file that changes is reparsed.  Returned Ledger objects are
    shared: callers must not mutate .records."""
    try:
        stat = os.stat(path)
        key = (os.path.abspath(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return Ledger(path)
    cached = _PARSE_CACHE.get(key)
    if cached is None:
        cached = Ledger(path)
        _PARSE_CACHE[key] = cached
    return cached
