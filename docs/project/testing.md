# Tests

## Running them

```bash
pip install -e . --group test
pytest                                   # the unit tests
coverage run -m pytest && coverage report
```

Any warning during the tests is an error (`filterwarnings = error`): a test that expects one
has to say so.

## What is tested, and how

| Kind | Where | What it needs |
|---|---|---|
| unit tests | `tests/test_*.py` | nothing: they run offline, in a few seconds |
| documentation | `tests/test_docs.py`, and the Sphinx build | nothing for the test; the `docs` group for the build |
| interoperability | `tests/interop/` (`-m interop`) | Java 21 and the PGPainless 1.7.6 jars |
| integration | with the server, not in this repository | a copy of the PGP Chat server, which is not public |

Interoperability and integration tests are **skipped** when what they need is missing; they
don't fail.

### The documentation

The API reference is made from the docstrings, so `tests/test_docs.py` treats a gap in them as a
failure: every public class, function, method and property needs a docstring, every parameter
must be in its `Args:` section and every field in `Attributes:`, and every `>>>` example in a
docstring is run and must print what it shows. The CI also builds the site with
`sphinx-build -W -n`, which fails on any warning and on any link that does not resolve.

### The fake API

`tests/fakeapi.py` is an in-memory version of the Bot API, plugged into the clients as an
httpx2 transport. It speaks the real wire format (envelope, error codes, `sendId` replay,
long polling with offsets) and can be told to fail: drop a connection before the server sees
the request, lose the answer after the server handled it, answer `429` with a `Retry-After`,
and so on. Tests check what went over the wire with `fake.calls(...)`.

### Real OpenPGP material

`tests/fixtures/` holds keys and messages **made by PGPainless 1.7.6**, the library the phones
use: Ed25519 and RSA-3072 phone keys, a signed and compressed text message, an unsigned one, a
photo in the app's internal format, a binary attachment, a message for another key, a message
for the bot's old key, and a passphrase-protected bot key. The crypto tests decrypt and verify
these, so they test what phones really send.

### Interoperability with the phones

```bash
export PGPCBOT_PGPAINLESS_CP="/path/pgpainless-core-1.7.6.jar:/path/bcprov-jdk18on-1.80.jar:..."
pytest -m interop
```

`tests/interop/Interop.java` uses PGPainless exactly as the app does. The tests make keys and
messages on one side and open them on the other, in both directions: fingerprints, sealed and
signed text, unsigned text, binary attachments. The CI job downloads the jars from Maven
Central.

### Against the real server

The SDK is also driven against a real server on a temporary database - every operation of the
API, polling, the webhook, an encrypted private chat - with a bot and a test user made for the
run. Those tests need a copy of the server to start one, so they live with it; they are not in
this repository.

## Writing tests

- Use the fixtures in `tests/conftest.py`: `fake`, `api`, `sync_api`, `make_bot`,
  `make_sync_bot`, and the key fixtures.
- Don't sleep: patch the sleep, or wait on an event.
- Never use real credentials. Tokens in tests are made up.
- A test that shows a bug in the library stays, asserting the correct behaviour, marked
  `xfail(strict=True)` with the reason, until the bug is fixed.
