# Contributing

## Setting up

```bash
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -e . --group dev         # needs pip 25.1 or newer
```

## Before you open a pull request

```bash
ruff check src tests examples scripts
ruff format --check src tests examples scripts
mypy
pytest
```

`mypy` runs in `strict` mode on `src/pgpcbot`. Any warning during the tests is an error
(`filterwarnings = error`): if a test expects a warning, it has to say so.

### Tests that need more

- **Integration** (`-m integration`): the tests that drive a real PGP Chat server need a copy
  of the server to start one, so they live with it and not in this repository. The marker stays
  declared here, for a working copy that has both.
- **Interoperability** (`-m interop`): they check the SDK against PGPainless 1.7.6, the phones'
  library. You need Java 21 or newer and `PGPCBOT_PGPAINLESS_CP` set to a classpath with
  pgpainless-core 1.7.6, Bouncy Castle 1.80 (bcprov, bcpg, bcutil), slf4j-api, jsr305 and
  kotlin-stdlib. The CI job in `.github/workflows/ci.yml` shows how to download them.

Without those variables these tests are skipped, they don't fail.

## How the code is organised

- `src/pgpcbot/_spec.py` describes each API operation once. `client.py` has the two shells,
  async and blocking, with the same methods. A new method goes in both:
  `tests/test_parity.py` checks that the signatures match.
- The client-side checks mirror the server's own rules, so a request that passes them is not
  refused for that reason. When the server changes a rule, change it here too, with a
  test.
- Everything that touches OpenPGP goes through `src/pgpcbot/crypto/_lib.py`. A change there
  needs the interoperability tests with PGPainless, not only the unit tests.
- Comments explain why, not what. Code, comments and documentation are in English; keep the
  documentation simple, for readers whose first language is not English.

## Documentation

The API reference is generated from the docstrings, so a docstring is documentation. Write it
in Google style with reStructuredText markup, like the ones already there: a summary line,
then `Args:`, `Returns:`, `Raises:`, `Attributes:` and `Example:` sections as needed. Names in
single backticks become links (`Bot.send_message`); code and values go in double backticks.
`tests/test_docs.py` checks that everything public has a docstring, that every parameter is
described and that the `>>>` examples still run.

Build the site with Sphinx:

```bash
pip install -e . --group docs
sphinx-build -W --keep-going -n -b html docs docs/_build/html
```

then open `docs/_build/html/index.html`. `-n` reports every link that does not resolve and
`-W` turns warnings into errors, as in the CI. To read one page without a browser:
`python -m pydoc pgpcbot.Bot`.

The guides are Markdown pages in `docs/`. The examples in the documentation must work with the
real code: if you change an API, update the docstrings, the pages and the scripts in
`examples/` that use it.

`docs/openapi.json` is an exact copy of the document the server publishes for its Bot API. When
the server's spec changes, copy it again and run `python scripts/openapi_pages.py`: it rewrites
`docs/openapi-reference/operations.md`, and stops if an operation has no SDK method. The CI checks
that the page is up to date.

## Releases

1. update `src/pgpcbot/_version.py` and `CHANGELOG.md`;
2. create a `vX.Y.Z` tag;
3. the `release.yml` workflow builds and checks the packages. Publishing to PyPI needs a manual
   approval of the `pypi` environment.
