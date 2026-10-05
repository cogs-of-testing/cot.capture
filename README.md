# cot.capture

Building blocks for capturing output at different levels: Python's
`sys.stdout`/`sys.stderr`/`sys.stdin` slots and the process's file
descriptors, with proxies that are closed when their scope ends and say who
created them when misused afterwards.

Early, and not released. The design is in
[docs/design/streams.md](docs/design/streams.md); the research it starts from
is [docs/research.md](docs/research.md).
