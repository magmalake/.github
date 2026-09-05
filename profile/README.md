# magmalake

**Data infrastructure for Mojo, version 0.x.** A native implementation of [Apache Iceberg](https://iceberg.apache.org) and everything underneath it — Parquet, Avro, Thrift, compression codecs, Roaring bitmaps, object storage, SQL catalogs and threads — built as small, independently usable tins, each gated against the reference implementation of the format it speaks and published on [mojoshelf.org](https://mojoshelf.org) (`pixi shelf add iceberg-mojo`).

Mojo can open an Iceberg table today without reaching back through Python or the JVM. The stack does link a few very mature C libraries — **libzstd**, **liblz4**, **libbrotli**, **libcurl**, **libpq** and **pthreads** — so the claim is no Python and no JVM in the path, not no C.

**➡️ [magmalake.org](https://magmalake.org) is the canonical, up-to-date source.** This README is a pointer, not a copy: it drifts, the site doesn't.

## The tins

| tin | what it is | version |
|---|---|---|
| [iceberg](https://github.com/magmalake/iceberg.mojo) | Native Apache Iceberg: metadata v1–v3, scan planning, reads, writes (fast-append, delete, overwrite, expire), REST and SQL catalogs | 0.6.5 |
| [parquet](https://github.com/magmalake/parquet.mojo) | The first native Parquet reader/writer in Mojo, Arrow C Data Interface out, multi-core reads | 0.5.0 |
| [avro](https://github.com/magmalake/avro.mojo) | Object Container Files, read + write, all four codecs, schema-compiled cursor | 0.3.2 |
| [objectstore](https://github.com/magmalake/objectstore.mojo) | FileIO over local, HTTP(S), S3, GCS, Azure; pooled libcurl transport | 0.3.1 |
| [postgres](https://github.com/magmalake/postgres.mojo) | libpq binding: queries, parameters, COPY, transactions — the deployable SQL catalog | 0.2.0 |
| [sqlite](https://github.com/magmalake/sqlite.mojo) | libsqlite3 binding; the local-development SQL catalog | 0.3.2 |
| [roaring](https://github.com/magmalake/roaring.mojo) | 32/64-bit Roaring bitmaps, Iceberg deletion-vector framing | 0.1.1 |
| [thrift](https://github.com/magmalake/thrift.mojo) | Compact + binary protocol runtime and the `parquet.thrift` structs | 0.1.1 |
| [threads](https://github.com/magmalake/threads.mojo) | pthreads for Mojo — spawn/join/pin, atomics, typed `parallel_for`, worker pools | 0.4.0 |
| [zstd](https://github.com/magmalake/zstd.mojo) | FFI binding to libzstd | 0.1.2 |
| [lz4](https://github.com/magmalake/lz4.mojo) | FFI binding to liblz4 (block, frame, Hadoop framing) | 0.1.1 |
| [brotli](https://github.com/magmalake/brotli.mojo) | FFI binding to libbrotli | 0.1.1 |
| [snappy](https://github.com/magmalake/snappy.mojo) | Pure-Mojo Snappy, raw + framing | 0.1.2 |
| [hashes](https://github.com/magmalake/hashes.mojo) | CRC-32, MurmurHash3 x86-32, XXH64 + Iceberg bucket transform | 0.1.1 |
| [iceberg-rs](https://github.com/magmalake/iceberg-rs.mojo) | Rust cdylib over iceberg-rust, kept as a cross-implementation oracle | 0.1.0 |

All Apache-2.0, CI-tested on stable Mojo 1.0.0 and the current nightly, macOS and Linux.

## A few of the numbers

Apple M4, reproducible via each repo's `pixi run bench`. Medians, with the tails and the full tables on **[magmalake.org/performance](https://magmalake.org/performance)**.

- Parquet read, 1M rows × 4 columns: **4.7 ms** on one core, **2.1 ms** on eight — faster than pyarrow with its own threads
- Parquet read, 100k rows of mixed and nested types: **3.3 ms** on one core, **0.96 ms** on eight
- Avro decode, manifest-shaped records: **19.2M records/s** (fastavro: 1.74M)

Where it loses is on the site too, with the reason: threaded pyarrow is still ahead on nested reads, and the remaining gap is measured rather than estimated.

## Full detail: [magmalake.org](https://magmalake.org)

[Performance](https://magmalake.org/performance) · [Correctness](https://magmalake.org/correctness) · [Status](https://magmalake.org/status) · [Writing](https://magmalake.org/blog)

The site carries the complete tables, the correctness bar each tin is gated on, the upstream bugs this surfaced in PyIceberg, pyarrow and iceberg-rust, and what is not yet implemented.

## Proposals and issues

What is proposed but unbuilt lives in **[the issue tracker](https://github.com/magmalake/.github/issues)** (short link: [magmalake.org/issues](https://magmalake.org/issues)). Requests for a tin are welcome there.
