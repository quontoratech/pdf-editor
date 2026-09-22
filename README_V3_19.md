# PDF Text Style Editor V3.19 - Multi-Document Metadata Passthrough

V3.19 removes the single-demo-PDF marker requirement. The editor now reads metadata from **each PDF opened at runtime**. This makes it suitable for a classroom / final-year-project showcase where an examiner gives several different PDFs.

## Metadata and structure behavior

The exporter leaves the complete source Info dictionary and XMP metadata stream
untouched. This includes standard fields, private Info keys, custom XMP
namespaces, dates, and XMP document IDs.

After saving, it verifies that these values still match the source:

- trailer `/ID`
- xref family (classic table or xref stream)
- Catalog role and logical document structure
- complete Info object
- XMP stream dictionary and decoded bytes
- every untouched page content stream

An edited page content stream cannot be identical because its text changed. The
editor merges each edited page into one stream, matches its original Flate or
uncompressed stream policy, and performs a clean compact rewrite. Normal edits
use `garbage=2`; full-line reflow first inlines generated clipping Forms and then
uses `garbage=4` to merge copied duplicate resources. This removes temporary
redaction annotations, appearance streams, and superseded content instead of
leaving their dictionaries inside an object stream. Indirect object numbers,
xref offsets, and exact object serialization can therefore change.
Hybrid-reference PDFs are rejected when edited because PyMuPDF cannot reproduce
that xref layout exactly.

For direct-object xref-stream sources, export rebuilds the final xref stream
from the staged objects' real offsets. When the page set is unchanged, this
preserves the source `/Size`, final xref object number, Flate compression and
decoded width while avoiding an added `/ObjStm` or `Written by MuPDF` header
marker. All of these fields are checked again after reopening the exported PDF.
A real page addition/deletion necessarily changes the page tree and xref
cardinality; validation therefore requires the remaining page streams to stay
exact and still requires Flate xref compression, no object stream, no compressed
objects, and no writer marker.

When no edit was made, export is a byte-for-byte copy of the source.

## Multi-document showcase

There is no hardcoded Creator or Producer and no `PTE-FYP-OWNED-SOURCE`
marker. Open any project-authorized PDF, make an edit, export, then inspect the
generated edit log and validation report. Each file is handled independently.

## Test coverage

`tests/test_multi_document_metadata.py` creates and exports four PDFs with different metadata profiles:

1. Microsoft Word / Microsoft PDF engine
2. LibreOffice Writer / LibreOffice PDF engine
3. Adobe Acrobat / Adobe PDF Library
4. macOS Preview / Quartz PDFContext

The metadata test verifies that Creator and Producer match before/after for all
profiles. `tests/test_pdf_structure_preservation.py` additionally exercises
classic xref tables and xref streams, private Info/XMP data, edited-page cleanup,
and exact untouched-page streams.

Run:

```bash
PYTHONPATH=. .venv/bin/python tests/test_multi_document_metadata.py
PYTHONPATH=. .venv/bin/python tests/test_pdf_structure_preservation.py
```

The original clean-text smoke test is also included.
