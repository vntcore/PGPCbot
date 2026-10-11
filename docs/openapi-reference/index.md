# The specification

The PGP Chat server describes its Bot API in an OpenAPI 3.1 document. This documentation keeps
an exact copy of it, `openapi.json`. That file is the authority for how the
protocol works; this library follows it, and the [API coverage](../API_COVERAGE.md) page maps
each operation to the code and the tests.

- [Every operation](operations.md) shows each operation as a section of a page, generated from
  the file.
- {download}`Download openapi.json <../openapi.json>` to open it in any OpenAPI viewer or
  code generator.

## Where the server and the document differ

Testing the API showed a few details that the document leaves out or describes only
in part. The library follows the server; the details are listed in the
[research notes](../RESEARCH.md#1-the-protocol-checked-against-the-server) and on the
[API coverage](../API_COVERAGE.md#where-the-spec-and-the-server-differ) page.
