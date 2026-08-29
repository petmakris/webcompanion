# webcompanion

A local always-on companion server: addressable items, comment threads, and
an event queue.

`webcompanion` is a standalone, standard-library-only Python package and CLI.
It runs as a local HTTP daemon that Claude Code plugins and IDE integrations
talk to over a small versioned HTTP contract.

## Requirements

- Python 3.9+
- macOS or Linux (POSIX only; Windows is not supported)
- No runtime dependencies — everything needed ships in the standard library

## Install

```bash
pip install -e .
```

## Usage

```bash
webcompanion --version
```

## Development

```bash
pip install -e .
python3 -m pytest -q
```
