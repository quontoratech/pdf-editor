# PDF Text Style Editor V3.19 - Multi-Document Metadata Passthrough

V3.19 removes the single-demo-PDF marker requirement. The editor now reads metadata from **each PDF opened at runtime**. This makes it suitable for a classroom / final-year-project showcase where an examiner gives several different PDFs.

## Metadata behavior

For ordinary authorized documents, these fields are preserved per source file when present:

- Title
- Author
- Subject
- Keywords
- CreationDate
- Creator
- Producer
- XMP CreatorTool
- XMP Producer
- safe descriptive XMP fields

`ModDate` is refreshed because the output was edited. Low-level PDF serialization fingerprints and XMP DocumentID / InstanceID are not cloned.

A conservative provenance guard keeps explicit sensitive institutional issuer/system identities out of Creator/Producer passthrough.

## Multi-document showcase

There is no hardcoded Creator or Producer and no `PTE-FYP-OWNED-SOURCE` marker. Open any ordinary project-authorized PDF, make an edit, export, then compare the source and output metadata. Repeat with the next PDF. Each file is handled independently.

## Test coverage

`tests/test_multi_document_metadata.py` creates and exports four PDFs with different metadata profiles:

1. Microsoft Word / Microsoft PDF engine
2. LibreOffice Writer / LibreOffice PDF engine
3. Adobe Acrobat / Adobe PDF Library
4. macOS Preview / Quartz PDFContext

The test verifies that Creator and Producer match before/after for all four ordinary documents. It also verifies that the protected-provenance guard falls back to the editor identity for a sensitive institutional profile.

Run:

```bash
python -m tests.test_multi_document_metadata
```

The original clean-text smoke test is also included.
