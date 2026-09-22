from pathlib import Path
import tempfile

import pymupdf

from app.clean_edit_session import CleanEditSession


PROFILES = [
    {
        "name": "word",
        "creator": "Microsoft Word",
        "producer": "Microsoft PDF Engine",
        "author": "Student A",
    },
    {
        "name": "libreoffice",
        "creator": "LibreOffice Writer",
        "producer": "LibreOffice 24.2.0.3",
        "author": "Student B",
    },
    {
        "name": "acrobat",
        "creator": "Adobe Acrobat Pro",
        "producer": "Adobe PDF Library 17.0",
        "author": "Student C",
    },
    {
        "name": "preview",
        "creator": "Preview",
        "producer": "macOS Quartz PDFContext",
        "author": "Student D",
    },
]


def make_pdf(path: Path, profile: dict):
    d = pymupdf.open()
    p = d.new_page(width=612, height=792)
    p.insert_text((72, 100), f"Metadata test - {profile['name']}", fontsize=12)
    d.set_metadata({
        "title": f"Test {profile['name']}",
        "author": profile["author"],
        "subject": "FYP multi-document metadata test",
        "keywords": "fyp; showcase; metadata",
        "creator": profile["creator"],
        "producer": profile["producer"],
        "creationDate": "D:20260102030405+00'00'",
      
    })
    d.set_xml_metadata(
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" '
        'xmlns:pdf="http://ns.adobe.com/pdf/1.3/" '
        'xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
        f'<pdf:Producer>{profile["producer"]}</pdf:Producer>'
        f'<xmp:CreatorTool>{profile["creator"]}</xmp:CreatorTool>'
        '<xmp:CreateDate>2026-01-02T03:04:05Z</xmp:CreateDate>'
        '<xmp:ModifyDate>2026-01-02T03:04:05Z</xmp:ModifyDate>'
        '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
    )
    d.save(path)
    d.close()


def read_meta(path: Path):
    d = pymupdf.open(path)
    try:
        return dict(d.metadata or {}), (d.get_xml_metadata() or "")
    finally:
        d.close()


def export_copy(src: Path, out: Path):
    d = pymupdf.open(src)
    try:
        session = CleanEditSession(str(src))
        session.export_clean(d, str(out))
        return session
    finally:
        d.close()


def main():
    with tempfile.TemporaryDirectory(prefix="ptse_v319_multi_") as td:
        td = Path(td)
        rows = []
        for profile in PROFILES:
            src = td / f"{profile['name']}_source.pdf"
            out = td / f"{profile['name']}_edited.pdf"
            make_pdf(src, profile)
            session = export_copy(src, out)
            before, before_xmp = read_meta(src)
            after, after_xmp = read_meta(out)

            assert session.source_metadata_passthrough is True, profile
            assert before.get("creator") == profile["creator"]
            assert before.get("producer") == profile["producer"]
            assert after.get("creator") == before.get("creator"), (profile, before, after)
            assert after.get("producer") == before.get("producer"), (profile, before, after)
            assert profile["creator"] in after_xmp
            assert profile["producer"] in after_xmp
           
            rows.append((profile["name"], after.get("creator"), after.get("producer")))

        # Exact-metadata export does not special-case a producer name: the raw
        # Info/XMP objects are preserved for every input and verified post-save.
        protected = {
            "name": "protected",
            "creator": "Bank of America",
            "producer": "TargetStream StreamEDS rv1.7.161 for Bank of America",
            "author": "Example",
        }
        src = td / "protected_source.pdf"
        out = td / "protected_edited.pdf"
        make_pdf(src, protected)
        session = export_copy(src, out)
        after, _ = read_meta(out)
        assert session.source_metadata_passthrough is True
        assert after.get("creator") == protected["creator"]
        assert after.get("producer") == protected["producer"]

        print("V3.19 multi-document metadata test: PASS")
        for name, creator, producer in rows:
            print(f"{name}: Creator={creator!r} | Producer={producer!r}")
        print("exact metadata preservation for additional profile: PASS")


if __name__ == "__main__":
    main()
