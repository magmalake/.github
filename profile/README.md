# magmalake

**magmalake** is an exploration of running data workflows natively in Mojo: an implementation of [Apache Iceberg](https://iceberg.apache.org) and everything underneath it — Parquet, Avro, Thrift, compression codecs, Roaring bitmaps, object storage and threads — built as small, independently usable tins, each validated against the reference implementation of its format and packaged for [mojoshelf.org](https://mojoshelf.org), the Mojo package registry (`shelf add iceberg-mojo`). The stack still links **libzstd**, **liblz4**, **libcurl** and **pthreads**: the claim is no Python and no JVM in the path, not no C.

**➡️ [magmalake.org](https://magmalake.org) is the canonical, up-to-date source** — full performance tables, the correctness bar, upstream bugs found, and current status. This README is a pointer, not a copy: it will drift out of date, the site won't.

## The tins

| tin | what it is | version |
|---|---|---|
| [iceberg](https://github.com/magmalake/iceberg.mojo) | Native Apache Iceberg: metadata v1–v3, snapshots, scan planning, reads, and writes (fast-append, delete, overwrite, expire) | 0.4.4 |
| [parquet](https://github.com/magmalake/parquet.mojo) | The first native Parquet reader/writer in Mojo, Arrow C Data Interface out | 0.3.3 |
| [avro](https://github.com/magmalake/avro.mojo) | Object Container Files, read + write, all four codecs | 0.3.0 |
| [objectstore](https://github.com/magmalake/objectstore.mojo) | FileIO over local, HTTP(S), S3, GCS, Azure; pooled libcurl transport | 0.3.0 |
| [roaring](https://github.com/magmalake/roaring.mojo) | 32/64-bit Roaring bitmaps, Iceberg deletion-vector framing | 0.1.0 |
| [thrift](https://github.com/magmalake/thrift.mojo) | Compact + binary protocol runtime and the `parquet.thrift` structs | 0.1.0 |
| [zstd](https://github.com/magmalake/zstd.mojo) | FFI binding to libzstd | 0.1.1 |
| [lz4](https://github.com/magmalake/lz4.mojo) | FFI binding to liblz4 (block, frame, Hadoop framing) | 0.1.1 |
| [snappy](https://github.com/magmalake/snappy.mojo) | Pure-Mojo Snappy, raw + framing | 0.1.1 |
| [hashes](https://github.com/magmalake/hashes.mojo) | CRC-32, MurmurHash3 x86-32, XXH64 + Iceberg bucket transform | 0.1.0 |
| [threads](https://github.com/magmalake/threads.mojo) | pthreads for Mojo — spawn/join/pin, atomics, `parallel_for` | 0.1.0 |
| [iceberg-rs](https://github.com/magmalake/iceberg-rs.mojo) | Rust cdylib over iceberg-rust, kept as a cross-implementation oracle | 0.1.0 |

All Apache-2.0, CI-tested on stable Mojo 1.0.0 and the current nightly, macOS and Linux.

## A few of the numbers

Apple M4, single core, reproducible via each repo's `pixi run bench`. The rest — every operation, every reference comparison, and how these were found — is on **[magmalake.org](https://magmalake.org)**.

- Parquet read, 1M rows: **4.3 ms — 232M rows/s** (pyarrow: 8.2 ms)
- Iceberg scan, 1M rows (zstd): **35.8 ms — 28M rows/s**
- Avro decode, manifest-shaped records: **19.2M records/s** (fastavro: 1.74M)

## Full detail: [magmalake.org](https://magmalake.org)

The site has the complete performance tables, the correctness bar each tin is gated on, the upstream bugs this surfaced in PyIceberg/pyarrow/iceberg-rust, and what's not yet implemented.

## Proposals and issues

What is proposed but unbuilt lives in **[the issue tracker](https://github.com/magmalake/.github/issues)** (short link: [magmalake.org/issues](https://magmalake.org/issues)). Open right now:

- [#1 — postgres.mojo](https://github.com/magmalake/.github/issues/1): a libpq-backed PostgreSQL tin, which would let the Iceberg SQL catalog run on something other than a local file.

Requests for a tin are welcome there.
