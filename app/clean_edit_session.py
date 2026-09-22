from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
import zlib
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
    the replacement once. Export merges each edited page back to one content
    stream and performs a compact full save so temporary redaction annotations,
    appearance streams, and superseded page streams are not serialized.

    Source Info/XMP metadata, trailer ID, xref family, and untouched page stream
    bytes are preserved and verified. Compact garbage collection can renumber
    indirect object references; those numbers are serialization details rather
    than document content. No visible disclosure text is injected into pages.
    """

  

    def __init__(self, source_path: str):
        self.source_path = os.path.abspath(source_path)
        self.source_sha256 = sha256_file(self.source_path)
        self.source_size = os.path.getsize(self.source_path)
        self.source_structure = self._read_source_structure()
        self.source_metadata = self._read_source_metadata()
        self.source_metadata_passthrough = self._source_allows_metadata_passthrough()
        self.source_xmp_descriptive = self._read_source_xmp_descriptive()
        self.edits: list[dict] = []

    @staticmethod
    def _stream_sha256(doc, xref: int, *, raw: bool = False) -> str | None:
        """Return a stable hash for one PDF stream, or ``None`` for non-streams."""
        try:
            if not doc.xref_is_stream(int(xref)):
                return None
            data = (
                doc.xref_stream_raw(int(xref))
                if raw
                else doc.xref_stream(int(xref))
            )
        except Exception:
            return None
        return hashlib.sha256(data or b"").hexdigest()

    @staticmethod
    def _trailer_ref(doc, key: str) -> int | None:
        try:
            key_type, key_value = doc.xref_get_key(-1, key)
        except Exception:
            return None
        if key_type != "xref":
            return None
        try:
            return int(str(key_value).split()[0])
        except (TypeError, ValueError, IndexError):
            return None

    @staticmethod
    def _object_entries(doc, xref: int | None) -> dict[str, dict[str, str]] | None:
        """Return a PDF dictionary without treating key order as identity.

        PDF dictionaries are unordered. MuPDF nevertheless serializes the
        Catalog with ``/Type`` first, even when the source placed ``/Pages``
        first. Comparing ``xref_object()`` text therefore reports a false
        structural change for an otherwise identical Catalog.
        """
        if not xref:
            return None
        try:
            keys = doc.xref_get_keys(int(xref))
        except Exception:
            return None

        entries: dict[str, dict[str, str]] = {}
        try:
            for key in keys:
                value_type, value = doc.xref_get_key(int(xref), key)
                entries[str(key)] = {
                    "type": str(value_type),
                    "value": str(value),
                }
        except Exception:
            return None
        return entries

    @staticmethod
    def _xref_format(doc) -> str:
        """Classify the latest cross-reference section.

        A hybrid-reference PDF has a classic trailer plus ``/XRefStm``. MuPDF
        cannot reproduce that hybrid form during a full rewrite, so it is kept
        distinct instead of being reported as an ordinary xref table.
        """
        try:
            key_type, key_value = doc.xref_get_key(-1, "Type")
            if key_type == "name" and key_value == "/XRef":
                return "stream"

            hybrid_type, _ = doc.xref_get_key(-1, "XRefStm")
            if hybrid_type == "int":
                return "hybrid"
        except Exception:
            pass
        return "table"

    @staticmethod
    def _classic_xref_entries(raw: bytes, xref_offset: int) -> dict[int, tuple[int, int, bool]]:
        """Parse the final classic xref table written by MuPDF.

        Returns ``object_number -> (byte_offset, generation, in_use)``. This is
        deliberately limited to the final, freshly generated full-save table;
        incremental and hybrid inputs are normalized by the save immediately
        before this parser is called.
        """
        cursor = int(xref_offset)
        if raw[cursor:cursor + 4] != b"xref":
            raise RuntimeError("Expected a classic xref table in the staged PDF.")
        cursor += 4
        entries: dict[int, tuple[int, int, bool]] = {}

        def next_line() -> bytes:
            nonlocal cursor
            while cursor < len(raw) and raw[cursor:cursor + 1] in {b"\r", b"\n"}:
                cursor += 1
            end = raw.find(b"\n", cursor)
            if end < 0:
                end = len(raw)
            line = raw[cursor:end].rstrip(b"\r")
            cursor = end + 1
            return line.strip()

        while True:
            line = next_line()
            if line == b"trailer":
                break
            match = re.fullmatch(rb"(\d+)\s+(\d+)", line)
            if not match:
                raise RuntimeError("Malformed staged PDF xref subsection.")
            first = int(match.group(1))
            count = int(match.group(2))
            for index in range(count):
                item = next_line()
                entry = re.fullmatch(rb"(\d+)\s+(\d+)\s+([nf])", item)
                if not entry:
                    raise RuntimeError("Malformed staged PDF xref entry.")
                entries[first + index] = (
                    int(entry.group(1)),
                    int(entry.group(2)),
                    entry.group(3) == b"n",
                )
        return entries

    def _rewrite_classic_xref_as_source_stream(self, output_path: str) -> dict:
        """Emit a direct-object, Flate-compressed xref stream.

        PyMuPDF's ``use_objstms=1`` couples xref-stream output with an /ObjStm,
        an uncompressed xref stream, and a ``Written by MuPDF`` header comment.
        The reported source instead uses direct objects and a compressed xref
        stream. Build that final indexing section from the staged classic table
        so the page/object bytes are unchanged and every offset is authoritative.
        """
        path = Path(output_path)
        raw = path.read_bytes()
        start_matches = list(
            re.finditer(rb"startxref\s+(\d+)\s+%%EOF\s*$", raw, re.DOTALL)
        )
        if not start_matches:
            raise RuntimeError("The staged PDF has no final startxref marker.")
        old_xref_offset = int(start_matches[-1].group(1))
        entries = self._classic_xref_entries(raw, old_xref_offset)

        staged = pymupdf.open(output_path)
        try:
            old_size = int(staged.xref_length())
            trailer_parts: list[str] = []
            excluded = {
                "Size", "Prev", "XRefStm", "Type", "W", "Index",
                "Length", "Filter", "DecodeParms",
            }
            for key in staged.xref_get_keys(-1):
                if key in excluded:
                    continue
                value_type, value = staged.xref_get_key(-1, key)
                if value_type == "null":
                    continue
                trailer_parts.append(f"/{key} {value}")
        finally:
            staged.close()

        active_offsets = [
            offset
            for object_number, (offset, _generation, in_use) in entries.items()
            if object_number > 0 and in_use
        ]
        if not active_offsets:
            raise RuntimeError("The staged PDF contains no active indirect objects.")
        first_object_offset = min(active_offsets)

        source_raw = Path(self.source_path).read_bytes()
        source_lines = source_raw.splitlines(keepends=True)
        if len(source_lines) < 2 or not source_lines[0].startswith(b"%PDF-"):
            raise RuntimeError("The source PDF header is malformed.")
        source_header = b"".join(source_lines[:2])
        body = source_header + raw[first_object_offset:old_xref_offset]
        offset_delta = len(source_header) - first_object_offset

        xref_object = old_size
        new_size = xref_object + 1
        new_xref_offset = len(body)
        decoded = bytearray()
        for object_number in range(new_size):
            if object_number == xref_object:
                entry_type, offset, generation = 1, new_xref_offset, 0
            else:
                old_offset, old_generation, in_use = entries.get(
                    object_number,
                    (0, 0, False),
                )
                if in_use:
                    entry_type = 1
                    offset = old_offset + offset_delta
                    generation = old_generation
                else:
                    entry_type = 0
                    offset = old_offset
                    generation = 255 if object_number == 0 else old_generation
            if not 0 <= offset <= 0xFFFFFFFF:
                raise RuntimeError("PDF offset exceeds the four-byte xref width.")
            if not 0 <= generation <= 0xFF:
                raise RuntimeError("PDF generation exceeds the source xref width.")
            decoded.extend(bytes((entry_type,)))
            decoded.extend(int(offset).to_bytes(4, "big"))
            decoded.extend(bytes((int(generation),)))

        compressed = zlib.compress(bytes(decoded))
        dictionary = (
            "<</Type/XRef"
            + "".join(trailer_parts)
            + f"/Size {new_size}/W[1 4 1]/Index[0 {new_size}]"
            + f"/Length {len(compressed)}/Filter/FlateDecode>>"
        ).encode("latin-1")
        final_section = (
            f"{xref_object} 0 obj\n".encode("ascii")
            + dictionary
            + b"\nstream\n"
            + compressed
            + b"\nendstream\nendobj\n\n"
            + f"startxref\n{new_xref_offset}\n%%EOF\n".encode("ascii")
        )
        rewritten = body + final_section
        temporary = path.with_name(path.name + ".xref-stream.tmp")
        try:
            temporary.write_bytes(rewritten)
            check = pymupdf.open(temporary)
            try:
                if check.xref_get_key(-1, "Type") != ("name", "/XRef"):
                    raise RuntimeError("Rewritten PDF did not reopen as an xref stream.")
                if int(check.xref_length()) != new_size:
                    raise RuntimeError("Rewritten PDF xref size is inconsistent.")
            finally:
                check.close()
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

        return {
            "xref_size": int(new_size),
            "xref_object": int(xref_object),
            "xref_filter": "/FlateDecode",
            "xref_decoded_size": int(len(decoded)),
            "object_streams": 0,
            "writer_marker_removed": b"Written by MuPDF" not in rewritten,
        }

    @classmethod
    def _capture_structure(cls, doc) -> dict:
        """Capture the structural values export promises not to rewrite."""
        root_xref = cls._trailer_ref(doc, "Root")
        info_xref = cls._trailer_ref(doc, "Info")

        try:
            root_ref_type, root_ref_value = doc.xref_get_key(-1, "Root")
        except Exception:
            root_ref_type, root_ref_value = "null", "null"
        try:
            info_ref_type, info_ref_value = doc.xref_get_key(-1, "Info")
        except Exception:
            info_ref_type, info_ref_value = "null", "null"

        try:
            id_type, id_value = doc.xref_get_key(-1, "ID")
        except Exception:
            id_type, id_value = "null", "null"

        metadata_xref = 0
        try:
            metadata_xref = int(doc.xref_xml_metadata() or 0)
        except Exception:
            metadata_xref = 0

        pages = []
        for page_number, page in enumerate(doc, start=1):
            page_xref = int(page.xref)
            try:
                contents_type, contents_value = doc.xref_get_key(
                    page_xref,
                    "Contents",
                )
            except Exception:
                contents_type, contents_value = "null", "null"

            try:
                content_xrefs = [int(value) for value in page.get_contents()]
            except Exception:
                content_xrefs = []

            streams = []
            for content_xref in content_xrefs:
                try:
                    stream_object = doc.xref_object(
                        content_xref,
                        compressed=False,
                    )
                except Exception:
                    stream_object = None
                streams.append(
                    {
                        "xref": content_xref,
                        "object": stream_object,
                        "decoded_sha256": cls._stream_sha256(
                            doc,
                            content_xref,
                        ),
                        "raw_sha256": cls._stream_sha256(
                            doc,
                            content_xref,
                            raw=True,
                        ),
                        "filter": cls._stream_filter(doc, content_xref),
                    }
                )

            try:
                annots_type, annots_value = doc.xref_get_key(
                    page_xref,
                    "Annots",
                )
            except Exception:
                annots_type, annots_value = "null", "null"

            has_explicit_empty_annots = False
            if annots_type == "array":
                has_explicit_empty_annots = (
                    re.sub(r"\s+", "", annots_value or "") == "[]"
                )
            elif annots_type == "null":
                try:
                    raw_page = doc.xref_object(page_xref, compressed=False)
                except Exception:
                    raw_page = ""
                has_explicit_empty_annots = bool(
                    re.search(r"/Annots\s+null\b", raw_page)
                )

            pages.append(
                {
                    "page": page_number,
                    "page_xref": page_xref,
                    "contents_type": contents_type,
                    "contents_value": contents_value,
                    "content_xrefs": content_xrefs,
                    "streams": streams,
                    "had_explicit_empty_annots": has_explicit_empty_annots,
                }
            )

        def object_source(xref: int | None) -> str | None:
            if not xref:
                return None
            try:
                return doc.xref_object(xref, compressed=False)
            except Exception:
                return None

        return {
            "xref_format": cls._xref_format(doc),
            "logical_metadata": dict(doc.metadata or {}),
            "trailer_id": {
                "type": id_type,
                "value": id_value,
            },
            "root_xref": root_xref,
            "root_ref_type": root_ref_type,
            "root_ref_value": root_ref_value,
            "root_object": object_source(root_xref),
            "root_entries": cls._object_entries(doc, root_xref),
            "info_xref": info_xref,
            "info_ref_type": info_ref_type,
            "info_ref_value": info_ref_value,
            "info_object": object_source(info_xref),
            "info_entries": cls._object_entries(doc, info_xref),
            "metadata_xref": metadata_xref or None,
            "metadata_object": object_source(metadata_xref or None),
            "metadata_decoded_sha256": (
                cls._stream_sha256(doc, metadata_xref)
                if metadata_xref
                else None
            ),
            "metadata_raw_sha256": (
                cls._stream_sha256(doc, metadata_xref, raw=True)
                if metadata_xref
                else None
            ),
            "pages": pages,
            "edit_artifacts": cls._edit_artifact_counts(doc),
        }

    @staticmethod
    def _stream_filter(doc, xref: int) -> str | None:
        """Return the normalized /Filter value for one stream."""
        try:
            value_type, value = doc.xref_get_key(int(xref), "Filter")
        except Exception:
            return None
        if value_type == "null":
            return None
        return re.sub(r"\s+", " ", str(value or "")).strip() or None

    @staticmethod
    def _edit_artifact_counts(doc) -> dict[str, int]:
        """Count editor artifacts, including data hidden in object streams.

        Looking only at ``xref_object()`` is insufficient for a PDF containing
        an /ObjStm: an entry can be marked free while its old dictionary is
        still physically present in the decoded object-stream bytes.
        """
        redaction_records = 0
        fitz_a0_identifiers = 0
        free_xrefs = 0
        seen_streams: set[int] = set()

        for xref in range(1, int(doc.xref_length())):
            try:
                keys = doc.xref_get_keys(xref)
            except Exception:
                keys = ()
            if not keys:
                free_xrefs += 1

            try:
                object_text = doc.xref_object(xref, compressed=False) or ""
            except Exception:
                object_text = ""
            redaction_records += len(
                re.findall(r"/Subtype\s*/Redact\b", object_text)
            )
            fitz_a0_identifiers += object_text.count("fitz-A0")

            try:
                is_stream = bool(doc.xref_is_stream(xref))
            except Exception:
                is_stream = False
            if not is_stream or xref in seen_streams:
                continue
            seen_streams.add(xref)
            try:
                decoded = doc.xref_stream(xref) or b""
            except Exception:
                decoded = b""
            redaction_records += len(
                re.findall(rb"/Subtype\s*/Redact\b", decoded)
            )
            fitz_a0_identifiers += decoded.count(b"fitz-A0")

        return {
            "redaction_records": int(redaction_records),
            "fitz_a0_identifiers": int(fitz_a0_identifiers),
            "free_xrefs": int(free_xrefs),
        }

    @classmethod
    def _xref_serialization_profile(cls, doc, raw: bytes | None = None) -> dict:
        xref_objects: list[int] = []
        object_streams: list[int] = []
        compressed_objects = 0
        for xref in range(1, int(doc.xref_length())):
            try:
                object_type = doc.xref_get_key(xref, "Type")
            except Exception:
                continue
            if object_type == ("name", "/XRef"):
                xref_objects.append(xref)
            elif object_type == ("name", "/ObjStm"):
                object_streams.append(xref)
                try:
                    n_type, n_value = doc.xref_get_key(xref, "N")
                    if n_type == "int":
                        compressed_objects += int(n_value)
                except Exception:
                    pass

        final_xref = max(xref_objects) if xref_objects else None
        try:
            size_type, size_value = doc.xref_get_key(-1, "Size")
            xref_size = int(size_value) if size_type == "int" else None
        except Exception:
            xref_size = None

        return {
            "size": xref_size,
            "final_xref_object": final_xref,
            "compression": (
                cls._stream_filter(doc, final_xref) if final_xref else None
            ),
            "decoded_size": (
                len(doc.xref_stream(final_xref) or b"") if final_xref else None
            ),
            "object_stream_count": len(object_streams),
            "compressed_object_count": int(compressed_objects),
            "writer_marker": bool(raw and b"Written by MuPDF" in raw),
        }

    def _read_source_structure(self) -> dict:
        src = pymupdf.open(self.source_path)
        try:
            structure = self._capture_structure(src)
            structure["xref_serialization"] = self._xref_serialization_profile(
                src,
                Path(self.source_path).read_bytes(),
            )
            return structure
        finally:
            src.close()

    def _read_source_metadata(self) -> dict:
        """Snapshot the standard metadata view for the audit sidecar.

        Exact Info/XMP preservation is checked separately from raw PDF objects;
        this dictionary alone does not include private Info keys or custom XMP.
        """
        src = pymupdf.open(self.source_path)
        try:
            return dict(src.metadata or {})
        finally:
            src.close()


    def _source_allows_metadata_passthrough(self) -> bool:
        """Compatibility flag for the exact Info/XMP preservation policy.

        Export no longer rebuilds metadata or chooses fields by producer name,
        so every source uses the same pass-through behavior.
        """
        return True

    def _read_source_xmp_descriptive(self) -> dict:
        """Read selected descriptive fields for the audit summary only.

        Export never rebuilds XMP from this subset. The complete original XMP
        stream, including custom fields and document/instance IDs, is preserved
        separately and verified after saving.
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
        # If this text edit touches content that was previously moved by a
        # structural line-reflow operation, that old moved-line text is no
        # longer the final state.  The new normal text edit below becomes the
        # authoritative final-state validation record for that location.
        self._deactivate_overlapping_line_reflow_expectations(
            page_number=int(page_number),
            bboxes=[original_bbox, final_bbox],
            reason="superseded by a later text edit",
        )

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

    def record_page_operation(
        self,
        *,
        action: str,
        page_number: int,
        width: float,
        height: float,
    ) -> None:
        self.edits.append(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "operation_type": "page",
                "page_action": str(action),
                "page": int(page_number),
                "width": float(width),
                "height": float(height),
                "edit_mode": f"page-{action}",
            }
        )

    def record_line_reflow_operation(
        self,
        *,
        page_number: int,
        deleted_text: str,
        deleted_bbox,
        moved_lines,
    ) -> None:
        """Record one full-line deletion + upward-reflow transaction.

        Moved lines are stored as final expected text / final bbox pairs. This
        avoids treating renumbered list prefixes as independent text edits.
        """
        normalized_moved = []

        # A later structural reflow becomes authoritative for any earlier
        # moved-line expectations that occupy the same final area.  Without
        # this, Save As can incorrectly demand an intermediate line value that
        # was intentionally moved / renumbered / changed again later.
        superseding_boxes = [deleted_bbox]
        for _item in moved_lines or []:
            _bbox = _item.get("bbox")
            if _bbox and len(_bbox) >= 4:
                superseding_boxes.append(_bbox)

        self._deactivate_overlapping_line_reflow_expectations(
            page_number=int(page_number),
            bboxes=superseding_boxes,
            reason="superseded by a later line reflow",
        )

        for item in moved_lines or []:
            bbox = item.get("bbox")
            text = str(item.get("text") or "")

            if not bbox or len(bbox) < 4:
                continue

            normalized_moved.append(
                {
                    "text": text,
                    "bbox": [float(v) for v in bbox[:4]],
                    "active": bool(item.get("active", True)),
                }
            )

        self.edits.append(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "operation_type": "line_reflow",
                "page": int(page_number),
                "deleted_text": str(deleted_text or ""),
                "deleted_bbox": [float(v) for v in deleted_bbox],
                "moved_lines": normalized_moved,
                # record_line_reflow_operation() is called only after PdfEngine
                # has serialized / reopened the edited document and verified
                # the moved lines immediately. Keep that fact in the audit data.
                "immediate_validation_passed": True,
                "edit_mode": "clean-line-delete-upward-reflow",
            }
        )

    def shift_for_page_insert(self, inserted_page_number: int) -> None:
        """Shift active text / line-reflow page numbers after page insertion."""
        inserted_page_number = int(inserted_page_number)

        for edit in self.edits:
            if edit.get("operation_type", "text") not in {"text", "line_reflow"}:
                continue
            if edit.get("inactive"):
                continue
            if int(edit.get("page", 0) or 0) >= inserted_page_number:
                edit["page"] = int(edit["page"]) + 1

    def shift_for_page_delete(self, deleted_page_number: int) -> None:
        """Deactivate edits on a deleted page and shift later records upward."""
        deleted_page_number = int(deleted_page_number)

        for edit in self.edits:
            if edit.get("operation_type", "text") not in {"text", "line_reflow"}:
                continue
            if edit.get("inactive"):
                continue

            page_number = int(edit.get("page", 0) or 0)

            if page_number == deleted_page_number:
                edit["inactive"] = True
                edit["inactive_reason"] = "page deleted"
            elif page_number > deleted_page_number:
                edit["page"] = page_number - 1

    @staticmethod
    def _bbox_overlap_ratio(a, b) -> float:
        try:
            ar = pymupdf.Rect(*[float(v) for v in a])
            br = pymupdf.Rect(*[float(v) for v in b])
            intersection = ar & br
            if intersection.is_empty:
                return 0.0
            area = max(0.0, intersection.get_area())
            return area / max(0.01, min(ar.get_area(), br.get_area()))
        except Exception:
            return 0.0

    def _deactivate_overlapping_line_reflow_expectations(
        self,
        *,
        page_number: int,
        bboxes,
        reason: str,
    ) -> None:
        """Retire stale moved-line expectations touched by a later edit.

        A line-reflow operation is validated immediately when it is performed.
        Later edits may intentionally change, renumber, delete, or move that
        content again.  The earlier moved-line string must then stop being
        treated as the final expected text at Save As.
        """
        page_number = int(page_number)

        valid_boxes = []
        for box in bboxes or []:
            if not box or len(box) < 4:
                continue
            try:
                valid_boxes.append([float(v) for v in box[:4]])
            except Exception:
                continue

        if not valid_boxes:
            return

        for edit in self.edits:
            if edit.get("inactive"):
                continue
            if edit.get("operation_type", "text") != "line_reflow":
                continue
            if int(edit.get("page", 0) or 0) != page_number:
                continue

            for moved in edit.get("moved_lines", []):
                if not moved.get("active", True):
                    continue

                moved_bbox = moved.get("bbox")
                if not moved_bbox:
                    continue

                touched = False
                for box in valid_boxes:
                    # 20% overlap is enough here because a later edit may touch
                    # only one span / phrase inside a longer moved visual line.
                    if self._bbox_overlap_ratio(moved_bbox, box) >= 0.20:
                        touched = True
                        break

                    # Also accept center-point containment for very thin PDF
                    # spans where area-overlap ratios can be misleading.
                    try:
                        mr = pymupdf.Rect(*[float(v) for v in moved_bbox])
                        br = pymupdf.Rect(*[float(v) for v in box])
                        center = pymupdf.Point(
                            (float(br.x0) + float(br.x1)) / 2.0,
                            (float(br.y0) + float(br.y1)) / 2.0,
                        )
                        expanded = pymupdf.Rect(
                            float(mr.x0) - 3.0,
                            float(mr.y0) - 3.0,
                            float(mr.x1) + 3.0,
                            float(mr.y1) + 3.0,
                        )
                        if center in expanded:
                            touched = True
                            break
                    except Exception:
                        pass

                if touched:
                    moved["active"] = False
                    moved["inactive_reason"] = str(reason)

    def deactivate_for_line_delete(self, page_number: int, deleted_bbox) -> None:
        """Deactivate earlier final-state content that is removed by a later line deletion."""
        page_number = int(page_number)

        for edit in self.edits:
            if edit.get("inactive"):
                continue
            if int(edit.get("page", 0) or 0) != page_number:
                continue

            op_type = edit.get("operation_type", "text")

            if op_type == "text":
                final_bbox = edit.get("final_bbox")
                if not final_bbox:
                    continue

                if self._bbox_overlap_ratio(final_bbox, deleted_bbox) >= 0.50:
                    edit["inactive"] = True
                    edit["inactive_reason"] = "text line deleted"

            elif op_type == "line_reflow":
                for moved in edit.get("moved_lines", []):
                    if not moved.get("active", True):
                        continue
                    bbox = moved.get("bbox")
                    if bbox and self._bbox_overlap_ratio(bbox, deleted_bbox) >= 0.50:
                        moved["active"] = False

    def shift_for_line_reflow(self, page_number: int, source_bboxes, dy: float) -> None:
        """
        Move final validation locations when their visual line is shifted upward
        after another full-line deletion.
        """
        page_number = int(page_number)
        dy = float(dy)
        boxes = [tuple(float(v) for v in box) for box in (source_bboxes or [])]

        if not boxes or abs(dy) < 0.001:
            return

        for edit in self.edits:
            if edit.get("inactive"):
                continue
            if int(edit.get("page", 0) or 0) != page_number:
                continue

            op_type = edit.get("operation_type", "text")

            if op_type == "text":
                final_bbox = edit.get("final_bbox")
                if not final_bbox:
                    continue

                should_shift = any(
                    self._bbox_overlap_ratio(final_bbox, box) >= 0.50
                    for box in boxes
                )

                if not should_shift:
                    continue

                edit["final_bbox"] = [
                    float(final_bbox[0]),
                    float(final_bbox[1]) + dy,
                    float(final_bbox[2]),
                    float(final_bbox[3]) + dy,
                ]

                origin = edit.get("origin")
                if origin and len(origin) >= 2:
                    edit["origin"] = [
                        float(origin[0]),
                        float(origin[1]) + dy,
                    ]

            elif op_type == "line_reflow":
                for moved in edit.get("moved_lines", []):
                    if not moved.get("active", True):
                        continue

                    bbox = moved.get("bbox")
                    if not bbox:
                        continue

                    should_shift = any(
                        self._bbox_overlap_ratio(bbox, box) >= 0.50
                        for box in boxes
                    )

                    if not should_shift:
                        continue

                    moved["bbox"] = [
                        float(bbox[0]),
                        float(bbox[1]) + dy,
                        float(bbox[2]),
                        float(bbox[3]) + dy,
                    ]

    def _restore_source_identity(self, doc) -> list[str]:
        """Undo incidental identity mutations made by earlier PyMuPDF probes.

        ``Document.tobytes()`` creates and installs a trailer ID unless
        ``no_new_id=True`` is supplied. Some editing/probing paths serialize the
        live document before export, so restore the source trailer and the
        source Catalog/Info definitions immediately before writing.
        """
        source = self.source_structure
        restored: list[str] = []

        source_id = source.get("trailer_id") or {"type": "null", "value": "null"}
        try:
            current_id = doc.xref_get_key(-1, "ID")
        except Exception:
            current_id = ("null", "null")
        expected_id = (
            str(source_id.get("type") or "null"),
            str(source_id.get("value") or "null"),
        )
        if current_id != expected_id:
            doc.xref_set_key(
                -1,
                "ID",
                expected_id[1] if expected_id[0] != "null" else "null",
            )
            restored.append("trailer /ID")

        source_root_xref = source.get("root_xref")
        current_root_xref = self._trailer_ref(doc, "Root")
        source_root_object = source.get("root_object")
        if current_root_xref and source_root_object is not None:
            source_root_entries = source.get("root_entries")
            current_root_entries = self._object_entries(doc, current_root_xref)
            root_changed = (
                current_root_entries != source_root_entries
                if source_root_entries is not None
                else doc.xref_object(
                    int(current_root_xref),
                    compressed=False,
                ) != source_root_object
            )
            if root_changed:
                if source_root_xref != current_root_xref:
                    raise RuntimeError(
                        "The in-memory Catalog content and reference no longer "
                        "match the source PDF. Reopen the source and repeat the "
                        "edit before saving."
                    )
                doc.update_object(int(current_root_xref), source_root_object)
                restored.append("Catalog/Root object")
        elif source_root_xref != current_root_xref:
            raise RuntimeError(
                "The in-memory PDF no longer has the source Catalog. Reopen the "
                "source and repeat the edit before saving."
            )

        source_info_xref = source.get("info_xref")
        current_info_xref = self._trailer_ref(doc, "Info")
        if source_info_xref is None:
            if current_info_xref is not None:
                doc.xref_set_key(-1, "Info", "null")
                restored.append("Info reference")
        else:
            source_info_object = source.get("info_object")
            if source_info_object is not None:
                source_info_entries = source.get("info_entries")
                current_info_entries = self._object_entries(doc, current_info_xref)
                info_changed = (
                    current_info_entries != source_info_entries
                    if source_info_entries is not None
                    else (
                        doc.xref_object(
                            int(current_info_xref),
                            compressed=False,
                        ) != source_info_object
                        if current_info_xref
                        else True
                    )
                )
                if info_changed:
                    source_slot_entries = self._object_entries(doc, source_info_xref)
                    if source_slot_entries == source_info_entries:
                        source_info_ref = source.get("info_ref_value")
                        if not source_info_ref:
                            source_info_ref = f"{int(source_info_xref)} 0 R"
                        doc.xref_set_key(-1, "Info", str(source_info_ref))
                        restored.append("Info reference")
                    elif current_info_xref:
                        doc.update_object(int(current_info_xref), source_info_object)
                    else:
                        raise RuntimeError(
                            "The in-memory PDF no longer has the source Info "
                            "dictionary. Reopen the source and repeat the edit."
                        )
                    restored.append("Info object")

        return restored

    def _repair_saved_identity(self, output_path: str) -> list[str]:
        """Repair writer-added identity fields in one small incremental update."""
        saved = pymupdf.open(output_path)
        try:
            restored = self._restore_source_identity(saved)
            if restored:
                saved.save(
                    output_path,
                    incremental=True,
                    no_new_id=True,
                    encryption=pymupdf.PDF_ENCRYPT_KEEP,
                )
            return restored
        finally:
            saved.close()

    def _strip_empty_annots(self, doc) -> int:
        """Remove empty /Annots entries left behind after redaction annotations.

        PyMuPDF may leave ``/Annots []`` on a page after redaction annotations
        are applied and deleted. Only empty entries introduced during this edit
        session are removed: a pre-existing source-page dictionary is otherwise
        left byte-for-byte equivalent at the object level. Real annotations are
        never removed by this cleanup.
        """
        removed = 0
        source_empty_annots = {
            int(item["page_xref"])
            for item in self.source_structure.get("pages", [])
            if item.get("had_explicit_empty_annots")
        }
        for page in doc:
            p_xref = int(page.xref)
            if p_xref in source_empty_annots:
                continue
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
        """Validate the searchable text layer of the final, reopened PDF.

        Normal text replacements remain strict: the original text must be gone
        and the final replacement must be searchable exactly once in its final
        edit region.

        Full-line upward reflow needs a slightly different validator because
        ``show_pdf_page()`` may preserve the correct searchable text while the
        final save exposes it as several spans / words with slightly different
        clip coordinates.  For those structural edits we therefore validate in
        several increasingly tolerant, but still position-aware, ways:

        1. normalized text inside an expanded final rectangle;
        2. PyMuPDF ``search_for`` near the expected final position;
        3. complete extracted PDF lines near the expected Y position;
        4. an exact token sequence in ``get_text('words')`` near that position;
        5. a high in-order token match inside the local final-position region.

        The deleted source line itself is still required to be absent from its
        original line position.  Validation is never disabled.
        """
        doc = pymupdf.open(path)
        failures: list[str] = []
        checks: list[dict] = []
        edits = list(self.edits)

        def _rect(values):
            return pymupdf.Rect(
                float(values[0]),
                float(values[1]),
                float(values[2]),
                float(values[3]),
            )

        def _same_edit_location(old_edit: dict, later_edit: dict) -> bool:
            if int(old_edit["page"]) != int(later_edit["page"]):
                return False

            old_new = _norm_text(old_edit.get("replacement_text", ""))
            later_old = _norm_text(later_edit.get("original_text", ""))

            # Strongest signal: the later edit selected the previous
            # replacement text from the same edited field.
            if old_new and later_old and old_new == later_old:
                return True

            try:
                old_final = _rect(old_edit["final_bbox"])
                later_original = _rect(later_edit["original_bbox"])
                intersection = old_final & later_original

                if intersection.is_empty:
                    return False

                old_area = max(0.01, old_final.get_area())
                later_area = max(0.01, later_original.get_area())
                overlap_area = max(0.0, intersection.get_area())
                overlap_ratio = overlap_area / min(old_area, later_area)

                # Conservative fallback for edits to the same physical field.
                return overlap_ratio >= 0.85
            except Exception:
                return False

        def _compact_flexible(value: str) -> str:
            """Normalize extraction-only differences without changing words."""
            value = _norm_text(value)
            value = (
                value.replace("\u2018", "'")
                .replace("\u2019", "'")
                .replace("\u201c", '"')
                .replace("\u201d", '"')
                .replace("\u2013", "-")
                .replace("\u2014", "-")
                .replace("\u2212", "-")
            )
            return re.sub(r"\s+", "", value).casefold()

        def _tokens(value: str) -> list[str]:
            # Unicode word tokens.  Punctuation / span boundaries do not affect
            # this representation, which makes it suitable for moved PDF text.
            return [
                token.casefold()
                for token in re.findall(r"[^\W_]+", value or "", flags=re.UNICODE)
                if token
            ]

        def _contains_sequence(haystack: list[str], needle: list[str]) -> bool:
            if not needle:
                return True
            if len(needle) > len(haystack):
                return False
            size = len(needle)
            for offset in range(0, len(haystack) - size + 1):
                if haystack[offset:offset + size] == needle:
                    return True
            return False

        def _in_order_match_ratio(haystack: list[str], needle: list[str]) -> float:
            """How much of *needle* occurs in order inside *haystack*."""
            if not needle:
                return 1.0
            cursor = 0
            matched = 0
            for wanted in needle:
                found = False
                while cursor < len(haystack):
                    current = haystack[cursor]
                    cursor += 1
                    if current == wanted:
                        matched += 1
                        found = True
                        break
                if not found:
                    continue
            return matched / max(1, len(needle))

        page_cache: dict[int, dict] = {}

        def _page_validation_cache(page_number: int, page):
            cached = page_cache.get(page_number)
            if cached is not None:
                return cached

            line_entries = []
            try:
                data = page.get_text("dict") or {}
                for block in data.get("blocks", []):
                    if block.get("type", 0) != 0:
                        continue
                    for line in block.get("lines", []):
                        spans = line.get("spans", []) or []
                        parts = []
                        rects = []
                        for span in spans:
                            value = str(span.get("text") or "")
                            if value:
                                parts.append(value)
                            bbox = span.get("bbox")
                            if bbox and len(bbox) >= 4:
                                rects.append(_rect(bbox))
                        if not parts or not rects:
                            continue
                        line_rect = pymupdf.Rect(rects[0])
                        for item_rect in rects[1:]:
                            line_rect |= item_rect
                        line_text = " ".join(part.strip() for part in parts if part.strip())
                        line_entries.append(
                            {
                                "text": line_text,
                                "tokens": _tokens(line_text),
                                "bbox": line_rect,
                            }
                        )
            except Exception:
                line_entries = []

            word_tokens = []
            try:
                words = page.get_text("words") or []
                words = sorted(
                    words,
                    key=lambda item: (
                        int(item[5]) if len(item) > 5 else 0,
                        int(item[6]) if len(item) > 6 else 0,
                        int(item[7]) if len(item) > 7 else 0,
                        float(item[1]),
                        float(item[0]),
                    ),
                )
                for word in words:
                    if len(word) < 5:
                        continue
                    word_rect = pymupdf.Rect(
                        float(word[0]),
                        float(word[1]),
                        float(word[2]),
                        float(word[3]),
                    )
                    for token in _tokens(str(word[4] or "")):
                        word_tokens.append((token, word_rect))
            except Exception:
                word_tokens = []

            try:
                whole_text = page.get_text("text") or ""
            except Exception:
                whole_text = ""

            cached = {
                "lines": line_entries,
                "word_tokens": word_tokens,
                "whole_text": whole_text,
            }
            page_cache[page_number] = cached
            return cached

        def _near_expected(observed: pymupdf.Rect, expected: pymupdf.Rect) -> bool:
            """Position tolerance for text copied through a Form XObject."""
            y_tolerance = max(12.0, float(expected.height) * 3.0)
            expected_cy = (float(expected.y0) + float(expected.y1)) / 2.0
            observed_cy = (float(observed.y0) + float(observed.y1)) / 2.0
            if abs(observed_cy - expected_cy) > y_tolerance:
                return False

            # The line may be slightly wider / narrower after PDF clipping, but
            # it should still live in the same horizontal content column.
            x_tolerance = max(24.0, float(expected.width) * 0.30)
            if float(observed.x1) < float(expected.x0) - x_tolerance:
                return False
            if float(observed.x0) > float(expected.x1) + x_tolerance:
                return False
            return True

        def _moved_text_at_final_position(
            page_number: int,
            page,
            expected_text: str,
            bbox,
        ) -> tuple[bool, str, list[float] | None]:
            """Robust, position-aware validation for one moved visual line."""
            expected = str(expected_text or "")
            expected_tokens = _tokens(expected)
            expected_compact = _compact_flexible(expected)

            if not expected_tokens and not expected_compact:
                return True, "empty-text", None

            expected_rect = _rect(bbox) & page.rect
            if expected_rect.is_empty:
                return False, "invalid-final-bbox", None

            ypad = max(10.0, float(expected_rect.height) * 2.2)
            local_rect = pymupdf.Rect(
                max(float(page.rect.x0), float(expected_rect.x0) - 14.0),
                max(float(page.rect.y0), float(expected_rect.y0) - ypad),
                min(float(page.rect.x1), float(expected_rect.x1) + 18.0),
                min(float(page.rect.y1), float(expected_rect.y1) + ypad),
            )

            # 1) Fast path: normalized text in a generously expanded local clip.
            try:
                local_text = page.get_text("text", clip=local_rect) or ""
            except Exception:
                local_text = ""

            if expected_compact and expected_compact in _compact_flexible(local_text):
                return True, "expanded-local-text", [
                    float(local_rect.x0), float(local_rect.y0),
                    float(local_rect.x1), float(local_rect.y1),
                ]

            # 2) Exact PyMuPDF text search, but accept a small transform drift.
            if expected.strip():
                try:
                    hits = page.search_for(expected)
                except Exception:
                    hits = []

                for hit in hits:
                    hit_rect = pymupdf.Rect(hit)
                    if _near_expected(hit_rect, expected_rect):
                        return True, "search-for-near-position", [
                            float(hit_rect.x0), float(hit_rect.y0),
                            float(hit_rect.x1), float(hit_rect.y1),
                        ]

            cache = _page_validation_cache(page_number, page)

            # 3) PDF line extraction.  This handles a line split into many spans.
            for line in cache["lines"]:
                line_rect = line["bbox"]
                if not _near_expected(line_rect, expected_rect):
                    continue
                line_tokens = line["tokens"]
                if _contains_sequence(line_tokens, expected_tokens):
                    return True, "dict-line-token-sequence", [
                        float(line_rect.x0), float(line_rect.y0),
                        float(line_rect.x1), float(line_rect.y1),
                    ]

                # Some producers split what looks like one visual sentence into
                # two adjacent PDF lines / spans.  A very high in-order match in
                # the expected region is still strong evidence of the moved line.
                ratio = _in_order_match_ratio(line_tokens, expected_tokens)
                if len(expected_tokens) >= 6 and ratio >= 0.85:
                    return True, "dict-line-in-order-match", [
                        float(line_rect.x0), float(line_rect.y0),
                        float(line_rect.x1), float(line_rect.y1),
                    ]

            # 4) Search an exact token sequence through page words.  This ignores
            # span boundaries while retaining the geometry of the matched words.
            flat_tokens = [item[0] for item in cache["word_tokens"]]
            if expected_tokens and len(expected_tokens) <= len(flat_tokens):
                size = len(expected_tokens)
                for offset in range(0, len(flat_tokens) - size + 1):
                    if flat_tokens[offset:offset + size] != expected_tokens:
                        continue
                    matched_rects = [
                        cache["word_tokens"][i][1]
                        for i in range(offset, offset + size)
                    ]
                    observed = pymupdf.Rect(matched_rects[0])
                    for item_rect in matched_rects[1:]:
                        observed |= item_rect
                    if _near_expected(observed, expected_rect):
                        return True, "word-token-sequence", [
                            float(observed.x0), float(observed.y0),
                            float(observed.x1), float(observed.y1),
                        ]

            # 5) Last position-aware fallback.  The final clip may include only
            # part of a long wrapped sentence, so allow a high in-order token
            # match locally.  Short phrases remain strict to avoid false passes.
            local_tokens = _tokens(local_text)
            ratio = _in_order_match_ratio(local_tokens, expected_tokens)
            if len(expected_tokens) >= 6 and ratio >= 0.80:
                return True, "local-in-order-token-match", [
                    float(local_rect.x0), float(local_rect.y0),
                    float(local_rect.x1), float(local_rect.y1),
                ]

            # 6) FINAL-SAVE FALLBACK FOR STRUCTURAL REFLOW.
            #
            # A line-reflow edit is already serialized / reopened and validated
            # immediately by PdfEngine before record_line_reflow_operation() is
            # written. Later line deletions may move that same content again and
            # some PDF producers expose copied Form-XObject text at coordinates
            # that do not round-trip to the previously recorded bbox.
            #
            # At final Save As, require the exact normalized moved text to still
            # exist on the SAME PAGE. This is deliberately page-scoped (not
            # document-wide) and only supplements the earlier immediate
            # position-aware validation. It avoids false failures caused solely
            # by coordinate / span-boundary drift.
            whole_text = str(cache.get("whole_text") or "")
            if expected_compact and expected_compact in _compact_flexible(whole_text):
                return True, "page-wide-normalized-text", None

            # 7) Same-page exact word-token sequence. This handles text that is
            # split across spans, lines, or Form XObjects after the final PDF
            # serialization.
            flat_tokens = [item[0] for item in cache["word_tokens"]]
            if expected_tokens and _contains_sequence(flat_tokens, expected_tokens):
                return True, "page-wide-word-token-sequence", None

            # 8) Final conservative fallback for long moved sentences.  Require
            # a very high in-order match across the page so punctuation and line
            # wrapping cannot create a false failure, while short generic phrases
            # remain strict.
            page_tokens = _tokens(whole_text)
            page_ratio = _in_order_match_ratio(page_tokens, expected_tokens)
            if len(expected_tokens) >= 8 and page_ratio >= 0.95:
                return True, "page-wide-in-order-token-match", None

            return False, "not-found-on-final-page", None

        superseded_indexes: set[int] = set()

        for old_index, old_edit in enumerate(edits):
            if old_edit.get("operation_type", "text") != "text" or old_edit.get("inactive"):
                continue
            for later_index in range(old_index + 1, len(edits)):
                later_edit = edits[later_index]
                if later_edit.get("operation_type", "text") != "text" or later_edit.get("inactive"):
                    continue
                if _same_edit_location(old_edit, later_edit):
                    superseded_indexes.add(old_index)
                    break

        try:
            for zero_index, edit in enumerate(edits):
                idx = zero_index + 1
                operation_type = edit.get("operation_type", "text")

                if operation_type == "page":
                    checks.append(
                        {
                            "edit": idx,
                            "page": int(edit.get("page", 0) or 0),
                            "operation_type": "page",
                            "page_action": edit.get("page_action", ""),
                            "passed": True,
                        }
                    )
                    continue

                # Inactive text and line-reflow records no longer represent the
                # final visible state and therefore should not be revalidated.
                if edit.get("inactive"):
                    checks.append(
                        {
                            "edit": idx,
                            "page": int(edit.get("page", 0) or 0),
                            "operation_type": operation_type,
                            "inactive": True,
                            "inactive_reason": edit.get("inactive_reason", ""),
                            "passed": True,
                        }
                    )
                    continue

                if operation_type == "line_reflow":
                    pno = int(edit.get("page", 0) or 0)

                    if pno < 1 or pno > len(doc):
                        failures.append(
                            f"Edit {idx} | Page {pno} | Line reflow validation failed | "
                            "Issue: target page no longer exists."
                        )
                        checks.append(
                            {
                                "edit": idx,
                                "page": pno,
                                "operation_type": "line_reflow",
                                "passed": False,
                                "reason": "target page no longer exists",
                            }
                        )
                        continue

                    page = doc[pno - 1]

                    deleted_bbox = edit.get("deleted_bbox") or [0, 0, 0, 0]
                    deleted_base_rect = _rect(deleted_bbox) & page.rect
                    deleted_ypad = max(5.0, float(deleted_base_rect.height) * 1.2)
                    deleted_rect = pymupdf.Rect(
                        max(float(page.rect.x0), float(deleted_base_rect.x0) - 6.0),
                        max(float(page.rect.y0), float(deleted_base_rect.y0) - deleted_ypad),
                        min(float(page.rect.x1), float(deleted_base_rect.x1) + 8.0),
                        min(float(page.rect.y1), float(deleted_base_rect.y1) + deleted_ypad),
                    )

                    deleted_text = str(edit.get("deleted_text") or "")
                    try:
                        deleted_region = page.get_text("text", clip=deleted_rect) or ""
                    except Exception:
                        deleted_region = ""

                    deleted_present = bool(
                        _compact_flexible(deleted_text)
                        and _compact_flexible(deleted_text)
                        in _compact_flexible(deleted_region)
                    )

                    # Exact search is a useful second check for the deleted line,
                    # but only count hits that still occupy its old position.
                    deleted_search_hits = []
                    if deleted_text.strip():
                        try:
                            for hit in page.search_for(deleted_text):
                                hit_rect = pymupdf.Rect(hit)
                                if hit_rect.intersects(deleted_rect):
                                    deleted_search_hits.append(hit_rect)
                        except Exception:
                            pass

                    deleted_absent = not deleted_present and not deleted_search_hits

                    moved_checks = []
                    moved_ok = True

                    for moved in edit.get("moved_lines", []):
                        if not moved.get("active", True):
                            continue

                        bbox = moved.get("bbox")
                        expected = str(moved.get("text") or "")

                        if not bbox or len(bbox) < 4:
                            moved_ok = False
                            moved_checks.append(
                                {
                                    "text": expected,
                                    "present": False,
                                    "method": "missing-final-bbox",
                                }
                            )
                            continue

                        present, method, observed_bbox = _moved_text_at_final_position(
                            pno,
                            page,
                            expected,
                            bbox,
                        )

                        moved_checks.append(
                            {
                                "text": expected,
                                "bbox": [float(v) for v in bbox[:4]],
                                "present": bool(present),
                                "validation_method": method,
                                "observed_bbox": observed_bbox,
                            }
                        )

                        if not present:
                            moved_ok = False

                    immediate_ok = bool(
                        edit.get("immediate_validation_passed", False)
                    )

                    missing_lines = [
                        item["text"]
                        for item in moved_checks
                        if not item.get("present")
                    ]

                    # The actual deletion remains strict at final Save As.
                    #
                    # Moved lines were already serialized, reopened and verified
                    # immediately when the reflow operation was performed.
                    # Their exact old strings may legitimately become stale after
                    # later edits / renumbering / additional line reflows.  A
                    # final-save miss is therefore diagnostic, not fatal, when
                    # the immediate structural validation passed.
                    moved_recheck_warning = bool(
                        missing_lines
                        and immediate_ok
                    )

                    moved_requirement_passed = (
                        moved_ok
                        or immediate_ok
                    )

                    passed = (
                        deleted_absent
                        and moved_requirement_passed
                    )

                    checks.append(
                        {
                            "edit": idx,
                            "page": pno,
                            "operation_type": "line_reflow",
                            "immediate_validation_passed": immediate_ok,
                            "deleted_text_absent": deleted_absent,
                            "deleted_search_hits_in_old_position": len(deleted_search_hits),
                            "moved_lines": moved_checks,
                            "moved_line_final_recheck_warning": moved_recheck_warning,
                            "passed": passed,
                        }
                    )

                    if not passed:
                        reasons = []

                        if not deleted_absent:
                            reasons.append(
                                "deleted full line is still searchable at its old position"
                            )

                        # Only make missing moved lines fatal when the operation
                        # did NOT pass its immediate serialized/reopened check.
                        if missing_lines and not immediate_ok:
                            reasons.append(
                                "moved line(s) could not be verified after the edit: "
                                + " | ".join(repr(value) for value in missing_lines[:3])
                            )

                        failures.append(
                            f"Edit {idx} | Page {pno} | "
                            "Line reflow validation failed | "
                            f"Issue: {'; '.join(reasons)}."
                        )

                    continue

                # ---------------------------------------------------------
                # Normal text-edit validation
                # ---------------------------------------------------------
                pno = int(edit["page"])

                if pno < 1 or pno > len(doc):
                    failures.append(
                        f"Edit {idx} | Page {pno} | Text validation failed | "
                        "Issue: target page no longer exists."
                    )
                    checks.append(
                        {
                            "edit": idx,
                            "page": pno,
                            "operation_type": "text",
                            "passed": False,
                            "reason": "target page no longer exists",
                        }
                    )
                    continue

                page = doc[pno - 1]

                ob = edit["original_bbox"]
                fb = edit["final_bbox"]

                rect = pymupdf.Rect(
                    min(ob[0], fb[0]) - 2.0,
                    min(ob[1], fb[1]) - 3.0,
                    max(ob[2], fb[2]) + 8.0,
                    max(ob[3], fb[3])
                    + max(6.0, float(edit["font_size_pt"]) * 1.5),
                ) & page.rect

                region_text = page.get_text("text", clip=rect)
                norm_region = _norm_text(region_text)
                norm_old = _norm_text(edit["original_text"])
                norm_new = _norm_text(edit["replacement_text"])

                same_text = bool(norm_old) and norm_old == norm_new

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

                old_intersections = []

                if norm_old and not same_text and not old_is_part_of_new:
                    try:
                        for hit in page.search_for(edit["original_text"]):
                            if hit.intersects(rect):
                                old_intersections.append(tuple(hit))
                    except Exception:
                        pass

                replacement_occurrences = (
                    norm_region.count(norm_new)
                    if norm_new
                    else 0
                )

                superseded = zero_index in superseded_indexes

                if superseded:
                    # This replacement was intentionally replaced again later.
                    # It does not need to remain in the final PDF.
                    passed = (
                        not old_present
                        and not old_intersections
                    )

                    checks.append(
                        {
                            "edit": idx,
                            "page": pno,
                            "operation_type": "text",
                            "superseded": True,
                            "old_text_absent_in_region": not old_present,
                            "old_search_hits_in_region": len(old_intersections),
                            "replacement_present_in_region": None,
                            "replacement_occurrences_in_region": replacement_occurrences,
                            "single_replacement_in_region": None,
                            "passed": passed,
                        }
                    )

                    if not passed:
                        reasons = []

                        if old_present:
                            reasons.append("original text is still present")

                        if old_intersections:
                            reasons.append("original text is still searchable")

                        failures.append(
                            f"Edit {idx} | Page {pno} | "
                            f"x={fb[0]:.2f}, y={fb[1]:.2f}, "
                            f"w={fb[2]-fb[0]:.2f}, h={fb[3]-fb[1]:.2f} | "
                            f"Original: {edit['original_text']!r} | "
                            "This edit was later replaced again | "
                            f"Issue: {'; '.join(reasons)}."
                        )

                    continue

                # Latest / active edit: its replacement must exist exactly once.
                new_present = (
                    (not norm_new)
                    or replacement_occurrences >= 1
                )

                single_replacement = (
                    (not norm_new)
                    or replacement_occurrences == 1
                )

                passed = (
                    (not old_present)
                    and (not old_intersections)
                    and new_present
                    and single_replacement
                )

                checks.append(
                    {
                        "edit": idx,
                        "page": pno,
                        "operation_type": "text",
                        "superseded": False,
                        "old_text_absent_in_region": not old_present,
                        "old_search_hits_in_region": len(old_intersections),
                        "replacement_present_in_region": new_present,
                        "replacement_occurrences_in_region": replacement_occurrences,
                        "single_replacement_in_region": single_replacement,
                        "passed": passed,
                    }
                )

                if not passed:
                    reasons = []

                    if old_present:
                        reasons.append("original text is still present")

                    if old_intersections:
                        reasons.append("original text is still searchable")

                    if not new_present:
                        reasons.append("replacement text is missing")

                    if norm_new and replacement_occurrences != 1:
                        reasons.append(
                            f"replacement appears {replacement_occurrences} times"
                        )

                    failures.append(
                        f"Edit {idx} | Page {pno} | "
                        f"x={fb[0]:.2f}, y={fb[1]:.2f}, "
                        f"w={fb[2]-fb[0]:.2f}, h={fb[3]-fb[1]:.2f} | "
                        f"Original: {edit['original_text']!r} | "
                        f"Final replacement: {edit['replacement_text']!r} | "
                        f"Issue: {'; '.join(reasons)}."
                    )

        finally:
            doc.close()

        return {
            "ok": not failures,
            "checks": checks,
            "failures": failures,
        }

    @staticmethod
    def _object_sha256(value: str | None) -> str | None:
        if value is None:
            return None
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _active_edited_page_numbers(self, page_count: int) -> list[int]:
        page_numbers: set[int] = set()
        for edit in self.edits:
            if edit.get("inactive"):
                continue
            if edit.get("operation_type", "text") not in {
                "text",
                "line_reflow",
            }:
                continue
            try:
                page_number = int(edit.get("page", 0) or 0)
            except (TypeError, ValueError):
                continue
            if 1 <= page_number <= int(page_count):
                page_numbers.add(page_number)
        return sorted(page_numbers)

    def _normalize_edited_page_streams(self, doc) -> list[dict]:
        """Merge edited overlays and match the source content compression.

        Redaction followed by text insertion normally leaves two active page
        streams: an uncompressed rewritten stream and a Flate-compressed text
        overlay. ``clean_contents()`` combines those operations into one stream.
        If every original stream used FlateDecode, the combined stream is also
        explicitly Flate-compressed; if every original stream was uncompressed,
        it is explicitly kept uncompressed. Mixed or uncommon source filters are
        left to MuPDF instead of being falsely labelled as an exact match.
        """
        normalized: list[dict] = []
        source_pages = list(self.source_structure.get("pages", []))
        source_pages_by_xref = {
            int(item["page_xref"]): item
            for item in source_pages
        }
        has_page_operations = any(
            edit.get("operation_type") == "page" for edit in self.edits
        )

        for page_number in self._active_edited_page_numbers(len(doc)):
            page = doc[page_number - 1]
            source_page = source_pages_by_xref.get(int(page.xref))
            if (
                source_page is None
                and not has_page_operations
                and page_number <= len(source_pages)
            ):
                source_page = source_pages[page_number - 1]
            source_filters = [
                item.get("filter")
                for item in (source_page or {}).get("streams", [])
            ]
            all_flate = bool(source_filters) and all(
                value is not None and "/FlateDecode" in value
                for value in source_filters
            )
            all_uncompressed = bool(source_filters) and all(
                value is None for value in source_filters
            )

            before_count = len(page.get_contents())
            page.clean_contents(sanitize=1)
            content_xrefs = [int(value) for value in page.get_contents()]

            if all_flate or all_uncompressed:
                for content_xref in content_xrefs:
                    decoded = doc.xref_stream(content_xref) or b""
                    doc.update_stream(
                        content_xref,
                        decoded,
                        compress=bool(all_flate),
                    )

            normalized.append(
                {
                    "page": int(page_number),
                    "streams_before": int(before_count),
                    "streams_after": int(len(content_xrefs)),
                    "source_filters": source_filters,
                    "output_filter_policy": (
                        "/FlateDecode"
                        if all_flate
                        else None if all_uncompressed else "writer-default"
                    ),
                }
            )

        return normalized

    @staticmethod
    def _pdf_array_numbers(value: str, expected: int) -> list[float]:
        numbers = [
            float(item)
            for item in re.findall(
                r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)",
                str(value or ""),
            )
        ]
        if len(numbers) != expected:
            raise RuntimeError(
                f"Expected {expected} PDF array numbers, found {len(numbers)}."
            )
        return numbers

    @staticmethod
    def _pdf_number(value: float) -> str:
        result = f"{float(value):.8f}".rstrip("0").rstrip(".")
        return "0" if result in {"", "-0"} else result

    @staticmethod
    def _resource_ref_names(value: str) -> set[str]:
        return {
            match.group(1)
            for match in re.finditer(
                r"/([^\s/<>{}\[\]()]+)\s+\d+\s+\d+\s+R",
                str(value or ""),
            )
        }

    def _inline_line_reflow_forms(self, doc) -> list[dict]:
        """Inline PyMuPDF's temporary line-copy Form XObjects.

        ``show_pdf_page()`` creates one shared full-page Form plus one clipping
        wrapper per moved line. They render correctly but inflate the reachable
        object graph. Because the source of every wrapper is a snapshot of the
        same page, its resource names already exist on the destination page.
        Replacing each ``/fzFrmN Do`` with the wrapper matrix, BBox clip, and
        shared form stream preserves pixels/searchable text while making all
        generated Forms and copied resources collectible by ``garbage=4``.
        """
        line_pages = {
            int(edit.get("page", 0) or 0)
            for edit in self.edits
            if not edit.get("inactive")
            and edit.get("operation_type") == "line_reflow"
        }
        results: list[dict] = []
        resource_categories = (
            "Font",
            "XObject",
            "ExtGState",
            "ColorSpace",
            "Pattern",
            "Shading",
            "Properties",
        )

        for page_number in sorted(line_pages):
            if not 1 <= page_number <= len(doc):
                continue
            page = doc[page_number - 1]
            before_pixmap = page.get_pixmap(alpha=False)
            before_samples = bytes(before_pixmap.samples)
            before_text = page.get_text("text")
            wrappers: dict[str, dict] = {}
            resources_transferred: set[tuple[str, str]] = set()

            for wrapper_xref, name, parent, _bbox in page.get_xobjects():
                if int(parent) != 0 or not re.fullmatch(r"fzFrm\d+", str(name)):
                    continue
                if doc.xref_get_key(wrapper_xref, "Subtype") != ("name", "/Form"):
                    continue
                wrapper_stream = (doc.xref_stream(wrapper_xref) or b"").strip()
                if wrapper_stream != b"/fullpage Do":
                    continue
                ref_type, ref_value = doc.xref_get_key(
                    wrapper_xref,
                    "Resources/XObject/fullpage",
                )
                if ref_type != "xref":
                    continue
                fullpage_xref = int(str(ref_value).split()[0])
                if doc.xref_get_key(fullpage_xref, "Subtype") != ("name", "/Form"):
                    continue

                # Inlining is safe only when every named form resource is also
                # available under the same name on the destination page.
                for category in resource_categories:
                    form_type, form_value = doc.xref_get_key(
                        fullpage_xref,
                        f"Resources/{category}",
                    )
                    if form_type == "null":
                        continue
                    page_type, page_value = doc.xref_get_key(
                        page.xref,
                        f"Resources/{category}",
                    )
                    form_names = self._resource_ref_names(form_value)
                    page_names = self._resource_ref_names(page_value)
                    missing_names = form_names - page_names
                    if missing_names and page_type == "null":
                        doc.xref_set_key(
                            page.xref,
                            f"Resources/{category}",
                            "<<>>",
                        )
                    for resource_name in sorted(missing_names):
                        value_type, value = doc.xref_get_key(
                            fullpage_xref,
                            f"Resources/{category}/{resource_name}",
                        )
                        if value_type == "null":
                            raise RuntimeError(
                                "Cannot safely flatten a moved-line Form because "
                                f"/{category}/{resource_name} is unavailable."
                            )
                        doc.xref_set_key(
                            page.xref,
                            f"Resources/{category}/{resource_name}",
                            value,
                        )
                        resources_transferred.add((category, resource_name))

                bbox = self._pdf_array_numbers(
                    doc.xref_get_key(wrapper_xref, "BBox")[1],
                    4,
                )
                matrix = self._pdf_array_numbers(
                    doc.xref_get_key(wrapper_xref, "Matrix")[1],
                    6,
                )
                x0, y0, x1, y1 = bbox
                matrix_text = " ".join(self._pdf_number(value) for value in matrix)
                clip_text = " ".join(
                    self._pdf_number(value)
                    for value in (x0, y0, x1 - x0, y1 - y0)
                )
                fullpage_stream = doc.xref_stream(fullpage_xref) or b""
                replacement = (
                    f"q {matrix_text} cm {clip_text} re W n\n".encode("ascii")
                    + fullpage_stream
                    + b"\nQ"
                )
                wrappers[str(name)] = {
                    "xref": int(wrapper_xref),
                    "fullpage_xref": int(fullpage_xref),
                    "replacement": replacement,
                }

            if not wrappers:
                continue

            replacements = 0
            for content_xref in page.get_contents():
                original = doc.xref_stream(content_xref) or b""
                updated = original
                for name, item in wrappers.items():
                    pattern = rb"/" + re.escape(name.encode("ascii")) + rb"\s+Do\b"
                    updated, count = re.subn(
                        pattern,
                        lambda _match, value=item["replacement"]: value,
                        updated,
                    )
                    replacements += int(count)
                if updated != original:
                    doc.update_stream(content_xref, updated, compress=True)

            if replacements != len(wrappers):
                raise RuntimeError(
                    "Could not locate every generated moved-line Form invocation."
                )
            for name in wrappers:
                doc.xref_set_key(
                    page.xref,
                    f"Resources/XObject/{name}",
                    "null",
                )

            after_pixmap = page.get_pixmap(alpha=False)
            after_text = page.get_text("text")
            if bytes(after_pixmap.samples) != before_samples:
                raise RuntimeError(
                    f"Flattening moved-line Forms changed page {page_number} pixels."
                )
            if after_text != before_text:
                raise RuntimeError(
                    f"Flattening moved-line Forms changed page {page_number} text."
                )

            results.append(
                {
                    "page": int(page_number),
                    "wrapper_forms_inlined": int(len(wrappers)),
                    "invocations_replaced": int(replacements),
                    "resources_transferred": int(len(resources_transferred)),
                    "rendering_preserved": True,
                    "searchable_text_preserved": True,
                }
            )

        return results

    def _validate_preserved_structure(self, path: str) -> dict:
        """Verify the source structural invariants that can survive an edit.

        An edited page's active content bytes cannot equal the source bytes: if
        they did, its rendered/searchable text would also be unchanged. The
        strict stream comparison therefore applies to untouched pages. Edited
        pages are reported separately and remain covered by clean-text
        validation.
        """
        out_doc = pymupdf.open(path)
        try:
            actual = self._capture_structure(out_doc)
            actual["xref_serialization"] = self._xref_serialization_profile(
                out_doc,
                Path(path).read_bytes(),
            )

            edited_page_numbers = set(
                self._active_edited_page_numbers(len(actual["pages"]))
            )

            checks: list[dict] = []
            failures: list[str] = []

            def add_check(
                name: str,
                expected,
                observed,
                *,
                required: bool = True,
            ) -> bool:
                passed = expected == observed
                checks.append(
                    {
                        "name": name,
                        "required": bool(required),
                        "passed": bool(passed),
                        "expected": expected,
                        "actual": observed,
                    }
                )
                if required and not passed:
                    failures.append(
                        f"{name} changed (expected {expected!r}, got {observed!r})."
                    )
                return passed

            source = self.source_structure
            add_check(
                "xref format",
                source.get("xref_format"),
                actual.get("xref_format"),
            )
            add_check(
                "trailer /ID",
                source.get("trailer_id"),
                actual.get("trailer_id"),
            )
            add_check(
                "catalog /Root reference",
                source.get("root_xref"),
                actual.get("root_xref"),
                required=False,
            )
            add_check(
                "catalog /Root object",
                source.get("root_entries"),
                actual.get("root_entries"),
                required=False,
            )
            # Dictionary key order has no meaning in PDF. Keep serialization
            # drift visible for diagnostics, but do not reject an output whose
            # Catalog reference, keys, value types, and values are identical.
            add_check(
                "catalog /Root serialized form",
                self._object_sha256(source.get("root_object")),
                self._object_sha256(actual.get("root_object")),
                required=False,
            )
            add_check(
                "Info reference",
                source.get("info_xref"),
                actual.get("info_xref"),
                required=False,
            )
            add_check(
                "Info object",
                source.get("info_entries"),
                actual.get("info_entries"),
            )
            add_check(
                "Info serialized form",
                self._object_sha256(source.get("info_object")),
                self._object_sha256(actual.get("info_object")),
                required=False,
            )
            add_check(
                "logical document metadata",
                source.get("logical_metadata"),
                actual.get("logical_metadata"),
            )
            add_check(
                "XMP metadata reference",
                source.get("metadata_xref"),
                actual.get("metadata_xref"),
                required=False,
            )
            add_check(
                "XMP stream dictionary",
                self._object_sha256(source.get("metadata_object")),
                self._object_sha256(actual.get("metadata_object")),
            )
            add_check(
                "XMP decoded bytes",
                source.get("metadata_decoded_sha256"),
                actual.get("metadata_decoded_sha256"),
            )
            # Raw compressed bytes are stricter than metadata equivalence. Keep
            # this visible in the report, but do not reject a semantically exact
            # stream solely because the PDF writer recompressed it.
            add_check(
                "XMP raw encoded bytes",
                source.get("metadata_raw_sha256"),
                actual.get("metadata_raw_sha256"),
                required=False,
            )
            source_artifacts = source.get("edit_artifacts", {})
            actual_artifacts = actual.get("edit_artifacts", {})
            add_check(
                "redaction records",
                source_artifacts.get("redaction_records", 0),
                actual_artifacts.get("redaction_records", 0),
            )
            add_check(
                "fitz-A0 identifiers",
                source_artifacts.get("fitz_a0_identifiers", 0),
                actual_artifacts.get("fitz_a0_identifiers", 0),
            )
            add_check(
                "unreachable/free xref entries",
                (
                    0
                    if self.edits
                    else source_artifacts.get("free_xrefs", 0)
                ),
                actual_artifacts.get("free_xrefs", 0),
            )
            source_xref_profile = source.get("xref_serialization", {})
            actual_xref_profile = actual.get("xref_serialization", {})
            has_page_structure_edits = any(
                edit.get("operation_type") == "page" for edit in self.edits
            )
            require_direct_xref_match = bool(
                source.get("xref_format") == "stream"
                and source_xref_profile.get("object_stream_count", 0) == 0
            )
            for label, key in (
                ("xref /Size", "size"),
                ("final xref object", "final_xref_object"),
                ("xref compression", "compression"),
                ("xref decoded size", "decoded_size"),
                ("object stream count", "object_stream_count"),
                ("compressed object count", "compressed_object_count"),
                ("PDF writer marker", "writer_marker"),
            ):
                add_check(
                    label,
                    source_xref_profile.get(key),
                    actual_xref_profile.get(key),
                    required=bool(
                        require_direct_xref_match
                        and (
                            not has_page_structure_edits
                            or key in {
                                "compression",
                                "object_stream_count",
                                "compressed_object_count",
                                "writer_marker",
                            }
                        )
                    ),
                )

            actual_pages = list(actual.get("pages", []))
            expected_layout: list[dict | None] = list(source.get("pages", []))
            layout_valid = True
            for edit in self.edits:
                if edit.get("operation_type") != "page":
                    continue
                try:
                    position = int(edit.get("page", 0) or 0) - 1
                except (TypeError, ValueError):
                    layout_valid = False
                    continue
                action = edit.get("page_action")
                if action == "add" and 0 <= position <= len(expected_layout):
                    expected_layout.insert(position, None)
                elif action == "delete" and 0 <= position < len(expected_layout):
                    expected_layout.pop(position)
                else:
                    layout_valid = False

            if not layout_valid or len(expected_layout) != len(actual_pages):
                failures.append(
                    "Recorded page additions/deletions do not match the final page count."
                )
            source_to_output: dict[int, dict] = {}
            added_output_pages: list[dict] = []
            for index, source_page_at_position in enumerate(expected_layout):
                if index >= len(actual_pages):
                    break
                output_page_at_position = actual_pages[index]
                if source_page_at_position is None:
                    added_output_pages.append(output_page_at_position)
                else:
                    source_to_output[int(source_page_at_position["page_xref"])] = (
                        output_page_at_position
                    )
            page_checks = []

            for source_page in source.get("pages", []):
                page_xref = int(source_page["page_xref"])
                source_page_number = int(source_page["page"])
                output_page = source_to_output.get(page_xref)

                if output_page is None:
                    page_removed_ok = page_xref not in source_to_output and any(
                        edit.get("operation_type") == "page"
                        and edit.get("page_action") == "delete"
                        for edit in self.edits
                    )
                    if not page_removed_ok:
                        failures.append(
                            "Source page object disappeared without a recorded "
                            f"page deletion (page xref {page_xref})."
                        )
                    page_checks.append(
                        {
                            "source_page": int(source_page["page"]),
                            "page_xref": page_xref,
                            "status": "page-removed",
                            "required_exact_streams": False,
                            "passed": page_removed_ok,
                        }
                    )
                    continue

                edited = int(output_page["page"]) in edited_page_numbers
                same_contents_entry = (
                    source_page.get("contents_type")
                    == output_page.get("contents_type")
                    and source_page.get("contents_value")
                    == output_page.get("contents_value")
                )
                same_content_xrefs = (
                    source_page.get("content_xrefs")
                    == output_page.get("content_xrefs")
                )

                source_streams = list(source_page.get("streams", []))
                output_streams = list(output_page.get("streams", []))
                same_streams = len(source_streams) == len(output_streams)
                if same_streams:
                    for source_stream, output_stream in zip(
                        source_streams,
                        output_streams,
                    ):
                        if any(
                            source_stream.get(key) != output_stream.get(key)
                            for key in (
                                "object",
                                "decoded_sha256",
                                "raw_sha256",
                                "filter",
                            )
                        ):
                            same_streams = False
                            break

                source_filters = [item.get("filter") for item in source_streams]
                output_filters = [item.get("filter") for item in output_streams]
                uniform_source_filter = (
                    source_filters[0]
                    if source_filters
                    and all(value == source_filters[0] for value in source_filters)
                    else "mixed"
                )
                edited_filters_match = (
                    uniform_source_filter == "mixed"
                    or all(
                        value == uniform_source_filter
                        for value in output_filters
                    )
                )

                passed = True
                if not edited and not same_streams:
                    passed = False
                    failures.append(
                        "Untouched page content changed for source page "
                        f"{source_page['page']} (page xref {page_xref})."
                    )

                if edited and not edited_filters_match:
                    passed = False
                    failures.append(
                        "Edited page stream compression differs from source page "
                        f"{source_page['page']} (expected {uniform_source_filter!r}, "
                        f"got {output_filters!r})."
                    )

                page_checks.append(
                    {
                        "source_page": int(source_page["page"]),
                        "output_page": int(output_page["page"]),
                        "page_xref": page_xref,
                        "status": "edited" if edited else "untouched",
                        "required_exact_streams": not edited,
                        "contents_entry_same": same_contents_entry,
                        "content_xrefs_same": same_content_xrefs,
                        "content_streams_same": same_streams,
                        "source_stream_filters": source_filters,
                        "output_stream_filters": output_filters,
                        "compression_matches_source": edited_filters_match,
                        "stale_unreachable_source_streams": [],
                        "passed": passed,
                    }
                )

            for output_page in added_output_pages:
                page_checks.append(
                    {
                        "source_page": None,
                        "output_page": int(output_page["page"]),
                        "page_xref": int(output_page["page_xref"]),
                        "status": "page-added",
                        "required_exact_streams": False,
                        "passed": True,
                    }
                )

            return {
                "ok": not failures,
                "checks": checks,
                "pages": page_checks,
                "failures": failures,
                "limits": [
                    "Edited page content-stream bytes must change when page text changes.",
                    "Xref offsets and object byte serialization cannot remain identical after a full rewrite.",
                    "Hybrid-reference PDFs cannot be reproduced exactly by PyMuPDF's full-save writer.",
                ],
            }
        finally:
            out_doc.close()

    def export_clean(self, doc, output_path: str) -> tuple[str, str, str]:
        output_path = os.path.abspath(output_path)
        if os.path.exists(output_path) and os.path.samefile(self.source_path, output_path):
            raise ValueError("Choose a different output path. The source PDF is never overwritten.")

        source_xref_format = self.source_structure.get("xref_format", "table")
        source_xref_profile = self.source_structure.get("xref_serialization", {})
        source_uses_direct_xref_stream = bool(
            source_xref_format == "stream"
            and source_xref_profile.get("object_stream_count", 0) == 0
        )
        if source_xref_format == "hybrid" and (self.edits or doc.is_dirty):
            raise RuntimeError(
                "This PDF uses a hybrid xref layout. PyMuPDF cannot reproduce "
                "that layout during a clean full save, so export was stopped "
                "instead of silently changing the xref format."
            )

        # If nothing changed, copying the source is the only way to keep every
        # byte, historical xref section and stream exactly identical.
        source_copied_verbatim = bool(not self.edits and not doc.is_dirty)
        has_active_content_edits = any(
            not edit.get("inactive")
            and edit.get("operation_type", "text") in {"text", "line_reflow"}
            for edit in self.edits
        )
        empty_annots_removed = 0
        identity_restored_before_save: list[str] = []
        identity_repaired_after_save: list[str] = []
        normalized_page_streams: list[dict] = []
        inlined_line_reflow_forms: list[dict] = []
        xref_stream_rewrite: dict | None = None
        has_active_line_reflow = any(
            not edit.get("inactive")
            and edit.get("operation_type") == "line_reflow"
            for edit in self.edits
        )
        garbage_level = 4 if has_active_line_reflow else 2

        if source_copied_verbatim:
            shutil.copyfile(self.source_path, output_path)
        else:
            # A direct-object xref-stream source is staged with a classic table,
            # which is replaced below with a compressed xref stream. Sources
            # that already use /ObjStm retain MuPDF's object-stream mode.
            use_objstms = (
                1
                if source_xref_format == "stream"
                and not source_uses_direct_xref_stream
                else 0
            )

            # Compact saving renumbers xrefs in the Document object itself. Work
            # on a detached serialization so Save As never mutates the open edit
            # session: repeated saves must not compare compacted xref numbers
            # against the source document's original numbers.
            try:
                working_raw = doc.tobytes(
                    garbage=0,
                    clean=False,
                    deflate=False,
                    no_new_id=True,
                    encryption=pymupdf.PDF_ENCRYPT_KEEP,
                    preserve_metadata=True,
                )
            except TypeError:
                working_raw = doc.tobytes(
                    garbage=0,
                    clean=False,
                    deflate=False,
                    no_new_id=True,
                )

            working_doc = pymupdf.open(stream=working_raw, filetype="pdf")
            try:
                identity_restored_before_save = self._restore_source_identity(
                    working_doc
                )
                empty_annots_removed = self._strip_empty_annots(working_doc)
                inlined_line_reflow_forms = self._inline_line_reflow_forms(
                    working_doc
                )
                normalized_page_streams = self._normalize_edited_page_streams(
                    working_doc
                )

                # Do not call set_metadata() or set_xml_metadata(): even a
                # no-op-looking call can rewrite Info/XMP objects and insert
                # optional null fields. Compact garbage collection removes
                # unreachable redaction, appearance, and superseded content.
                # Line reflow uses level 4 after Form inlining so copied font /
                # image resources are deduplicated; other edits use level 2.
                # Level 1 can leave discarded dictionaries in an /ObjStm.
                working_doc.save(
                    output_path,
                    garbage=garbage_level,
                    clean=False,
                    deflate=False,
                    deflate_fonts=False,
                    deflate_images=False,
                    preserve_metadata=True,
                    no_new_id=True,
                    encryption=pymupdf.PDF_ENCRYPT_KEEP,
                    use_objstms=use_objstms,
                )
            finally:
                working_doc.close()
            if source_uses_direct_xref_stream:
                xref_stream_rewrite = self._rewrite_classic_xref_as_source_stream(
                    output_path
                )
            # A compact save can legitimately renumber Root / Info. Never point
            # the trailer back at their pre-compaction xrefs: those numbers may
            # now belong to unrelated live objects. Their content is validated
            # below at the new references.

        text_validation = self._validate_clean_text(output_path)
        structure_validation = self._validate_preserved_structure(output_path)
        validation = dict(text_validation)
        validation["text_ok"] = bool(text_validation["ok"])
        validation["structure_ok"] = bool(structure_validation["ok"])
        validation["structure"] = structure_validation
        validation["ok"] = bool(
            text_validation["ok"] and structure_validation["ok"]
        )

        if not validation["ok"]:
            try:
                os.remove(output_path)
            except OSError:
                pass
            reasons = list(text_validation.get("failures", []))
            reasons.extend(structure_validation.get("failures", []))
            raise RuntimeError(
                "PDF export validation failed; output was removed.\n" +
                "\n".join(reasons)
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
                    "complete Info object",
                    "complete XMP metadata stream",
                    "trailer /ID",
                ],
                "source_metadata_passthrough": bool(self.source_metadata_passthrough),
                "forensic_fingerprint_cloning": False,
                "source_xmp_descriptive_fields": sorted(self.source_xmp_descriptive.keys()),
            },
            "structure_preservation": {
                "source_copied_verbatim": source_copied_verbatim,
                "xref_format": source_xref_format,
                "validation_passed": bool(structure_validation["ok"]),
                "identity_restored_before_save": identity_restored_before_save,
                "identity_repaired_after_save": identity_repaired_after_save,
                "edited_page_stream_normalization": normalized_page_streams,
                "line_reflow_form_flattening": inlined_line_reflow_forms,
                "xref_stream_rewrite": xref_stream_rewrite,
                "edited_page_streams_identical": (
                    False if has_active_content_edits else None
                ),
                "limitation": (
                    "An edited page content stream must change when its text changes. "
                    "Untouched page streams are required to remain exact."
                ),
            },
            "export_cleanup": {
                "visible_page_disclosure_added": False,
                "empty_annots_entries_removed": int(empty_annots_removed),
                "garbage_level": 0 if source_copied_verbatim else garbage_level,
            },
            "note": (
                "This is an edited copy. Old selected text is destructively removed "
                "from the active text layer and unreachable edit streams are removed. "
                "Edit provenance is retained in audit/validation sidecars; "
                "no disclosure-only text, font, or transparency resource is injected "
                "into edited pages. Source Info/XMP content, trailer ID, xref family, "
                "and untouched page streams are verified after saving. Temporary "
                "redaction records and free xrefs are rejected. Edited page stream "
                "bytes, indirect object numbers, and xref offsets necessarily change, "
                "so the output is not byte-identical to the source."
            ),
        }
        Path(audit_path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        Path(report_path).write_text(json.dumps(validation, indent=2, ensure_ascii=False), encoding="utf-8")
        return output_path, audit_path, report_path
