# Installation

## What you need

- **Python 3.11 or newer.** The library is tested on 3.11, 3.12, 3.13 and 3.14.
- **Linux, macOS or Windows.** The two OpenPGP engines (pysequoia and rpgp-py) come as
  prebuilt wheels for these systems, so you don't need a compiler.
- A PGP Chat bot and its token. You make the bot in the app: Settings → Account → My bots, or
  write to `@bot`.

## Install

```bash
pip install PGPCbot
```

It is a good idea to use a virtual environment:

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install PGPCbot
```

### Optional extras

| Extra | Adds | When you need it |
|---|---|---|
| `PGPCbot[webhook]` | uvicorn | to serve a webhook without another web framework |
| `PGPCbot[http2]` | HTTP/2 support for httpx2 | only if your server or proxy needs HTTP/2 |

```bash
pip install "PGPCbot[webhook]"
```

FastAPI and Starlette are not dependencies: install them yourself if you use them.

### From the source code

From the project folder:

```bash
pip install .          # a normal install
pip install -e .       # an editable install, to work on the library
```

## Check that it works

```bash
python -c "import pgpcbot; print(pgpcbot.__version__)"
pgpcbot --version
```

With a token in `PGPCHAT_BOT_TOKEN`, this asks the server who the bot is:

```bash
pgpcbot whoami
```

## What gets installed

| Package | Why | License |
|---|---|---|
| `httpx2` | HTTP client, sync and async | BSD-3-Clause |
| `pysequoia` | OpenPGP: encrypts and decrypts messages (Sequoia PGP) | Apache-2.0, with Sequoia under LGPL-2.0-or-later |
| `rpgp-py` | OpenPGP: makes keys and checks signatures (rPGP) | MIT |

The [research page](../RESEARCH.md) explains why these were chosen.

## Problems

**`pip` tries to compile Rust code.** There is no prebuilt wheel for your platform or your
Python version (for example a very new Python, or an unusual CPU). Use a Python version from
the list above, or install a Rust toolchain so pip can build the wheel.

**`SSL: CERTIFICATE_VERIFY_FAILED`.** httpx2 uses your operating system's certificate store.
In a minimal container, install the CA certificates (`ca-certificates` on Debian and Alpine),
or point `SSL_CERT_FILE` to a certificate bundle.

**`pgpcbot: command not found`.** The virtual environment is not active, or its `bin` (or
`Scripts` on Windows) folder is not on your `PATH`. `python -m pgpcbot` works too.
