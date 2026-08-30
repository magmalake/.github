# magmalake

**Data lake building blocks in Mojo.** A native, pure-Mojo implementation of [Apache Iceberg](https://iceberg.apache.org) and everything underneath it — Parquet, Avro, Thrift, compression codecs, Roaring bitmaps, object storage, and threads — built as small, independently usable tins, each validated against the reference implementation of its format.

Before magmalake, none of this existed for Mojo: no Parquet page decode, no Avro, no zstd/snappy/lz4, no Roaring, no SHA-256, no table format anywhere in the ecosystem. Every Mojo dataframe reached Parquet through PyArrow. Now:

```mojo
from iceberg import Catalog

var table = Catalog.rest("https://polaris.example/api/catalog").load("db.events")
var rows = table.scan().filter('["=", "region", "eu"]').select(["id", "amount"]).to_table()
```

— metadata, manifests, scan planning, Parquet decode, deletes and deletion vectors, S3 IO, and the REST catalog, with no Python and no JVM in the path.

## The stack

```
                      ┌───────────────────────────────┐
                      │           iceberg             │  reads v1–v3 (deletes, DVs, schema
                      │  metadata · plan · read · write │  evolution) · fast-append · delete
                      └──────┬──────────┬─────────┬───┘  (CoW + MoR) · overwrite · commit
                             │          │         │
             ┌───────────────┤          │         ├───────────────┐
        ┌────▼────┐    ┌─────▼─────┐ ┌──▼──────┐ ┌▼───────────────┐
        │ parquet │    │   avro    │ │ roaring │ │  objectstore   │
        │ r/w     │    │ OCF r/w   │ │ 32/64 + │ │ local·HTTP·S3  │
        └──┬───┬──┘    └─────┬─────┘ │ DV blob │ │ GCS·Azure·SigV4│
           │   │             │       └─────────┘ └───────┬────────┘
      ┌────▼┐ ┌▼──────────────▼────┐                ┌────▼─────┐
      │thrift│ │ zstd · lz4 · snappy│                │  hashes  │
      └─────┘ └────────────────────┘                └──────────┘
                        threads — pthreads · atomics · parallel_for
```

| tin | what it is | validated against |
|---|---|---|
| [iceberg](https://github.com/magmalake/iceberg.mojo) | Native Apache Iceberg: metadata v1–v3, snapshots, scan planning, reads (position/equality deletes, v3 deletion vectors, schema evolution, nested types), fast-append, row-level delete (copy-on-write and merge-on-read), overwrite, REST/filesystem commit | PyIceberg and DuckDB, cell-exact, both directions |
| [parquet](https://github.com/magmalake/parquet.mojo) | The first native Parquet reader/writer in Mojo — every physical/logical type, nested, all encodings, page index, bloom filters, field-id projection, Arrow C Data Interface out | pyarrow, value-exact on 33 fixtures; pyarrow reads every file we write |
| [avro](https://github.com/magmalake/avro.mojo) | Object Container Files, read + write, null/deflate/snappy/zstd | fastavro, both directions |
| [thrift](https://github.com/magmalake/thrift.mojo) | Compact + binary protocol runtime and all `parquet.thrift` structs | Apache Thrift byte-identical |
| [objectstore](https://github.com/magmalake/objectstore.mojo) | FileIO over local, HTTP(S) range reads, S3 (SigV4, vended credentials, presigned, multipart), GCS, Azure; pooled libcurl transport; pure-Mojo SHA-256/HMAC | AWS SigV4 test suite 37/37; MinIO end-to-end in CI |
| [roaring](https://github.com/magmalake/roaring.mojo) | 32/64-bit Roaring bitmaps, portable serialization, Iceberg deletion-vector framing | pyroaring, byte-exact |
| [zstd](https://github.com/magmalake/zstd.mojo) · [lz4](https://github.com/magmalake/lz4.mojo) | FFI bindings to libzstd / liblz4 (block, frame, Hadoop framing) | Python zstandard / lz4 |
| [snappy](https://github.com/magmalake/snappy.mojo) | Pure-Mojo Snappy, raw + framing | python-snappy, byte-exact |
| [hashes](https://github.com/magmalake/hashes.mojo) | CRC-32, MurmurHash3 x86-32, XXH64 + Iceberg bucket transform | zlib/mmh3/xxhash + the Iceberg spec vectors |
| [threads](https://github.com/magmalake/threads.mojo) | pthreads for Mojo — spawn/join/pin, atomics that bridge the stable/nightly `std.atomic` split, `parallel_for` | contended-count and visibility proofs |
| [iceberg-rs](https://github.com/magmalake/iceberg-rs.mojo) | Rust cdylib over iceberg-rust — no longer required for any operation; kept as a cross-implementation oracle | PyIceberg via a shared catalog |

All tins are on [mojoshelf](https://mojoshelf.org) (`shelf add iceberg-mojo`), Apache-2.0, and CI-tested on **stable Mojo 1.0.0 and the current nightly**, macOS and Linux.

## Performance

Apple M4, single core unless noted. Same machine, same files, reproducible via each repo's `pixi run bench`.

| operation | magmalake | reference |
|---|---|---|
| Parquet read, 1M rows (int64/double/dict) | **4.3 ms — 232M rows/s** | pyarrow 8.2 ms (we are 1.9× faster) |
| Parquet write, 1M rows | 42 ms | pyarrow 31 ms |
| Parquet read, 4 cores (`threads.parallel_for` over row groups) | 660M rows/s | memory-bandwidth-bound |
| Iceberg append, 1M rows (files + manifests + commit) | 233 ms | PyIceberg ~150 ms |
| Iceberg scan, 1M rows (zstd) | **35.8 ms — 28M rows/s** (12.1 ms on 4 workers) | pyarrow single-thread 26.8 ms on the same files; PyIceberg 7.9 ms (its thread pool) |
| zstd decompress (libzstd FFI) | 10–14 GB/s | — |
| lz4 block/frame (liblz4 FFI) | 8–19 GB/s | — |
| snappy decompress (pure Mojo) | up to 20 GB/s incompressible, ~3 GB/s compressible | — |
| CRC-32 / murmur3 / XXH64 (pure Mojo) | 1.2–1.5 GB/s | — |
| Parquet footer, 1,000 cols × 50 row groups | 78 ms read, 8 ms write | — |
| HTTP range read, reused connection | 0.15 ms/request local, 0.52 ms signed S3 (MinIO) | 19× / 11× vs fresh connections |
| SHA-256 (pure Mojo) | 60 MB/s | — |

## Correctness bar

Every format tin is gated on the reference implementation, not on its own tests:

- Iceberg scans: row-sets **cell-exact vs PyIceberg (48/48 filter cases) and DuckDB**, including deletes, deletion vectors and schema evolution; tables we write are read back cell-exact by both, and PyIceberg can append to tables we created.
- Parquet: every value of every column of every fixture equal to pyarrow; our Arrow export imports through `pyarrow.Array._import_from_c`.
- SigV4: 37/37 of the official AWS test-suite vectors; S3 verified end-to-end against MinIO **in CI**.
- Building this surfaced upstream bugs in PyIceberg (v3 manifest schemas on both read and write paths), pyarrow (statistics under `list<struct>`, codec/logical-type reporting) and iceberg-rust (metrics pruning) — documented in the repos.

## Status

Active, v0.x. Reads are complete for format v1–v3; writes cover create / append / delete (CoW + MoR, deletion vectors) / overwrite / dynamic partition overwrite / snapshot expiry. Not yet: compaction (`rewrite_manifests`), encryption, Brotli, predicates inside list/map elements. The spec's v4 drafts (relative paths, `content_stats`) are tracked.
