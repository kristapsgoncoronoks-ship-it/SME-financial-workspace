# Native runtime provenance correction — 5 November 2026

The native OCR fixtures identify immutable image
`sha256:addeda248664d6c97363d7f6ac06e59c076ec7555bfad58bd598e2b1f67214fe`.
An actual `python --version` execution in that exact image reports **Python3.14.8**.
Earlier prose describing those same-image profiles as Python3.11 was incorrect.
No separate Python3.11 native matrix is established by that evidence.

The host portable/API qualification used Python3.12.14 and remains a distinct
environment. The image identity, application observations and measurements are
unchanged; this corrects their interpreter attribution without rewriting frozen
artifacts or claiming additional version coverage. It does not change source,
dependencies, runtime images, assertions or gates.

The source-bound original invoice receipt
`ocr-resource-safety/native/frozen-invoice-ordinary-768m.json` identifies the same
immutable image. The current execution and its attribution are preserved in
`/workspace/artifacts/backlog-sixth-wave-2026-11-05/receipt-concurrency/runtime-python-version.txt`
and `runtime-provenance-erratum.json` (SHA256
`e4cd065156ebfd5fa90c403715cacc550eccaa851461964b28f2dc75ebb96932`).
All older qualification bundles remain intact.
