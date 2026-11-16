# Churn experiment binary ledger — schema and checksums

The per-run ledgers (`owner.ledger`, `requester.ledger`) are append-only
fixed-layout binary files written live by `src/churn.c` and read/verified
by `ledger.py` (and through it by `analyzer.py` and the tests). The two
implementations are byte-identical by construction; `tests/test_ledger.py`
holds the C output to the same integrity checks.

## Why binary

One full main attempt registers up to 1,048,576 target MRs. A JSONL event
stream would cost hundreds of bytes per registration; the fixed records
below cost 99 bytes per registration+deregistration pair on the wire
(92+24 payloads plus 11 bytes of framing each). Volume stays bounded and
parse time stays linear.

## File layout

All integers are little-endian. Every record frame is:

| bytes | field |
| --- | --- |
| 2 | `payload_len` (u16, fixed per kind) |
| 1 | `kind` (u8) |
| `payload_len` | payload (fixed size per kind) |
| 8 | `record_checksum` (u64) |

`record_checksum` = first 8 bytes of `SHA-256(kind_byte || payload)`.

Header (16 bytes, written once at open): the 8-byte magic `RKEYCHN1`,
u32 version `1`, u32 flags `0`.

## Integrity chain, checksums and checkpoints

A hash chain binds every record into one order:

- `c0 = SHA-256(magic || version_le || flags_le)[:8]`
- `c(i) = SHA-256(c(i-1)_le || record_checksum(i)_le)[:8]`

Kinds:

- **registration** = 1, payload 92 bytes
- **deregistration** = 2, payload 24 bytes
- **control-event** = 3, payload 36 bytes
- **probe** = 4, payload 316 bytes
- **run-meta** = 5, payload 80 bytes
- **checkpoint** = 254, payload 16 bytes: `u64 chain_before`, `u32
  record_count`, `u32 pad`
- **trailer** = 255, payload identical in shape to checkpoint; written
  exactly once at close, as the last record

`chain_before` is the chain value accumulated over all records *before*
that frame. The writer emits a checkpoint every 4096 appended records and
once more immediately before the trailer, so a corrupted region is
localized to a checkpoint interval. Verification (in any order of
severity):

1. header magic/version;
2. each frame's `payload_len` equals the kind's fixed size and the file
   ends exactly at the trailer (truncation, unclosed run, or trailing
   garbage fails);
3. each frame's `record_checksum` matches SHA-256 of kind+payload (a
   flipped byte fails);
4. each checkpoint/trailer `chain_before` equals the recomputed chain at
   that offset (dropped or reordered records fail);
5. the trailer's `record_count` equals the number of records before it.

The chain is an integrity check against accidental corruption and
truncation, not a cryptographic tamper-proof seal.

## Record payloads

### registration (92 bytes)

`u32 generation` · `u32 rkey` (full 32-bit key) · `u32 index` (= rkey >>
8, stored redundantly as the decoded index) · `u32 low_byte` (the decoded
low byte of the key = rkey & 0xFF) · `u64 address` · `u32 length` ·
`u64 pd_handle` · `char access[32]`
(actual ibv access flags, decimal string) · `i32 register_rc` (0 =
success; errno otherwise) · `u32 advertise_seq` (this generation's
position in the ordered descriptor advertisement stream = generation) ·
`u64 mono_ns` (host monotonic timestamp) · `u8 target` (1 for the churn
target MRs; 0 for setup MRs such as the requester source) · 7 pad bytes.

Target generations are contiguous from 1 in file order. Address, length,
PD and access are constant across all target registrations of a run (the
fixed allocation).

### deregistration (24 bytes)

`u32 generation` · `i32 deregister_rc` · `u32 quiesce_seq` (control-event
sequence of the quiesce barrier that preceded this deregistration; never
zero) · `u64 mono_ns` · 4 pad bytes.

### control-event (36 bytes)

`u32 seq` (strictly increasing per endpoint ledger) · `u32 control_kind`
· `u32 descriptor_lo` · `u32 descriptor_hi` (0 = none) · `u8 acked` ·
`u8 side` (0 owner, 1 requester) · `u64 mono_ns` · 8 pad bytes.

control_kind: 1 run-ready, 2 descriptor-batch (owner send), 3 batch-ack,
4 quiesce, 5 probe-request, 6 probe-result, 7 sample-begin, 8 run-end,
9 descriptor-received (requester receipt).

Descriptor batches are ACK-gated: the owner registers a batch of 4096
generations, sends their keys, and waits for the ACK before registering
the next batch. The owner's batch-ack control-events therefore prove, in
the owner's own causal (file) order, that the requester held every
descriptor of earlier batches before later generations were registered.

### probe (316 bytes)

`char probe_id[24]` · `u32 generation` · `u8 phase` (1 positive-control,
2 index-only-probe, 3 full-key-live, 4 full-key-post-deregistration,
5 selftest-live, 6 selftest-post-deregistration) · `i32 posting_rc` ·
`i32 wc_status` (-1 = no completion) · `u8 timed_out` · `u8 qp_role`
(0 control, 1 fresh, 2 original) · `i32 qp_state` (ibv QP state, -1
unknown) · `u8 qp_retired_after` · `u8 pad` · `u32 rkey` (the key carried
by the probe) · `u64 address` · `u32 length` · `u64 pd_handle` ·
`char access[32]` · 4 pad · `char sha_before[64]` · `char sha_after[64]`
· `char sha_expected[64]` (SHA-256 hex of the full 4096-byte allocation;
64 ASCII zeros = unknown) · `u8 guards_intact` (1 intact, 0 broken, 2
unknown) · 7 pad · `u64 mono_ns`.

Posting return code, WC status, QP state, owner bytes, guards, PD,
address, access and registration generation are kept in these separate
fields so no classification can silently conflate them.

### run-meta (80 bytes)

`u8 endpoint` (0 owner, 1 requester) · `char label[31]` ·
`char run_role[8]` ("pilot"/"main") · `char stop_reason[24]` ("" while
running; the final record carries the exact stop) ·
`u8 allocation_outlived_all_qps` (1 yes after QPs were destroyed while
the allocation was still live, 2 no, 0 unknown) · 7 pad · `u64 mono_ns`.

## Interpretation limits carried by the format

- `advertise_seq` and the descriptor-received events prove the requester
  *held* a saved descriptor before a later matching generation was
  registered. They do not claim the descriptor was held while the saved
  generation's own MR was live: batches are delivered after their
  registrations (and deregistrations) completed.
- Timeout/flush/transport probe outcomes stay `wc_status`/`timed_out`
  facts; the analyzer classifies them indeterminate, never as rkey
  rejections.
- The device MR allocator is device-global; other device users may
  advance it. Nothing in this ledger excludes them.
