from pathlib import Path
import json
import re
import tempfile

import pymupdf

from app.clean_edit_session import CleanEditSession, APP_NAME
from app.models import FontCandidate
from app.pdf_engine import PdfEngine


def first_span(page, exact):
    engine = PdfEngine()
    for span in engine.extract_spans(page):
        if span.text == exact:
            return span
    raise AssertionError(f"span not found: {exact!r}")


def page_object(doc, page_index: int) -> str:
    page = doc[page_index]
    return doc.xref_object(page.xref, compressed=False)


def main():
    with tempfile.TemporaryDirectory(prefix="ptse_v319_test_") as td:
        td = Path(td)
        src = td / "source.pdf"
        out = td / "edited.pdf"

        # Use Times-Roman so Helvetica is not present in the source. This lets
        # the test prove that export no longer injects disclosure-only Helvetica.
        d = pymupdf.open()
        p1 = d.new_page(width=612, height=792)
        p1.insert_text((72, 100), "Original Customer", fontsize=11, fontname="tiro")
        p1.insert_text((72, 130), "Period: January 1 to January 31", fontsize=11, fontname="tiro")
        p1.insert_text((72, 160), "Balance: $100.00", fontsize=11, fontname="tiro")
        p2 = d.new_page(width=612, height=792)
        p2.insert_text((72, 80), "Original Customer | January 1 to January 31", fontsize=9, fontname="tiro")
        d.set_metadata({
            "title": "Quarterly Account Summary",
            "author": "Example Author",
            "subject": "Original Subject",
            "keywords": "alpha; beta; PTE-FYP-OWNED-SOURCE",
            "creator": "Original Creator App",
            "producer": "Original Producer Engine",
            "creationDate": "D:20200102030405+00'00'",
            
        })
        d.set_xml_metadata(
            '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:pdf="http://ns.adobe.com/pdf/1.3/" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
            'xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/">'
            '<dc:title><rdf:Alt><rdf:li xml:lang="x-default">XMP Account Summary</rdf:li></rdf:Alt></dc:title>'
            '<dc:creator><rdf:Seq><rdf:li>Example Author</rdf:li></rdf:Seq></dc:creator>'
            '<dc:subject><rdf:Bag><rdf:li>alpha</rdf:li><rdf:li>beta</rdf:li></rdf:Bag></dc:subject>'
            '<pdf:Keywords>alpha; beta; PTE-FYP-OWNED-SOURCE</pdf:Keywords>'
            '<pdf:Producer>Original Producer Engine</pdf:Producer>'
            '<xmp:CreatorTool>Original Creator App</xmp:CreatorTool>'
            '<xmp:CreateDate>2020-01-02T03:04:05Z</xmp:CreateDate>'
            '<xmp:ModifyDate>2020-01-02T03:04:05Z</xmp:ModifyDate>'
            '<xmpMM:DocumentID>uuid:source-doc-id</xmpMM:DocumentID>'
            '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
        )
        d.save(src)
        d.close()

        doc = pymupdf.open(src)
        session = CleanEditSession(str(src))
        engine = PdfEngine()
        cand = FontCandidate(
            path=None,
            family="Times-Roman",
            full_name="Times-Roman",
            weight=400,
            source="Base14",
            base14_name="tiro",
        )

        edits = [
            (0, "Original Customer", "Replacement Customer"),
            (0, "Period: January 1 to January 31", "Period: February 1 to February 28"),
            (0, "Balance: $100.00", "Balance: $250.00"),
            (1, "Original Customer | January 1 to January 31", "Replacement Customer | February 1 to February 28"),
        ]

        for page_index, old, new in edits:
            page = doc[page_index]
            span = first_span(page, old)
            original_bbox = span.bbox
            engine.replace_text(page, span, new, cand)
            final_span = next((s for s in engine.extract_spans(page) if s.text == new), None)
            final_bbox = final_span.bbox if final_span else original_bbox
            session.record_edit(
                page_number=page_index + 1,
                original_text=old,
                replacement_text=new,
                original_bbox=original_bbox,
                final_bbox=final_bbox,
                origin=span.origin,
                font_family=cand.family,
                font_face=cand.display_name,
                font_source=cand.source,
                font_size=span.size,
                color=span.color,
                mode="smoke-clean-redaction",
            )

        output, audit, report = session.export_clean(doc, str(out))
        doc.close()

        check = pymupdf.open(output)
        text = "\n".join(page.get_text("text") for page in check)
        meta = check.metadata

        for old in [e[1] for e in edits]:
            assert old not in text, f"old text retained: {old}"
        for new in [e[2] for e in edits]:
            assert new in text, f"replacement missing: {new}"
        assert text.count("Replacement Customer | February 1 to February 28") == 1

        # V3.17 inherited behavior: no visible disclosure text is injected into pages.
        assert "EDITED COPY" not in text

        # V3.17 inherited behavior: no empty /Annots [] (or /Annots null) left by redactions.
        for i in range(len(check)):
            pobj = page_object(check, i)
            assert not re.search(r"/Annots\s*\[\s*\]", pobj), pobj
            assert not re.search(r"/Annots\s+null\b", pobj), pobj

        # V3.17 inherited behavior: no disclosure-only Helvetica font is introduced. The synthetic
        # source/replacement uses Times-Roman throughout.
        font_names = []
        for page in check:
            for item in page.get_fonts(full=True):
                # Base font name is field 3 in PyMuPDF's get_fonts tuple.
                if len(item) > 3:
                    font_names.append(str(item[3]))
        assert not any("Helvetica" in name for name in font_names), font_names

        # No visible-disclosure transparency state should be present on either
        # page. The synthetic source itself has no ExtGState resources.
        for i in range(len(check)):
            pobj = page_object(check, i)
            assert "/ExtGState" not in pobj

        check.close()

        # V3.19 ordinary-document policy: Creator / Producer pass through unchanged.
        assert meta.get("title") == "Quarterly Account Summary"
        assert meta.get("author") == "Example Author"
        assert meta.get("subject") == "Original Subject"
        assert meta.get("keywords") == "alpha; beta; PTE-FYP-OWNED-SOURCE"
        assert meta.get("creationDate") == "D:20200102030405+00'00'"
        assert meta.get("creator") == "Original Creator App"
        assert meta.get("producer") == "Original Producer Engine"
        

        xmp_out = pymupdf.open(output)
        try:
            xmp_text = xmp_out.get_xml_metadata() or ""
        finally:
            xmp_out.close()
        assert "XMP Account Summary" in xmp_text
        assert "Example Author" in xmp_text
        assert "alpha; beta; PTE-FYP-OWNED-SOURCE" in xmp_text
        assert "2020-01-02T03:04:05Z" in xmp_text
        assert "Original Producer Engine" in xmp_text
        assert "Original Creator App" in xmp_text
        assert "uuid:source-doc-id" not in xmp_text

        assert Path(audit).exists()
        assert Path(report).exists()

        audit_data = json.loads(Path(audit).read_text(encoding="utf-8"))
        assert audit_data["export_cleanup"]["visible_page_disclosure_added"] is False
        assert audit_data["export_cleanup"]["empty_annots_entries_removed"] >= 1

        assert audit_data["metadata_policy"]["source_metadata_passthrough"] is True

        # A second ordinary PDF also passes through dynamically; no marker is required.
        neutral_src = td / "unmarked.pdf"
        neutral_out = td / "unmarked-edited.pdf"
        nd = pymupdf.open()
        np = nd.new_page(width=300, height=300)
        np.insert_text((40, 60), "Neutral demo", fontsize=11)
        nd.set_metadata({
            "creator": "External Creator",
            "producer": "External Producer",
            "keywords": "no ownership marker",
        })
        nd.save(neutral_src)
        nd.close()
        ndoc = pymupdf.open(neutral_src)
        nsession = CleanEditSession(str(neutral_src))
        nsession.export_clean(ndoc, str(neutral_out))
        ndoc.close()
        ncheck = pymupdf.open(neutral_out)
        try:
            nmeta = ncheck.metadata
        finally:
            ncheck.close()
        assert nmeta.get("creator") == "External Creator"
        assert nmeta.get("producer") == "External Producer"

        print("V3.19 metadata passthrough smoke test: PASS")
        print(output)


if __name__ == "__main__":
    main()
