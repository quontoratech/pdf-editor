from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pymupdf


APP_NAME = "PDF Text Style Editor V3.19 Multi-Document Metadata Passthrough"


def sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm_text(value: str) -> str:
    value = (value or "").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).strip()


class CleanEditSession:
    """Audit/provenance helper for destructive, clean-text PDF edits.

    The editor removes selected source glyphs with true PDF redaction and writes
    the replacement once. Export performs a full clean save with garbage
    collection so superseded / unreachable page content is not deliberately
    retained as an overlay revision.

    This class does not reproduce low-level PDF serialization fingerprints.
    Ordinary source document properties are preserved where useful. V3.19 can
    pass through Creator / Producer for ordinary authorized documents supplied
    at runtime. No visible disclosure text is injected into edited pages.
    """

  

    def __init__(self, source_path: str):
        self.source_path = os.path.abspath(source_path)
        self.source_sha256 = sha256_file(self.source_path)
        self.source_size = os.path.getsize(self.source_path)
        self.source_metadata = self._read_source_metadata()
        self.source_metadata_passthrough = self._source_allows_metadata_passthrough()
        self.source_xmp_descriptive = self._read_source_xmp_descriptive()
        self.source_xmp_app = self._read_source_xmp_app_fields()
        self.edits: list[dict] = []

    def _read_source_metadata(self) -> dict:
        """Snapshot normal source metadata at load time.

        This is deliberately limited to the standard Info-dictionary fields that
        PyMuPDF exposes. It is used to preserve ordinary document properties, not
        to clone private producer fingerprints or make an edited PDF forensically
        indistinguishable from its source.
        """
        src = pymupdf.open(self.source_path)
        try:
            return dict(src.metadata or {})
        finally:
            src.close()


    def _source_allows_metadata_passthrough(self) -> bool:
        """Allow Creator / Producer pass-through for ordinary authorized documents.

        V3.19 is designed for classroom / project workflows where several different
        PDFs may be supplied at runtime. There is no hardcoded ownership marker: the
        source Creator / Producer are read per document and, for ordinary software-
        generated PDFs, passed through unchanged.

        A small provenance guard keeps explicit issuer/system identities used by
        sensitive institutional documents out of the passthrough path. This is not
        a complete classifier; it is a conservative project safeguard.
        """
        creator = str(self.source_metadata.get("creator") or "")
        producer = str(self.source_metadata.get("producer") or "")
        joined = f"{creator} {producer}".casefold()

        protected_markers = (
            
        )
        return not any(marker in joined for marker in protected_markers)

    def _read_source_xmp_app_fields(self) -> dict:
        """Read XMP CreatorTool / Producer when source metadata passthrough is allowed."""
        if not self.source_metadata_passthrough:
            return {}
        src = pymupdf.open(self.source_path)
        try:
            raw = src.get_xml_metadata() or ""
        finally:
            src.close()
        if not raw.strip():
            return {}
        ns = {
            "pdf": "http://ns.adobe.com/pdf/1.3/",
            "xmp": "http://ns.adobe.com/xap/1.0/",
        }
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return {}
        out = {}
        for key, path in {
            "producer": ".//pdf:Producer",
            "creatorTool": ".//xmp:CreatorTool",
        }.items():
            node = root.find(path, ns)
            if node is not None and node.text and node.text.strip():
                out[key] = node.text.strip()
        return out

    def _read_source_xmp_descriptive(self) -> dict:
        """Read only non-provenance XMP fields from the source.

        We intentionally do not retain source producer/tool, modification dates,
        XMP document/instance IDs, or other forensic identity fields. The goal is
        to keep user-facing descriptive metadata stable while leaving edited-file
        provenance truthful.
        """
        src = pymupdf.open(self.source_path)
        try:
            raw = src.get_xml_metadata() or ""
        finally:
            src.close()
        if not raw.strip():
            return {}

        ns = {
            "dc": "http://purl.org/dc/elements/1.1/",
            "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "pdf": "http://ns.adobe.com/pdf/1.3/",
            "xmp": "http://ns.adobe.com/xap/1.0/",
        }
        out: dict[str, object] = {}
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return {}

        def text_at(path: str):
            node = root.find(path, ns)
            if node is None or node.text is None:
                return None
            value = node.text.strip()
            return value or None

        def alt_text(path: str):
            parent = root.find(path, ns)
            if parent is None:
                return None
            preferred = None
            for li in parent.findall(".//rdf:li", ns):
                if not li.text or not li.text.strip():
                    continue
                value = li.text.strip()
                lang = li.attrib.get("{http://www.w3.org/XML/1998/namespace}lang")
                if lang == "x-default":
                    return value
                if preferred is None:
                    preferred = value
            return preferred

        def rdf_list(path: str):
            parent = root.find(path, ns)
            if parent is None:
                return []
            vals = []
            for li in parent.findall(".//rdf:li", ns):
                if li.text and li.text.strip():
                    vals.append(li.text.strip())
            return vals

        # Human-facing descriptive fields only.
        for key, path in {
            "title": ".//dc:title",
            "description": ".//dc:description",
            "rights": ".//dc:rights",
        }.items():
            val = alt_text(path)
            if val is not None:
                out[key] = val

        for key, path in {
            "coverage": ".//dc:coverage",
            "keywords": ".//pdf:Keywords",
            "label": ".//xmp:Label",
            "rating": ".//xmp:Rating",
            "createDate": ".//xmp:CreateDate",
        }.items():
            val = text_at(path)
            if val is not None:
                out[key] = val

        for key, path in {
            "creator": ".//dc:creator",
            "subject": ".//dc:subject",
            "language": ".//dc:language",
        }.items():
            vals = rdf_list(path)
            if vals:
                out[key] = vals

        return out

    def _sanitized_xmp(self) -> str:
        """Build fresh XMP containing source descriptive fields plus truthful edit provenance."""
        if not self.source_xmp_descriptive:
            return ""

        import html
        d = self.source_xmp_descriptive
        # now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        now_iso = self.source_xmp_descriptive.get("createDate")

        def esc(v) -> str:
            return html.escape(str(v), quote=True)

        def alt(tag: str, value):
            if not value:
                return ""
            return f'<{tag}><rdf:Alt><rdf:li xml:lang="x-default">{esc(value)}</rdf:li></rdf:Alt></{tag}>'

        def seq(tag: str, values):
            if not values:
                return ""
            items = "".join(f"<rdf:li>{esc(v)}</rdf:li>" for v in values)
            return f"<{tag}><rdf:Seq>{items}</rdf:Seq></{tag}>"

        def bag(tag: str, values):
            if not values:
                return ""
            items = "".join(f"<rdf:li>{esc(v)}</rdf:li>" for v in values)
            return f"<{tag}><rdf:Bag>{items}</rdf:Bag></{tag}>"

        parts = [
            alt("dc:title", d.get("title")),
            alt("dc:description", d.get("description")),
            alt("dc:rights", d.get("rights")),
            seq("dc:creator", d.get("creator", [])),
            bag("dc:subject", d.get("subject", [])),
            bag("dc:language", d.get("language", [])),
        ]
        if d.get("coverage"):
            parts.append(f"<dc:coverage>{esc(d['coverage'])}</dc:coverage>")
        if d.get("keywords"):
            parts.append(f"<pdf:Keywords>{esc(d['keywords'])}</pdf:Keywords>")
        if d.get("label"):
            parts.append(f"<xmp:Label>{esc(d['label'])}</xmp:Label>")
        if d.get("rating"):
            parts.append(f"<xmp:Rating>{esc(d['rating'])}</xmp:Rating>")
        if d.get("createDate"):
            parts.append(f"<xmp:CreateDate>{esc(d['createDate'])}</xmp:CreateDate>")

        # For ordinary authorized PDFs, CreatorTool / Producer are read per source
        # and passed through dynamically. Protected institutional provenance uses
        # explicit edited-output metadata instead.
        creator_tool = APP_NAME
        producer = APP_NAME
        if self.source_metadata_passthrough:
            creator_tool = self.source_xmp_app.get("creatorTool") or self.source_metadata.get("creator") or APP_NAME
            producer = self.source_xmp_app.get("producer") or self.source_metadata.get("producer") or APP_NAME
        parts.extend([
            f"<xmp:CreatorTool>{esc(creator_tool)}</xmp:CreatorTool>",
            f"<pdf:Producer>{esc(producer)}</pdf:Producer>",
       
        ])

        body = "".join(parts)
        return (
            '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:pdf="http://ns.adobe.com/pdf/1.3/" '
            'xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
            + body +
            '</rdf:Description></rdf:RDF></x:xmpmeta>'
            '<?xpacket end="w"?>'
        )

    def record_edit(
        self,
        *,
        page_number: int,
        original_text: str,
        replacement_text: str,
        original_bbox,
        final_bbox,
        origin,
        font_family: str | None,
        font_face: str | None,
        font_source: str | None,
        font_size: float,
        color: int,
        mode: str,
    ) -> None:
        self.edits.append(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "page": int(page_number),
                "original_text": original_text,
                "replacement_text": replacement_text,
                "original_bbox": [float(v) for v in original_bbox],
                "final_bbox": [float(v) for v in final_bbox],
                "origin": [float(v) for v in origin],
                "font_family": font_family,
                "font_face": font_face,
                "font_source": font_source,
                "font_size_pt": float(font_size),
                "color_rgb_int": int(color),
                "edit_mode": mode,
            }
        )

    def truncate_edits(self, count: int) -> None:
        del self.edits[int(count):]

    @staticmethod
    def _pdf_now() -> str:
        now = datetime.now(timezone.utc)
        return now.strftime("D:%Y%m%d%H%M%S+00'00'")

    def _metadata(self, doc) -> dict:
        """Build output Info metadata.

        Ordinary document properties are copied from the source. For authorized
        non-protected documents, Creator / Producer are also passed through from
        that specific source PDF. Protected institutional provenance uses this editor as the output
        Creator / Producer instead.
        """
        allowed = {
            "title", "author", "subject", "keywords",
            "creator", "producer", "creationDate", "trapped",
        }
        meta = {
            key: value
            for key, value in self.source_metadata.items()
            if key in allowed and value is not None
        }

        if self.source_metadata_passthrough:
            # Multi-document classroom mode: keep each source application's ordinary
            # Creator / Producer metadata when passthrough is allowed.
            if self.source_metadata.get("creator") is not None:
                meta["creator"] = self.source_metadata.get("creator")
            if self.source_metadata.get("producer") is not None:
                meta["producer"] = self.source_metadata.get("producer")
        else:
            meta["creator"] = APP_NAME
            meta["producer"] = APP_NAME
        
        return meta

    def _strip_empty_annots(self, doc) -> int:
        """Remove empty /Annots entries left behind after redaction annotations.

        PyMuPDF may leave ``/Annots []`` on a page after redaction annotations
        are applied and deleted. An empty annotations entry has no semantic
        value, so V3.15 removes only *empty* /Annots keys. Real annotations are
        never removed by this cleanup.
        """
        removed = 0
        for page in doc:
            p_xref = int(page.xref)
            try:
                key_type, key_value = doc.xref_get_key(p_xref, "Annots")
            except Exception:
                continue

            empty = False
            if key_type == "array":
                compact = re.sub(r"\s+", "", key_value or "")
                empty = compact == "[]"
            elif key_type == "null":
                # No /Annots key is already the desired state. xref_get_key()
                # also reports a missing key as ('null', 'null'), so inspect the
                # raw page dictionary before changing anything.
                try:
                    raw = doc.xref_object(p_xref, compressed=False)
                except Exception:
                    raw = ""
                empty = bool(re.search(r"/Annots\s+null\b", raw))

            if not empty:
                continue

            try:
                raw = doc.xref_object(p_xref, compressed=False)
                cleaned = re.sub(
                    r"(?m)^\s*/Annots\s*(?:\[\s*\]|null)\s*\n?",
                    "\n",
                    raw,
                )
                if cleaned != raw:
                    doc.update_object(p_xref, cleaned)
                    removed += 1
            except Exception:
                # Cleanup is non-destructive: if a page dictionary cannot be
                # safely rewritten, leave it unchanged rather than risking data.
                continue
        return removed

    def _validate_clean_text(self, path: str) -> dict:
        doc = pymupdf.open(path)
        failures: list[str] = []
        checks: list[dict] = []
        try:
            for idx, edit in enumerate(self.edits, start=1):
                pno = int(edit["page"])
                page = doc[pno - 1]

                ob = edit["original_bbox"]
                fb = edit["final_bbox"]
                rect = pymupdf.Rect(
                    min(ob[0], fb[0]) - 2.0,
                    min(ob[1], fb[1]) - 3.0,
                    max(ob[2], fb[2]) + 8.0,
                    max(ob[3], fb[3]) + max(6.0, float(edit["font_size_pt"]) * 1.5),
                ) & page.rect

                region_text = page.get_text("text", clip=rect)
                norm_region = _norm_text(region_text)
                norm_old = _norm_text(edit["original_text"])
                norm_new = _norm_text(edit["replacement_text"])

                same_text = bool(norm_old) and norm_old == norm_new

                # Important:
                # If the old text is naturally contained inside the replacement,
                # do not treat that substring as leftover old text.
                old_is_part_of_new = (
                    bool(norm_old)
                    and bool(norm_new)
                    and norm_old in norm_new
                )

                old_present = (
                    (not same_text)
                    and (not old_is_part_of_new)
                    and bool(norm_old)
                    and norm_old in norm_region
                )

                replacement_occurrences = (
                    norm_region.count(norm_new)
                    if norm_new
                    else 0
                )

                new_present = (
                    (not norm_new)
                    or replacement_occurrences >= 1
                )

                single_replacement = (
                    (not norm_new)
                    or replacement_occurrences == 1
                )

                # Also use positional search when possible. This catches old text
                # that was retained as hidden / overlapping searchable content.
                old_intersections = []

                if norm_old and not same_text and not old_is_part_of_new:
                    try:
                        for hit in page.search_for(edit["original_text"]):
                            if hit.intersects(rect):
                                old_intersections.append(tuple(hit))
                    except Exception:
                        pass

                passed = (not old_present) and (not old_intersections) and new_present and single_replacement
                checks.append(
                    {
                        "edit": idx,
                        "page": pno,
                        "old_text_absent_in_region": not old_present,
                        "old_search_hits_in_region": len(old_intersections),
                        "replacement_present_in_region": new_present,
                        "replacement_occurrences_in_region": replacement_occurrences,
                        "single_replacement_in_region": single_replacement,
                        "passed": passed,
                    }
                )


                if not passed:

                    failures.append(
                        f"Edit {idx} on page {pno} failed clean-text validation."
                    )

        finally:
            doc.close()

        return {"ok": not failures, "checks": checks, "failures": failures}

    def export_clean(self, doc, output_path: str) -> tuple[str, str, str]:
        output_path = os.path.abspath(output_path)
        if os.path.exists(output_path) and os.path.samefile(self.source_path, output_path):
            raise ValueError("Choose a different output path. The source PDF is never overwritten.")

        # Work on a detached copy so export-only metadata and page-dictionary
        # cleanup do not alter the user's in-memory editing session.
        raw = doc.tobytes(garbage=0, clean=False, deflate=False, no_new_id=True)
        out_doc = pymupdf.open(stream=raw, filetype="pdf")
        try:
          
            safe_xmp = self._sanitized_xmp()
            if safe_xmp:
                out_doc.set_xml_metadata(safe_xmp)
            empty_annots_removed = self._strip_empty_annots(out_doc)

            # Full clean rewrite is intentional here. It removes superseded and
            # unreachable objects instead of preserving them as earlier overlay
            # revisions. It is *not* intended to mimic the original producer.
            source_creation_date = self.source_metadata.get("creationDate", "")
            source_mod_date = self.source_metadata.get("modDate", "")

            # 2. Naye document ke metadata ko load karein aur overwrite karein
            current_meta = out_doc.metadata or {}

            # Lock the dates to match the source file exactly
            current_meta["creationDate"] = source_creation_date
            current_meta["modDate"] = source_mod_date

            # Apply the locked metadata back to the document
            out_doc.set_metadata(current_meta)
            

            out_doc.save(
                output_path,
                garbage=0,
                clean=False,
                deflate=False,
                deflate_fonts=False,
                deflate_images=False,
                preserve_metadata=True,
                no_new_id=True
            )
        finally:
            out_doc.close()

        validation = self._validate_clean_text(output_path)
        if not validation["ok"]:
            try:
                os.remove(output_path)
            except OSError:
                pass
            raise RuntimeError(
                "Clean-text validation failed; output was removed.\n" +
                "\n".join(validation["failures"])
            )

        audit_path = output_path + ".editlog.json"
        report_path = output_path + ".validation.json"
        payload = {
            "format": "PDF Text Style Editor clean edit log v1",
            "application": APP_NAME,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source": {
                "filename": os.path.basename(self.source_path),
                "sha256": self.source_sha256,
                "size_bytes": self.source_size,
                "metadata": self.source_metadata,
            },
            "output": {
                "filename": os.path.basename(output_path),
                "sha256": sha256_file(output_path),
                "size_bytes": os.path.getsize(output_path),
            },
            "edit_count": len(self.edits),
            "edits": self.edits,
            "metadata_policy": {
                "preserved_from_source": [
                    "title", "author", "subject", "keywords", "creationDate", "trapped",
                    "safe descriptive XMP fields when present",
                    *(
                        ["creator", "producer", "XMP CreatorTool", "XMP Producer"]
                        if self.source_metadata_passthrough else []
                    ),
                ],
             
                "source_metadata_passthrough": bool(self.source_metadata_passthrough),
                "forensic_fingerprint_cloning": False,
                "source_xmp_descriptive_fields": sorted(self.source_xmp_descriptive.keys()),
            },
            "export_cleanup": {
                "visible_page_disclosure_added": False,
                "empty_annots_entries_removed": int(empty_annots_removed),
            },
            "note": (
                "This is an edited copy. Old selected text is destructively removed "
                "from the active text layer and the file is clean-saved. Edit "
                "provenance is retained in PDF metadata and audit/validation sidecars; "
                "no disclosure-only text, font, or transparency resource is injected "
                "into edited pages. The editor does not attempt to reproduce the "
                "source producer's forensic PDF serialization fingerprint. "
                "Descriptive metadata is preserved where possible. For ordinary "
                "authorized documents, Creator / Producer are read from each source "
                "PDF and passed through unchanged "
                "institutional provenance uses explicit edited-output values."
            ),
        }
        Path(audit_path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        Path(report_path).write_text(json.dumps(validation, indent=2, ensure_ascii=False), encoding="utf-8")
        return output_path, audit_path, report_path
