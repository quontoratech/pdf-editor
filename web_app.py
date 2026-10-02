from __future__ import annotations

import copy
import difflib
import math
import os
import shutil
import tempfile
import threading
import time
import uuid
import hashlib
import hmac
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

import pymupdf
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from pymongo import ASCENDING, MongoClient
    from pymongo.errors import DuplicateKeyError, PyMongoError
    from bson import ObjectId
    from bson.errors import InvalidId
    from gridfs import GridFS
except ImportError:
    ASCENDING = None
    MongoClient = None
    ObjectId = None
    GridFS = None
    InvalidId = Exception
    DuplicateKeyError = PyMongoError = Exception


from app.clean_edit_session import CleanEditSession, APP_NAME
from app.cff_subset_extender import (
    build_adaptive_external_glyph_plan,
    build_adaptive_pdf_glyph_plan,
    extension_unicode_widths,
    inspect_existing_cff,
    verify_donor_compatibility,
)
from app.docx_reference import DocxReferenceIndex
from app.env import load_env_file
from app.font_resolver import FontResolver, humanize_pdf_font_name
from app.online_font_resolver import OnlineFontResolver
from app.font_utils import (
    font_path_has_text,
    font_flags_text,
    normalize_font_name,
    read_font_metadata_from_path,
    strip_subset_prefix,
    weight_label,
    weight_from_flags,
    italic_from_flags,
)
from app.pdf_engine import PdfEngine
from app.models import FontCandidate, FontResolution


BASE_DIR = Path(__file__).resolve().parent

load_env_file(BASE_DIR / ".env")

WEB_DIR = BASE_DIR / "web"

RUNTIME_DIR = Path(tempfile.gettempdir()) / "pdf_text_style_editor_web"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_DOCX_BYTES = 50 * 1024 * 1024
MAX_FONT_BYTES = 50 * 1024 * 1024

SESSION_TTL_SECONDS = 6 * 60 * 60

AUTH_COOKIE_NAME = "pdf_text_browser_auth"
AUTH_SESSION_TTL_SECONDS = 12 * 60 * 60

AUTH_COOKIE_SECURE = os.environ.get(
    "AUTH_COOKIE_SECURE",
    "",
).lower() in {
    "1",
    "true",
    "yes",
}


MONGO_URI = (
    os.environ.get("MONGO_URI")
    or os.environ.get("MONGODB_URI")
)

MONGO_DB_NAME = os.environ.get(
    "MONGO_DB_NAME",
    "pdf_text_browser",
)

MONGO_CONNECT_RETRIES = int(
    os.environ.get(
        "MONGO_CONNECT_RETRIES",
        "3",
    )
)

MONGO_SERVER_SELECTION_TIMEOUT_MS = int(
    os.environ.get(
        "MONGO_SERVER_SELECTION_TIMEOUT_MS",
        "10000",
    )
)

PASSWORD_ITERATIONS = 260_000


class PointRequest(BaseModel):
    x: float
    y: float


class PageRequest(BaseModel):
    page_index: int


class TextRequest(BaseModel):
    replacement: str
    direction: str = "right"
    fallback_token: str | None = None


class DeleteLineRequest(BaseModel):
    # Backward-compatible default: preserve every detected rule and move it
    # with the upward row reflow. Removing the deleted row's boundary is an
    # explicit user choice.
    remove_table_rule: bool = False


class InsertTableRowRequest(BaseModel):
    # The first safe UI supports insertion directly below the selected row.
    # Keeping this explicit in the request leaves room for an "above" control
    # later without accepting an arbitrary placement now.
    position: str = "below"


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str


@dataclass(frozen=True)
class UserContext:
    user_id: str
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def public_data(self) -> dict[str, str]:
        return {
            "id": self.user_id,
            "username": self.username,
            "role": self.role,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_username(username: str) -> str:
    return (username or "").strip()


def _clean_role(role: Any) -> str:
    value = str(role or "user").strip().lower()
    return value or "user"


def _password_digest(
    password: str,
    salt: str,
    iterations: int = PASSWORD_ITERATIONS,
) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()


def _password_fields(password: str) -> dict[str, Any]:
    salt = secrets.token_hex(16)

    return {
        "password_salt": salt,
        "password_hash": _password_digest(
            password,
            salt,
        ),
        "password_iterations": PASSWORD_ITERATIONS,
    }


def _verify_password(
    password: str,
    user: dict[str, Any],
) -> bool:

    salt = user.get("password_salt")
    stored = user.get("password_hash")

    if salt and stored:

        iterations = int(
            user.get("password_iterations")
            or PASSWORD_ITERATIONS
        )

        digest = _password_digest(
            password,
            salt,
            iterations,
        )

        return hmac.compare_digest(
            digest,
            stored,
        )

    mongo_password = user.get("password")

    if isinstance(mongo_password, str):
        return hmac.compare_digest(
            password,
            mongo_password,
        )

    return False


def _public_user(
    user: dict[str, Any],
) -> dict[str, Any]:

    created_at = user.get("created_at")

    if isinstance(created_at, datetime):
        created_at_value = created_at.isoformat()
    else:
        created_at_value = created_at

    return {
        "id": str(user.get("_id", "")),
        "username": user.get("username", ""),
        "role": _clean_role(
            user.get("role")
        ),
        "created_at": created_at_value,
    }


class AuthStore:

    def __init__(self):

        if MongoClient is None:
            raise RuntimeError(
                "pymongo is not installed. "
                "Run `pip install -r requirements.txt` first."
            )

        if not MONGO_URI:
            raise RuntimeError(
                "MONGO_URI is not set in the environment."
            )

        last_error: PyMongoError | None = None

        for attempt in range(
            max(
                1,
                MONGO_CONNECT_RETRIES,
            )
        ):

            try:

                self.client = MongoClient(
                    MONGO_URI,
                    serverSelectionTimeoutMS=
                    MONGO_SERVER_SELECTION_TIMEOUT_MS,
                )

                self.client.admin.command(
                    "ping"
                )

                self.users = self.client[
                    MONGO_DB_NAME
                ]["users"]

                self.users.create_index(
                    [
                        (
                            "username",
                            ASCENDING,
                        )
                    ],
                    unique=True,
                )

                return

            except PyMongoError as exc:

                last_error = exc

                if (
                    attempt + 1
                    < MONGO_CONNECT_RETRIES
                ):
                    time.sleep(0.75)

        raise RuntimeError(
            f"Could not connect to MongoDB: "
            f"{last_error}"
        )

    def authenticate(
        self,
        username: str,
        password: str,
    ) -> dict[str, Any] | None:

        username = _clean_username(
            username
        )

        if not username or not password:
            return None

        user = self.users.find_one(
            {
                "username": username
            }
        )

        if (
            not user
            or not _verify_password(
                password,
                user,
            )
        ):
            return None

        return user

    def create_user(
        self,
        username: str,
        password: str,
    ) -> dict[str, Any]:

        username = _clean_username(
            username
        )

        if not username:
            raise ValueError(
                "Username is required."
            )

        if not password:
            raise ValueError(
                "Password is required."
            )

        now = _utc_now()

        user = {
            "username": username,
            "role": "user",
            "created_at": now,
            "updated_at": now,
            **_password_fields(
                password
            ),
        }

        try:

            result = self.users.insert_one(
                user
            )

        except DuplicateKeyError as exc:

            raise ValueError(
                "That username already exists."
            ) from exc

        user["_id"] = result.inserted_id

        return user

    def list_users(
        self,
    ) -> list[dict[str, Any]]:

        users = self.users.find(
            {},
            {
                "password": 0,
                "password_hash": 0,
                "password_salt": 0,
                "password_iterations": 0,
            },
        ).sort(
            [
                (
                    "role",
                    ASCENDING,
                ),
                (
                    "username",
                    ASCENDING,
                ),
            ]
        )

        return [
            _public_user(user)
            for user in users
        ]


def _public_font_record(record: dict[str, Any]) -> dict[str, Any]:
    created_at = record.get("created_at")
    if isinstance(created_at, datetime):
        created_at = created_at.isoformat()

    weight = int(record.get("weight") or 400)
    italic = bool(record.get("italic"))

    return {
        "id": str(record.get("_id", "")),
        "filename": record.get("filename") or "font.ttf",
        "family": record.get("family") or "Unknown",
        "subfamily": record.get("subfamily") or "",
        "full_name": record.get("full_name") or record.get("family") or "Unknown",
        "postscript": record.get("postscript") or "",
        "weight": weight,
        "weight_label": weight_label(weight),
        "italic": italic,
        "extension": record.get("extension") or "",
        "size_bytes": int(record.get("size_bytes") or 0),
        "sha256": record.get("sha256") or "",
        "uploaded_by": record.get("uploaded_by_username") or "",
        "created_at": created_at,
    }


class FontLibraryStore:
    """Persistent shared TTF/OTF library backed by MongoDB GridFS.

    Font files are stored as GridFS blobs. Search/list metadata lives in the
    `font_library` collection. A SHA-256 unique index prevents duplicate files.
    """

    def __init__(self, client):
        if GridFS is None or ObjectId is None:
            raise RuntimeError(
                "MongoDB GridFS support is unavailable. Reinstall pymongo."
            )

        self.db = client[MONGO_DB_NAME]
        self.fonts = self.db["font_library"]
        self.fs = GridFS(self.db, collection="font_files")

        self.fonts.create_index([("sha256", ASCENDING)], unique=True)
        self.fonts.create_index([("family", ASCENDING), ("full_name", ASCENDING)])
        self.fonts.create_index([("created_at", ASCENDING)])

    @staticmethod
    def _sha256_path(path: Path) -> str:
        h = hashlib.sha256()
        with Path(path).open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def save_font(
        self,
        path: Path,
        candidate,
        user: UserContext,
        original_filename: str,
    ) -> tuple[dict[str, Any], bool]:
        path = Path(path)
        size = path.stat().st_size
        if size > MAX_FONT_BYTES:
            raise ValueError("Font file is too large to save in the font library.")

        digest = self._sha256_path(path)
        existing = self.fonts.find_one({"sha256": digest})
        if existing:
            return existing, False

        suffix = Path(original_filename).suffix.lower()
        if suffix not in {".ttf", ".otf"}:
            suffix = path.suffix.lower()
        if suffix not in {".ttf", ".otf"}:
            suffix = ".ttf"

        data = path.read_bytes()
        gridfs_id = self.fs.put(
            data,
            filename=Path(original_filename).name,
            metadata={
                "sha256": digest,
                "family": getattr(candidate, "family", None),
                "full_name": getattr(candidate, "full_name", None),
            },
        )

        record = {
            "sha256": digest,
            "gridfs_id": gridfs_id,
            "filename": Path(original_filename).name,
            "extension": suffix,
            "family": getattr(candidate, "family", None),
            "subfamily": getattr(candidate, "subfamily", None),
            "full_name": getattr(candidate, "full_name", None),
            "postscript": getattr(candidate, "postscript", None),
            "weight": int(getattr(candidate, "weight", None) or 400),
            "italic": bool(getattr(candidate, "italic", False)),
            "size_bytes": size,
            "uploaded_by_id": user.user_id,
            "uploaded_by_username": user.username,
            "created_at": _utc_now(),
        }

        try:
            result = self.fonts.insert_one(record)
            record["_id"] = result.inserted_id
            return record, True
        except DuplicateKeyError:
            try:
                self.fs.delete(gridfs_id)
            except Exception:
                pass
            existing = self.fonts.find_one({"sha256": digest})
            if existing:
                return existing, False
            raise
        except Exception:
            try:
                self.fs.delete(gridfs_id)
            except Exception:
                pass
            raise

    def list_fonts(self) -> list[dict[str, Any]]:
        docs = self.fonts.find({}).sort(
            [("family", ASCENDING), ("full_name", ASCENDING), ("created_at", ASCENDING)]
        )
        return [_public_font_record(doc) for doc in docs]

    def materialize_font(
        self,
        font_id: str,
        destination_dir: Path,
    ) -> tuple[Path, dict[str, Any]]:
        try:
            oid = ObjectId(str(font_id))
        except (InvalidId, TypeError, ValueError) as exc:
            raise ValueError("Invalid saved-font id.") from exc

        record = self.fonts.find_one({"_id": oid})
        if not record:
            raise ValueError("Saved font was not found in MongoDB.")

        try:
            grid_out = self.fs.get(record["gridfs_id"])
            data = grid_out.read()
        except Exception as exc:
            raise ValueError("Saved font file could not be read from MongoDB GridFS.") from exc

        if len(data) > MAX_FONT_BYTES:
            raise ValueError("Saved font exceeds the allowed font size.")

        expected = str(record.get("sha256") or "")
        actual = hashlib.sha256(data).hexdigest()
        if expected and not hmac.compare_digest(expected, actual):
            raise ValueError("Saved font failed database integrity verification.")

        suffix = str(record.get("extension") or "").lower()
        if suffix not in {".ttf", ".otf"}:
            suffix = ".ttf"

        destination_dir = Path(destination_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = destination_dir / f"dbfont_{str(oid)}{suffix}"
        target.write_bytes(data)
        return target, record

    def best_compatible_font(
        self,
        *,
        replacement: str,
        target_family: str,
        target_weight: int,
        target_italic: bool,
        destination_dir: Path,
        require_exact_face: bool = False,
        candidate_validator: Callable | None = None,
    ):
        """Return the best stored face that safely covers the required glyphs.

        Subset completion can request an exact face and provide a validator
        that compares the materialized font with the original embedded CFF.
        Rejected candidates are skipped so a later genuine match can still be
        selected.
        """
        if not replacement:
            raise ValueError("Enter replacement text before choosing a fallback font.")
        target_norm = normalize_font_name(target_family)
        ranked = []
        for record in self.fonts.find({}):
            names = [
                record.get("family"),
                record.get("full_name"),
                record.get("postscript"),
            ]
            name_scores = []
            for name in names:
                current = normalize_font_name(name)
                if not current:
                    continue
                if target_norm and current == target_norm:
                    name_scores.append(1.0)
                elif target_norm:
                    name_scores.append(
                        difflib.SequenceMatcher(None, target_norm, current).ratio()
                    )
                else:
                    name_scores.append(0.0)
            similarity = max(name_scores or [0.0])
            weight_delta = abs(int(record.get("weight") or 400) - int(target_weight))
            italic_penalty = 0 if bool(record.get("italic")) == bool(target_italic) else 600
            score = (1.0 - similarity) * 1000.0 + weight_delta + italic_penalty
            ranked.append((score, record, similarity))

        ranked.sort(key=lambda item: (item[0], str(item[1].get("_id", ""))))
        missing_reports = []
        rejected_face_reports = []
        for score, record, similarity in ranked:
            candidate_weight = int(record.get("weight") or 400)
            requested_weight = int(target_weight)
            weight_delta = abs(candidate_weight - requested_weight)
            italic_match = bool(record.get("italic")) == bool(target_italic)
            family_exact = float(similarity) >= 0.995

            if require_exact_face and (
                not family_exact
                or weight_delta > 100
                or not italic_match
            ):
                rejected_face_reports.append(
                    (
                        record.get("full_name") or record.get("family") or "Unknown font",
                        (
                            f"family match={family_exact}, target weight={requested_weight}, "
                            f"candidate weight={candidate_weight}, style match={italic_match}"
                        ),
                    )
                )
                continue

            path, materialized = self.materialize_font(
                str(record["_id"]),
                destination_dir,
            )
            glyphs_ok, missing = font_path_has_text(str(path), replacement)
            if glyphs_ok:
                score_details = {
                    "score": float(score),
                    "family_similarity": float(similarity),
                    "weight_delta": weight_delta,
                    "target_weight": requested_weight,
                    "candidate_weight": candidate_weight,
                    "italic_match": italic_match,
                }
                if candidate_validator is not None:
                    accepted, reason = candidate_validator(
                        path,
                        materialized,
                        score_details,
                    )
                    if not accepted:
                        rejected_face_reports.append(
                            (
                                record.get("full_name")
                                or record.get("family")
                                or "Unknown font",
                                str(reason or "outline fingerprint mismatch"),
                            )
                        )
                        continue
                return path, materialized, score_details
            if missing:
                missing_reports.append((record.get("full_name") or record.get("family"), missing))

        preview = "; ".join(
            f"{name}: {' '.join(repr(ch) for ch in missing[:6])}"
            for name, missing in missing_reports[:3]
        )
        rejected_preview = "; ".join(
            f"{name}: {reason}"
            for name, reason in rejected_face_reports[:3]
        )
        if require_exact_face:
            raise ValueError(
                "No safe exact-family, weight/style and outline-compatible full font "
                f"in the database contains the required glyphs {replacement!r}. "
                "The original PDF was not changed. Upload the exact full font face; "
                "a neighboring weight will not be inserted into the original subset."
                + (f" Rejected candidates: {rejected_preview}." if rejected_preview else "")
                + (f" Missing-glyph checks: {preview}." if preview else "")
            )
        raise ValueError(
            "No saved MongoDB font contains and safely matches every required glyph."
            + (f" Rejected candidates: {rejected_preview}." if rejected_preview else "")
            + (f" Checked examples: {preview}" if preview else "")
        )


_auth_store: AuthStore | None = None

_auth_store_lock = threading.RLock()

_font_store: FontLibraryStore | None = None
_font_store_lock = threading.RLock()

_auth_sessions: dict[
    str,
    dict[str, Any],
] = {}

_auth_sessions_lock = threading.RLock()


def _get_auth_store() -> AuthStore:

    global _auth_store

    with _auth_store_lock:

        if _auth_store is None:

            try:
                _auth_store = AuthStore()

            except RuntimeError as exc:

                raise HTTPException(
                    status_code=503,
                    detail=str(exc),
                ) from exc

        return _auth_store


def _get_font_store() -> FontLibraryStore:
    global _font_store

    with _font_store_lock:
        if _font_store is None:
            auth_store = _get_auth_store()
            try:
                _font_store = FontLibraryStore(auth_store.client)
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"Font library is unavailable: {exc}",
                ) from exc
        return _font_store


def _create_auth_session(
    user: dict[str, Any],
) -> str:

    token = secrets.token_urlsafe(
        32
    )

    with _auth_sessions_lock:

        _auth_sessions[token] = {
            "user_id": str(
                user["_id"]
            ),
            "username": user[
                "username"
            ],
            "role": _clean_role(
                user.get("role")
            ),
            "last_access": time.time(),
        }

    return token


def _destroy_auth_session(
    token: str | None,
):

    if not token:
        return

    with _auth_sessions_lock:
        _auth_sessions.pop(
            token,
            None,
        )


def _require_user(
    request: Request,
) -> UserContext:

    token = request.cookies.get(
        AUTH_COOKIE_NAME
    )

    if not token:

        raise HTTPException(
            status_code=401,
            detail="Please log in.",
        )

    with _auth_sessions_lock:

        session = _auth_sessions.get(
            token
        )

        if not session:

            raise HTTPException(
                status_code=401,
                detail="Please log in.",
            )

        if (
            time.time()
            - session["last_access"]
            > AUTH_SESSION_TTL_SECONDS
        ):

            _auth_sessions.pop(
                token,
                None,
            )

            raise HTTPException(
                status_code=401,
                detail="Please log in again.",
            )

        session["last_access"] = (
            time.time()
        )

    return UserContext(
        user_id=session[
            "user_id"
        ],
        username=session[
            "username"
        ],
        role=session.get(
            "role",
            "user",
        ),
    )


def _require_admin(
    user: UserContext = Depends(
        _require_user
    ),
) -> UserContext:

    if not user.is_admin:

        raise HTTPException(
            status_code=403,
            detail="Admin access is required.",
        )

    return user


class WebEditorSession:

    def __init__(
        self,
        source_path: Path,
        original_name: str,
        session_dir: Path,
        owner_id: str,
        owner_username: str,
    ):

        self.source_path = Path(
            source_path
        )

        self.original_name = (
            original_name
        )

        self.session_dir = (
            session_dir
        )

        self.owner_id = owner_id

        self.owner_username = (
            owner_username
        )

        self.lock = threading.RLock()

        self.last_access = (
            time.time()
        )

        self.doc = None

        self.page_index = 0

        self.zoom = 1.35

        self.engine = PdfEngine()

        self.font_resolver = (
            FontResolver()
        )

        # IMPORTANT:
        # This resolver is separate.
        # It will only be called from
        # "Search Exact Font with AI".
        self.online_font_resolver = (
            OnlineFontResolver()
        )

        self.docx_index = (
            DocxReferenceIndex()
        )

        self.spans = []

        self.selected_span = None

        self.selected_line_spans = []

        self.selected_row_info = None

        self.selected_docx_ref = None

        self.current_resolution = None

        # Explicit, replacement-bound fallback selected by the user. It is
        # never reused when the replacement string or selection changes.
        self.approved_fallback = None

        self.undo_stack: list[
            tuple[bytes, Any]
        ] = []

        self.edit_session = None

        self.status = (
            "Open a PDF to begin."
        )

        self.match_status = (
            "Select text to begin."
        )

        self.match_tone = "neutral"

        self.docx_label = (
            "DOCX reference: not loaded"
        )

        self.last_output: Path | None = (
            None
        )

        self.last_audit: Path | None = (
            None
        )

        self.last_validation: (
            Path | None
        ) = None

        self._open()

    def touch(self):
        self.last_access = time.time()

    def _open(self):

        self.edit_session = (
            CleanEditSession(
                str(
                    self.source_path
                )
            )
        )

        self.doc = pymupdf.open(
            str(
                self.source_path
            )
        )

        if self.doc.needs_pass:

            self.doc.close()

            self.doc = None

            raise ValueError(
                "This version does not include "
                "password entry for protected PDFs."
            )

        self.page_index = 0

        self.undo_stack.clear()

        self.clear_selection()

        self.refresh_spans()

        self.status = (
            f"Loaded {self.original_name}. "
            "The source PDF will never be overwritten."
        )

    def close(self):

        with self.lock:

            try:

                if self.doc:
                    self.doc.close()

            except Exception:
                pass

            self.doc = None

    @property
    def page_count(self) -> int:

        return (
            len(self.doc)
            if self.doc
            else 0
        )

    def refresh_spans(self):

        if not self.doc:

            self.spans = []

            return

        page = self.doc[
            self.page_index
        ]

        self.spans = (
            self.engine.extract_spans(
                page
            )
        )

    @staticmethod
    def _span_order_key(span):
        return (
            float(span.origin[1]),
            float(span.bbox[0]),
            int(span.block_index),
            int(span.line_index),
            int(span.span_index),
        )

    @staticmethod
    def _translated_span(span, *, text=None, bbox=None, origin=None):
        """Return a style-identical cached span at its validated new geometry."""
        return replace(
            span,
            text=span.text if text is None else str(text),
            bbox=tuple(float(value) for value in (bbox or span.bbox)),
            origin=tuple(float(value) for value in (origin or span.origin)),
        )

    def _update_spans_after_line_delete(
        self,
        before_spans,
        selected_spans,
        shifted_items,
        moved_lines,
    ):
        """Update the browser hit-test index without re-parsing copied Forms.

        ``show_pdf_page`` deliberately preserves the source PDF's exact font
        resources, but its clipped full-page Form wrappers are expensive for
        PyMuPDF's ``get_text('dict')`` traversal.  The engine has already
        serialized and validated every destination line, so derive the new
        selectable spans from that validated transaction instead of scanning
        the same full-page display list once per moved line.
        """
        selected_ids = {id(span) for span in (selected_spans or [])}
        groups = {}
        for item in shifted_items or []:
            span = item.get("span")
            if span is None:
                continue
            key = tuple(
                item.get("line_key")
                or (int(span.block_index), int(span.line_index))
            )
            groups.setdefault(key, []).append(item)

        actual_records = [dict(item) for item in (moved_lines or [])]
        unused_records = set(range(len(actual_records)))
        replacements = {}

        def bbox_distance(first, second):
            return sum(
                abs(float(a) - float(b))
                for a, b in zip(first or (), second or ())
            )

        def normalized(value):
            return " ".join(str(value or "").split())

        for items in groups.values():
            items.sort(
                key=lambda item: (
                    float(item["span"].bbox[0]),
                    int(item["span"].span_index),
                )
            )
            final_bbox = (
                min(float(item["final_bbox"][0]) for item in items),
                min(float(item["final_bbox"][1]) for item in items),
                max(float(item["final_bbox"][2]) for item in items),
                max(float(item["final_bbox"][3]) for item in items),
            )
            record_index = None
            if unused_records:
                record_index = min(
                    unused_records,
                    key=lambda index: bbox_distance(
                        actual_records[index].get("bbox"),
                        final_bbox,
                    ),
                )
                unused_records.remove(record_index)

            actual_text = (
                str(actual_records[record_index].get("text") or "")
                if record_index is not None
                else ""
            )
            original_text = self.engine._line_text(
                [item["span"] for item in items]
            )
            planned_text = " ".join(
                str(item.get("text") or "").strip()
                for item in items
                if str(item.get("text") or "").strip()
            ).strip()

            use_original = (
                actual_text
                and normalized(actual_text) == normalized(original_text)
                and normalized(actual_text) != normalized(planned_text)
            )

            for item in items:
                span = item["span"]
                final_text = span.text if use_original else item.get("text", span.text)
                if len(items) == 1 and actual_text:
                    final_text = actual_text
                replacements[id(span)] = self._translated_span(
                    span,
                    text=final_text,
                    bbox=item.get("final_bbox"),
                    origin=item.get("origin"),
                )

        updated = []
        for span in before_spans or []:
            if id(span) in selected_ids:
                continue
            updated.append(replacements.get(id(span), span))
        self.spans = sorted(updated, key=self._span_order_key)

    def _update_spans_after_row_insert(
        self,
        before_spans,
        selected_spans,
        shifted_items,
        shift_dy,
    ):
        """Cache exact moved spans plus a style-identical clone of each cell."""
        replacements = {}
        for item in shifted_items or []:
            span = item.get("span")
            if span is None:
                continue
            replacements[id(span)] = self._translated_span(
                span,
                text=item.get("text", span.text),
                bbox=item.get("final_bbox"),
                origin=item.get("origin"),
            )

        updated = [
            replacements.get(id(span), span)
            for span in (before_spans or [])
        ]
        dy = float(shift_dy)
        for span in selected_spans or []:
            updated.append(
                self._translated_span(
                    span,
                    bbox=(
                        float(span.bbox[0]),
                        float(span.bbox[1]) + dy,
                        float(span.bbox[2]),
                        float(span.bbox[3]) + dy,
                    ),
                    origin=(
                        float(span.origin[0]),
                        float(span.origin[1]) + dy,
                    ),
                )
            )
        self.spans = sorted(updated, key=self._span_order_key)

    def clear_selection(self):

        self.selected_span = None

        self.selected_line_spans = []

        self.selected_row_info = None

        self.selected_docx_ref = None

        self.current_resolution = None

        self.approved_fallback = None

        self.match_status = (
            "Select text to begin."
        )

        self.match_tone = "neutral"

    def set_page(
        self,
        page_index: int,
    ):

        if not self.doc:

            raise ValueError(
                "No PDF is open."
            )

        page_index = max(
            0,
            min(
                int(page_index),
                len(self.doc) - 1,
            ),
        )

        self.page_index = (
            page_index
        )

        self.clear_selection()

        self.refresh_spans()

        self.status = (
            f"Page "
            f"{self.page_index + 1} "
            f"of {len(self.doc)}."
        )

    def render_png(
        self,
        zoom: float,
    ) -> bytes:

        if not self.doc:

            raise ValueError(
                "No PDF is open."
            )

        zoom = max(
            0.60,
            min(
                float(zoom),
                2.50,
            ),
        )

        self.zoom = zoom

        page = self.doc[
            self.page_index
        ]

        pix = page.get_pixmap(
            matrix=pymupdf.Matrix(
                zoom,
                zoom,
            ),
            alpha=False,
        )

        return pix.tobytes(
            "png"
        )

    def select_at(
        self,
        x: float,
        y: float,
    ):

        containing = []

        for span in self.spans:

            x0, y0, x1, y1 = (
                span.bbox
            )

            if (
                x0 <= x <= x1
                and y0 <= y <= y1
            ):

                area = max(
                    1.0,
                    (x1 - x0)
                    * (y1 - y0),
                )

                containing.append(
                    (
                        area,
                        span,
                    )
                )

        if containing:

            containing.sort(
                key=lambda item:
                item[0]
            )

            self.select_span(
                containing[0][1]
            )

            return

        closest = None

        closest_distance = 8.0

        for span in self.spans:

            x0, y0, x1, y1 = (
                span.bbox
            )

            cx = min(
                max(
                    x,
                    x0,
                ),
                x1,
            )

            cy = min(
                max(
                    y,
                    y0,
                ),
                y1,
            )

            distance = math.hypot(
                x - cx,
                y - cy,
            )

            if (
                distance
                < closest_distance
            ):

                closest_distance = (
                    distance
                )

                closest = span

        if closest:

            self.select_span(
                closest
            )

        else:

            self.clear_selection()

            self.status = (
                "No selectable PDF text "
                "detected there."
            )

    def select_span(
        self,
        span,
    ):

        self.approved_fallback = None
        self.selected_span = span
        self.selected_line_spans = []
        self.selected_row_info = None

        self.populate_docx_reference()

        # Normal PDF/font check only.
        # No AI is intentionally called here.
        self.resolve_selected_font(
            span.text
        )

        self.status = (
            "Text selected."
        )

    def select_full_line(self):
        if not self.doc or not self.selected_span:
            raise ValueError("Select text in the line first.")

        line_spans = self.engine.full_line_spans(
            self.spans,
            self.selected_span,
            page=self.doc[self.page_index],
        )
        if not line_spans:
            raise ValueError("The full text line could not be detected.")

        self.selected_line_spans = list(line_spans)
        self.selected_row_info = self._selected_table_row_info()
        self.current_resolution = None
        self.approved_fallback = None
        self.selected_docx_ref = None

        line_text = " ".join(
            span.text.strip()
            for span in self.selected_line_spans
            if span.text.strip()
        )
        self.match_status = (
            f"✓ FULL LINE SELECTED\n"
            f"{len(self.selected_line_spans)} text span(s) selected.\n"
            "Use 'Delete Full Line' to remove this line. For a detected "
            "multi-column table row, 'Add Full Row Below' clones every cell "
            "with its original PDF styling. Horizontal table "
            "rules move with their rows by default; enable the separator "
            "option only when that boundary should be removed."
        )
        self.match_tone = "success"
        self.status = f"Full line selected: {line_text[:120]}"

    def _selected_table_row_info(self):
        """Describe the selected row without running the structural planner.

        This stays intentionally cheap because ``state()`` is returned after
        most browser actions.  The actual insertion engine performs the full
        table/rule/overflow validation before changing the PDF.
        """
        selected = sorted(
            list(self.selected_line_spans or []),
            key=lambda span: (
                float(span.bbox[0]),
                float(span.bbox[1]),
                int(span.span_index),
            ),
        )
        if not selected:
            return {
                "eligible": False,
                "column_count": 0,
                "reason": "Select a full table row to enable row insertion.",
            }

        try:
            has_columns = self.engine._has_table_column_layout(selected)
        except Exception:
            has_columns = False

        groups = []
        for span in selected:
            if not groups:
                groups.append([span])
                continue

            previous = groups[-1][-1]
            typical_size = max(
                1.0,
                float(previous.size),
                float(span.size),
            )
            column_gap = max(24.0, typical_size * 3.0)
            gap = float(span.bbox[0]) - float(previous.bbox[2])
            if gap > column_gap:
                groups.append([span])
            else:
                groups[-1].append(span)

        column_count = len(groups) if has_columns else 0
        if column_count < 2:
            return {
                "eligible": False,
                "column_count": column_count,
                "reason": (
                    "The selected full line is not a safely detected "
                    "multi-column table row. Select a row with at least two "
                    "separated cells and a matching horizontal table rule."
                ),
            }

        return {
            "eligible": True,
            "column_count": int(column_count),
            "reason": "",
            "cell_text": [
                " ".join(
                    item.text.strip()
                    for item in group
                    if item.text.strip()
                )
                for group in groups
            ],
        }

    def add_selected_row_below(self, *, position: str = "below"):
        if not self.doc or not self.selected_line_spans:
            raise ValueError("Select a full table row before adding a row.")
        if str(position or "below").lower() != "below":
            raise ValueError("This version can add a table row below the selected row only.")

        page = self.doc[self.page_index]
        line_spans = list(self.selected_line_spans)
        before_spans = list(self.spans)
        template_text = " ".join(
            span.text.strip()
            for span in line_spans
            if span.text.strip()
        )

        # Planning is read-only and runs before the Undo snapshot so rejected
        # non-table rows / page-overflow cases leave no session mutation.
        plan = self.engine.plan_insert_table_row(
            page,
            self.spans,
            line_spans,
            position="below",
        )
        shifted_items = list(plan.get("shifted_items") or [])
        shift_dy = float(plan.get("shift_dy") or 0.0)
        column_count = int(plan.get("column_count") or 0)
        if column_count < 2:
            raise ValueError(
                "The selected line is not a safely detected multi-column table row."
            )

        self._snapshot_for_structural_edit()
        try:
            if self.edit_session:
                self.edit_session._inline_line_reflow_forms(self.doc)
            result = self.engine.insert_table_row_with_reflow(
                page,
                line_spans,
                plan,
            )
            column_count = int(result.get("column_count") or column_count)

            if self.edit_session:
                page_number = self.page_index + 1
                source_bboxes = [
                    item["span"].bbox
                    for item in shifted_items
                    if item.get("span") is not None
                ]
                self.edit_session.shift_for_table_row_insert(
                    page_number,
                    source_bboxes,
                    shift_dy,
                )
                self.edit_session.record_table_row_insert_operation(
                    page_number=page_number,
                    template_text=(
                        result.get("template_text")
                        or plan.get("template_text")
                        or template_text
                    ),
                    inserted_bbox=(
                        result.get("inserted_bbox")
                        or plan.get("inserted_bbox")
                    ),
                    inserted_lines=result.get("inserted_lines") or [],
                    moved_lines=result.get("moved_lines") or [],
                    moved_table_rules=result.get("moved_table_rules") or [],
                    inserted_table_rules=result.get("inserted_table_rules") or [],
                    column_count=column_count,
                    position="below",
                )

            moved_count = int(
                result.get("shifted_count", result.get("moved_count", 0)) or 0
            )
            moved_rule_count = int(
                result.get(
                    "moved_table_rule_count",
                    len(result.get("moved_table_rules") or []),
                )
                or 0
            )
            inserted_rule_count = int(
                result.get(
                    "inserted_table_rule_count",
                    len(result.get("inserted_table_rules") or []),
                )
                or 0
            )

            self._update_spans_after_row_insert(
                before_spans,
                line_spans,
                shifted_items,
                shift_dy,
            )
            self.clear_selection()
            self.last_output = None
            self.last_audit = None
            self.last_validation = None
            self.status = (
                f"Added a {column_count}-column table row below the selected row."
            )
            self.match_status = (
                "✓ FULL TABLE ROW ADDED\n"
                f"Cloned {column_count} detected table column(s) from the "
                "selected row using its existing PDF content and font resources.\n"
                f"Moved {moved_count} later text span(s) and {moved_rule_count} "
                "existing table line(s) downward; inserted "
                f"{inserted_rule_count} separator line(s).\n"
                "Click each cloned cell and use the normal text replacement "
                "workflow to enter the new row values."
            )
            self.match_tone = "success"
            return result

        except Exception:
            self._rollback_last_snapshot()
            raise

    def _snapshot_pdf_bytes(self):
        """Serialize Undo state without compacting or renumbering PDF objects."""
        try:
            return self.doc.tobytes(
                garbage=0,
                clean=False,
                deflate=False,
                no_new_id=True,
                encryption=pymupdf.PDF_ENCRYPT_KEEP,
                preserve_metadata=True,
            )
        except TypeError:
            # Older bindings may not accept preserve_metadata, but they still
            # support no_new_id. Never fall back to a call that mutates /ID.
            return self.doc.tobytes(
                garbage=0,
                clean=False,
                deflate=False,
                no_new_id=True,
            )

    def _snapshot_for_structural_edit(self):
        raw = self._snapshot_pdf_bytes()
        structural_state = {
            "edits": copy.deepcopy(
                self.edit_session.edits if self.edit_session else []
            ),
            "page_index": int(self.page_index),
            # Reuse the already verified hit-test index on Undo / rollback.
            # A snapshot may itself contain clipped row-copy Forms, and
            # rebuilding a full text dictionary through them is precisely the
            # slow path structural edits avoid.
            "spans": copy.deepcopy(self.spans),
        }
        self.undo_stack.append((raw, structural_state))

    def add_blank_page_after_current(self):
        if not self.doc:
            raise ValueError("No PDF is open.")

        self._snapshot_for_structural_edit()
        try:
            current_page = self.doc[self.page_index]
            rect = current_page.rect
            insert_index = self.page_index + 1

            if insert_index >= len(self.doc):
                self.doc.new_page(
                    pno=-1,
                    width=float(rect.width),
                    height=float(rect.height),
                )
            else:
                self.doc.new_page(
                    pno=insert_index,
                    width=float(rect.width),
                    height=float(rect.height),
                )

            inserted_page_number = insert_index + 1
            if self.edit_session:
                self.edit_session.shift_for_page_insert(inserted_page_number)
                self.edit_session.record_page_operation(
                    action="add",
                    page_number=inserted_page_number,
                    width=float(rect.width),
                    height=float(rect.height),
                )

            self.page_index = insert_index
            self.clear_selection()
            self.refresh_spans()
            self.last_output = None
            self.last_audit = None
            self.last_validation = None
            self.status = (
                f"Added blank page {self.page_index + 1} "
                f"with size {rect.width:.2f} × {rect.height:.2f} pt."
            )
            self.match_status = "Blank page added. Use Undo Last to restore the previous PDF."
            self.match_tone = "success"
        except Exception:
            self._rollback_last_snapshot()
            raise

    def delete_table_line_at(self, x: float, y: float):
        """Delete one safe horizontal table rule nearest the clicked point."""
        if not self.doc:
            raise ValueError("No PDF is open.")

        click_x = float(x)
        click_y = float(y)

        # Resolve a miss or ambiguous click before creating an Undo snapshot.
        # A harmless retry must not clear the current text selection or alter
        # any server-side session state.
        preview_rule = self.engine.nearest_horizontal_table_rule(
            self.doc[self.page_index],
            click_x,
            click_y,
            tolerance=6.0,
        )
        if preview_rule is None:
            raise ValueError(
                "No independently stroked horizontal table line was found "
                "within 6.0 PDF points of that position. Zoom in and click "
                "directly on the line."
            )

        # Standalone rule deletion is structural and independently undoable.
        # Keep one snapshot per click so several lines can be removed and then
        # undone one by one.
        self._snapshot_for_structural_edit()

        try:
            # A preceding full-line reflow temporarily uses clipped Form
            # XObjects for exact text copies. PyMuPDF's graphics redaction
            # rewrites those wrappers under generic names, which prevents the
            # normal final-export flattener from collecting them and can change
            # the xref profile. Flatten the verified wrappers *before* this
            # graphics-only edit; Undo still restores the pre-flatten snapshot.
            preflattened_forms = []
            if self.edit_session:
                preflattened_forms = (
                    self.edit_session._inline_line_reflow_forms(self.doc)
                )

            page = self.doc[self.page_index]
            result = self.engine.delete_horizontal_table_rule_at(
                page,
                click_x,
                click_y,
                tolerance=6.0,
            )
            result["preflattened_line_reflow_forms"] = preflattened_forms

            rule_bbox = result.get("bbox")
            rule = result.get("rule")

            if self.edit_session:
                self.edit_session.record_table_rule_delete_operation(
                    page_number=self.page_index + 1,
                    bbox=rule_bbox,
                    rule=rule,
                    click=(click_x, click_y),
                )

            # Graphics-only deletion cannot change the searchable text index.
            # Reusing the already validated spans also avoids an expensive
            # recursive text-dictionary scan after earlier line reflows.
            self.clear_selection()
            self.last_output = None
            self.last_audit = None
            self.last_validation = None

            deleted_y = click_y
            if rule_bbox is not None:
                try:
                    deleted_y = (
                        float(rule_bbox[1]) + float(rule_bbox[3])
                    ) / 2.0
                except (IndexError, TypeError, ValueError):
                    pass

            self.status = (
                "Deleted one horizontal table line at "
                f"y={deleted_y:.2f} pt on page {self.page_index + 1}."
            )
            self.match_status = (
                "✓ TABLE LINE DELETED\n"
                "Only the safe horizontal rule nearest your click was removed.\n"
                "Text and unrelated page graphics were not changed.\n"
                "Table-line delete mode remains active in the browser so you "
                "can click another line, or use Undo Last."
            )
            self.match_tone = "success"
            return result

        except Exception:
            self._rollback_last_snapshot()
            raise

    def delete_current_page(self):
        if not self.doc:
            raise ValueError("No PDF is open.")
        if len(self.doc) <= 1:
            raise ValueError("The PDF must keep at least one page.")

        self._snapshot_for_structural_edit()
        try:
            deleted_index = self.page_index
            deleted_page_number = deleted_index + 1
            rect = self.doc[deleted_index].rect

            self.doc.delete_page(deleted_index)

            if self.edit_session:
                self.edit_session.shift_for_page_delete(deleted_page_number)
                self.edit_session.record_page_operation(
                    action="delete",
                    page_number=deleted_page_number,
                    width=float(rect.width),
                    height=float(rect.height),
                )

            self.page_index = min(deleted_index, len(self.doc) - 1)
            self.clear_selection()
            self.refresh_spans()
            self.last_output = None
            self.last_audit = None
            self.last_validation = None
            self.status = f"Deleted page {deleted_page_number}."
            self.match_status = "Page deleted. Use Undo Last if you want to restore it."
            self.match_tone = "success"
        except Exception:
            self._rollback_last_snapshot()
            raise

    def delete_selected_line(self, *, remove_table_rule: bool = False):
        if not self.doc or not self.selected_line_spans:
            raise ValueError("Select a full line before deleting it.")

        page = self.doc[self.page_index]
        line_spans = list(self.selected_line_spans)
        before_spans = list(self.spans)
        line_text = " ".join(
            span.text.strip()
            for span in line_spans
            if span.text.strip()
        )

        # Build the visual upward-reflow plan BEFORE changing the PDF.
        #
        # Important:
        # We DO NOT ask FontResolver to re-create the following lines. Those
        # lines already exist in the PDF with the exact correct font/resources.
        # PdfEngine copies their original vector/searchable PDF content upward
        # from a pre-edit snapshot instead. This avoids false failures from
        # subset fonts, custom encodings, and missing "full" font files.
        plan = self.engine.plan_delete_line_reflow(
            self.spans,
            line_spans,
            page.rect,
            page=page,
        )

        prepared_shifted_items = [
            dict(item)
            for item in plan.get("shifted_items", [])
        ]

        table_rule_plan = self.engine.plan_table_rule_reflow(
            page,
            line_spans,
            prepared_shifted_items,
            plan.get("shift_dy", 0.0),
            remove_deleted_row_rule=bool(remove_table_rule),
        )

        # This operation changes PDF content and earlier edit locations,
        # so keep a complete Undo / rollback snapshot.
        self._snapshot_for_structural_edit()

        try:
            if self.edit_session:
                self.edit_session._inline_line_reflow_forms(self.doc)
            result = self.engine.delete_full_line_with_reflow(
                page,
                line_spans,
                prepared_shifted_items,
                table_rule_plan=table_rule_plan,
            )

            if self.edit_session:
                page_number = self.page_index + 1
                deleted_bbox = (
                    result.get("deleted_bbox")
                    or plan.get("selected_bbox")
                    or self.engine.line_bbox(line_spans)
                )

                # Any earlier edit occupying the deleted line is no longer an
                # active final-state text edit.
                self.edit_session.deactivate_for_line_delete(
                    page_number,
                    deleted_bbox,
                )

                # Earlier edits located in lines that moved upward must follow
                # those new final positions for Save As validation.
                self.edit_session.shift_for_line_reflow(
                    page_number,
                    [item["span"].bbox for item in prepared_shifted_items],
                    plan.get("shift_dy", 0.0),
                )

                # Record ONE structural line-reflow operation instead of one
                # deletion record per span. This prevents numbered prefixes such
                # as "3." / "4." from creating false old-text validation errors.
                self.edit_session.record_line_reflow_operation(
                    page_number=page_number,
                    deleted_text=(
                        result.get("deleted_text")
                        or line_text
                    ),
                    deleted_bbox=deleted_bbox,
                    moved_lines=result.get("moved_lines", []),
                    moved_table_rule_count=int(
                        result.get(
                            "moved_table_rule_count",
                            result.get(
                                "moved_rule_count",
                                len(result.get("moved_table_rules", []) or []),
                            ),
                        )
                        or 0
                    ),
                    removed_table_rule_count=int(
                        result.get(
                            "removed_table_rule_count",
                            result.get(
                                "removed_rule_count",
                                len(result.get("removed_table_rules", []) or []),
                            ),
                        )
                        or 0
                    ),
                    remove_table_rule=bool(remove_table_rule),
                )

            shifted_count = int(result.get("shifted_count", 0) or 0)
            renumbered_count = int(result.get("renumbered_count", 0) or 0)
            moved_rule_count = int(
                result.get(
                    "moved_table_rule_count",
                    result.get(
                        "moved_rule_count",
                        len(result.get("moved_table_rules", []) or []),
                    ),
                )
                or 0
            )
            removed_rule_count = int(
                result.get(
                    "removed_table_rule_count",
                    result.get(
                        "removed_rule_count",
                        len(result.get("removed_table_rules", []) or []),
                    ),
                )
                or 0
            )

            self._update_spans_after_line_delete(
                before_spans,
                line_spans,
                prepared_shifted_items,
                result.get("moved_lines", []),
            )
            self.clear_selection()
            self.last_output = None
            self.last_audit = None
            self.last_validation = None

            self.status = f"Deleted full text line: {line_text[:120]}"

            if shifted_count:
                extra = (
                    f"Moved {shifted_count} text span(s) upward using the "
                    "original PDF content."
                )
                if renumbered_count:
                    extra += (
                        f" Renumbered {renumbered_count} numbered-list "
                        "prefix span(s)."
                    )
            else:
                extra = "No later text in the same visual content flow needed to move."

            if moved_rule_count:
                extra += (
                    f" Moved {moved_rule_count} horizontal table line(s) upward "
                    "with the remaining rows."
                )
            if removed_rule_count:
                extra += (
                    f" Removed {removed_rule_count} separator line(s) belonging "
                    "to the deleted row."
                )

            self.match_status = (
                "✓ FULL LINE DELETED AND REFLOWED\n"
                f"Removed {len(line_spans)} selected span(s).\n"
                f"{extra}\n"
                "No font substitution was used for moved lines.\n"
                "The resulting searchable text layer was validated immediately."
            )
            self.match_tone = "success"

            return result.get("deleted_bbox")

        except Exception:
            self._rollback_last_snapshot()
            raise

    def populate_docx_reference(
        self,
    ):

        self.selected_docx_ref = None

        if not self.selected_span:
            return

        if self.docx_index.loaded:

            x0, y0, _, _ = (
                self.selected_span.bbox
            )

            self.selected_docx_ref = (
                self.docx_index.find(
                    self.selected_span.text,
                    pdf_size_pt=
                    self.selected_span.size,
                    pdf_page_number=
                    self.page_index + 1,
                    pdf_x_pt=x0,
                    pdf_y_pt=y0,
                )
            )

    def load_docx(
        self,
        path: Path,
        display_name: str,
    ) -> int:

        count = self.docx_index.load(
            str(path)
        )

        self.docx_label = (
            f"DOCX reference: "
            f"{display_name} "
            f"({count} formatted runs indexed)"
        )

        if self.selected_span:

            self.populate_docx_reference()

            self.resolve_selected_font(
                self.selected_span.text
            )

        self.status = (
            self.docx_label
        )

        return count

    def load_manual_font(
        self,
        path: Path,
    ):

        if not self.selected_span:

            raise ValueError(
                "Select PDF text before "
                "loading a font."
            )

        candidate = (
            self.font_resolver
            .add_manual_font(
                str(path)
            )
        )

        self.current_resolution = None
        self.approved_fallback = None

        self.match_status = (
            "Exact font loaded.\n"
            "Waiting to verify the "
            "replacement text..."
        )

        self.match_tone = (
            "warning"
        )

        self.status = (
            f"Loaded exact font: "
            f"{candidate.display_name}"
        )

        return candidate

    def search_selected_font_with_ai(
        self,
        replacement: str,
    ):

        """
        This function is the ONLY explicit
        OpenAI font-search action from the UI.
        """

        if (
            not self.doc
            or not self.selected_span
        ):

            raise ValueError(
                "Select PDF text before "
                "searching for a font."
            )

        replacement = (
            ""
            if replacement is None
            else str(replacement)
        )

        self.approved_fallback = None

        if (
            self.selected_docx_ref
            and self.selected_docx_ref.family
        ):

            family = (
                self.selected_docx_ref.family
            )

            target_weight = (
                700
                if (
                    self.selected_docx_ref.bold
                    is True
                )
                else 400
                if (
                    self.selected_docx_ref.bold
                    is False
                )
                else weight_from_flags(
                    self.selected_span.flags
                )
            )

            target_italic = (
                bool(
                    self.selected_docx_ref.italic
                )
                if (
                    self.selected_docx_ref.italic
                    is not None
                )
                else italic_from_flags(
                    self.selected_span.flags
                )
            )

        else:

            family = (
                humanize_pdf_font_name(
                    self.selected_span.font
                )
            )

            target_weight = (
                weight_from_flags(
                    self.selected_span.flags
                )
            )

            target_italic = (
                italic_from_flags(
                    self.selected_span.flags
                )
            )

        family = (
            family
            or strip_subset_prefix(
                self.selected_span.font
            )
            or ""
        ).strip()

        if not family:

            raise ValueError(
                "The selected PDF text "
                "does not expose a reliable "
                "font family name."
            )

        self.status = (
            f"Searching for exact font "
            f"'{family}' with AI..."
        )

        self.match_status = (
            "Searching with AI...\n"
            f"Family: {family}\n"
            f"Replacement: "
            f"{replacement!r}"
        )

        self.match_tone = (
            "warning"
        )

        result, note = (
            self.online_font_resolver
            .find_exact_with_ai(
                family,
                target_weight,
                target_italic,
                replacement,
            )
        )

        if (
            not result
            or not result.path
        ):

            self.current_resolution = None

            self.match_status = (
                "✗ AI FONT SEARCH DID NOT "
                "FIND A VERIFIED FONT\n"
                f"Replacement checked: "
                f"{replacement!r}\n"
                f"Requested family: "
                f"{family}\n\n"
                f"{note or 'No verified downloadable font candidate was found.'}"
            )

            self.match_tone = "error"

            self.status = (
                "AI font search finished "
                "without a verified exact font."
            )

            return None

        candidate = (
            self.font_resolver
            .add_manual_font(
                result.path
            )
        )

        # AI only searches/downloads.
        # Existing FontResolver performs
        # final verification.
        resolution = (
            self.resolve_selected_font(
                replacement
            )
        )

        if (
            resolution
            and resolution.ok
            and resolution.candidate
        ):

            self.match_status += (
                "\n\nAI search source:\n"
                + (
                    note
                    or (
                        "Verified download "
                        f"from {result.provider}."
                    )
                )
            )

            self.status = (
                "AI search found and "
                "verified exact font: "
                f"{candidate.display_name}"
            )

        else:

            self.match_status += (
                "\n\nAI search found a "
                "downloadable candidate, "
                "but the existing PDF "
                "exact-match verification "
                "did not accept it.\n"
                + (note or "")
            )

            self.match_tone = "error"

            self.status = (
                "AI font candidate was "
                "rejected by exact PDF "
                "verification."
            )

        return resolution

    def resolve_selected_font(
        self,
        replacement: str,
    ):

        if (
            not self.doc
            or not self.selected_span
        ):

            self.current_resolution = None

            self.match_status = (
                "✗ Select PDF text first."
            )

            self.match_tone = "error"

            return None

        self.approved_fallback = None

        if replacement is None:
            replacement = ""

        page = self.doc[
            self.page_index
        ]

        # NORMAL resolver only.
        # This action itself does not
        # call search_selected_font_with_ai().
        resolution = (
            self.font_resolver.resolve(
                self.doc,
                page,
                self.selected_span,
                replacement,
                self.selected_docx_ref,
            )
        )

        # A manually loaded, installed, or downloaded full font can carry the
        # expected family/weight metadata while still being a different face.
        # When that font would donate glyph programs to the original Type1C
        # subset, verify its outlines before exposing the resolution as ready.
        # Original PDF resources that need no external donor have no attached
        # subset-extension source and intentionally bypass this check.
        if (
            resolution
            and resolution.ok
            and resolution.candidate
            and resolution.exact_family
            and resolution.exact_weight
            and resolution.exact_style
            and resolution.glyphs_ok
        ):
            candidate = resolution.candidate
            has_external_donor = bool(
                getattr(candidate, "subset_extension_font_path", None)
                or getattr(candidate, "subset_extension_font_bytes", None)
            )
            if getattr(candidate, "extend_original_cff", False) and has_external_donor:
                compatibility = verify_donor_compatibility(
                    self.doc,
                    candidate,
                    self.selected_span.text,
                )
                if not compatibility.get("ok"):
                    resolution = FontResolution(
                        ok=False,
                        reason=(
                            "The proposed full-font donor was rejected before editing "
                            "because its glyph outlines do not match the selected PDF "
                            "font closely enough. "
                            + str(
                                compatibility.get("reason")
                                or "Outline fingerprint verification failed."
                            )
                            + " The original PDF was not changed. Upload the genuine "
                            "full font face."
                        ),
                        missing_chars=list(resolution.missing_chars or []),
                    )
                else:
                    verification_note = str(compatibility.get("reason") or "").strip()
                    if verification_note:
                        resolution.source_note = (
                            str(resolution.source_note or "").rstrip()
                            + " Donor outline verification: "
                            + verification_note
                        ).strip()

        self.current_resolution = (
            resolution
        )

        if (
            not resolution
            or not resolution.ok
            or not resolution.candidate
        ):

            reason = (
                resolution.reason
                if resolution
                else (
                    "No font resolution result "
                    "was returned."
                )
            )

            missing = ""

            if (
                resolution
                and resolution.missing_chars
            ):

                missing = (
                    "\nMissing glyphs: "
                    + " ".join(
                        repr(ch)
                        for ch in
                        resolution.missing_chars[
                            :15
                        ]
                    )
                )

            self.match_status = (
                "✗ FONT NOT READY\n"
                f"Replacement checked: "
                f"{replacement!r}\n\n"
                f"{reason}"
                f"{missing}"
            )

            self.match_tone = "error"

            return resolution

        exact_family = bool(
            resolution.exact_family
        )

        exact_weight = bool(
            resolution.exact_weight
        )

        exact_style = bool(
            resolution.exact_style
        )

        glyphs_ok = bool(
            resolution.glyphs_ok
        )

        strict_match = (
            exact_family
            and exact_weight
            and exact_style
            and glyphs_ok
        )

        if not strict_match:

            failed = []

            if not glyphs_ok:
                failed.append(
                    "glyph coverage"
                )

            if not exact_family:
                failed.append(
                    "font family/program"
                )

            if not exact_weight:
                failed.append(
                    "font weight"
                )

            if not exact_style:
                failed.append(
                    "italic/style"
                )

            self.match_status = (
                "✗ FONT NOT READY\n"
                f"Replacement checked: "
                f"{replacement!r}\n\n"
                "Exact verification failed "
                "for: "
                + ", ".join(failed)
            )

            if resolution.source_note:

                self.match_status += (
                    "\n\nResolver information:\n"
                    + resolution.source_note
                )

            self.match_tone = "error"

            return resolution

        checks = [
            "✓ font available",
            (
                "✓ exact PDF size: "
                f"{self.selected_span.size:.4f} pt"
            ),
            "✓ color preserved",
            "✓ baseline preserved",
            "✓ family/program match",
            "✓ weight match",
            "✓ italic/style match",
            "✓ replacement glyphs available",
        ]

        self.match_status = (
            "\n".join(checks)
        )

        if resolution.source_note:

            self.match_status += (
                "\n\nSource verification:\n"
                + resolution.source_note
            )

        self.match_tone = "success"

        return resolution

    def mark_replacement_changed(
        self,
    ):

        if self.selected_span:

            self.current_resolution = None

            self.approved_fallback = None

            self.match_status = (
                "Text changed. Click "
                "'Re-check Font Match' "
                "before replacing.\n"
                "The previous font result "
                "will not be reused."
            )

            self.match_tone = (
                "warning"
            )

    def target_font_profile(self):
        if not self.selected_span:
            raise ValueError("Select PDF text first.")
        if self.selected_docx_ref and self.selected_docx_ref.family:
            family = self.selected_docx_ref.family
            weight = (
                700 if self.selected_docx_ref.bold is True
                else 400 if self.selected_docx_ref.bold is False
                else weight_from_flags(self.selected_span.flags)
            )
            italic = (
                bool(self.selected_docx_ref.italic)
                if self.selected_docx_ref.italic is not None
                else italic_from_flags(self.selected_span.flags)
            )
        else:
            family = (
                humanize_pdf_font_name(self.selected_span.font)
                or strip_subset_prefix(self.selected_span.font)
                or ""
            )
            weight = weight_from_flags(self.selected_span.flags)
            italic = italic_from_flags(self.selected_span.flags)
        return family, int(weight), bool(italic)

    def find_glyph_elsewhere_in_pdf(self, replacement: str):
        if not self.doc or not self.selected_span:
            raise ValueError("Select PDF text before searching its embedded fonts.")
        page = self.doc[self.page_index]
        replacement = str(replacement or "")
        candidate = None
        note = ""
        missing = []
        fallback_mode = "pdf_glyph_resource"
        adaptive_error = ""

        selected_font_record = self.font_resolver.find_page_font(
            page,
            self.selected_span,
        )
        selected_xref = int((selected_font_record or {}).get("xref", 0) or 0)
        selected_refname = str(
            (selected_font_record or {}).get("refname", "") or ""
        ).lstrip("/")
        precise_cff_missing = []
        if selected_xref > 0:
            _can_encode, precise_cff_missing, _widths = inspect_existing_cff(
                self.doc,
                selected_xref,
                replacement,
            )

        # A Type1C subset may already contain the outlines even when its current
        # one-byte Encoding / ToUnicode maps cannot emit the requested text.
        # Prefer that exact original resource and let the CFF writer allocate
        # only the missing byte mappings.
        candidate = self.font_resolver._original_cff_candidate_if_encodable(
            self.doc,
            page,
            self.selected_span,
            replacement,
        )
        if candidate:
            note = (
                f"The original PDF font /{candidate.pdf_refname} already contains "
                "every requested glyph outline. Only its unused character mappings "
                "will be completed; no font resource will be substituted."
            )
            missing = []
        else:
            # When the selected CFF is genuinely missing glyph programs, first
            # try to borrow those outlines from other embedded CFF resources.
            # The donor is not inserted as-is: shared target/donor glyphs are
            # measured, the required emboldening and geometry are calibrated,
            # and only the missing programs are appended to the original /F#.
            if precise_cff_missing and selected_xref > 0 and selected_refname:
                try:
                    missing_text = "".join(dict.fromkeys(precise_cff_missing))
                    plan = build_adaptive_pdf_glyph_plan(
                        self.doc,
                        selected_xref,
                        missing_text,
                        self.selected_span.text,
                    )
                    target_family, target_weight, target_italic = (
                        self.target_font_profile()
                    )
                    basefont = str(
                        (selected_font_record or {}).get("basefont", "") or ""
                    )
                    candidate = FontCandidate(
                        path=None,
                        family=target_family,
                        full_name=(
                            strip_subset_prefix(basefont)
                            or strip_subset_prefix(self.selected_span.font)
                        ),
                        postscript=(
                            strip_subset_prefix(basefont)
                            or strip_subset_prefix(self.selected_span.font)
                        ),
                        weight=target_weight,
                        italic=target_italic,
                        source="Original PDF Type1C + calibrated PDF glyphs",
                        pdf_refname=selected_refname,
                        pdf_xref=selected_xref,
                        extend_original_cff=True,
                        adaptive_glyph_plan=plan,
                    )
                    candidate.pdf_unicode_widths = extension_unicode_widths(
                        self.doc,
                        candidate,
                        replacement,
                    )
                    source_xrefs = ", ".join(
                        str(source.get("font_xref"))
                        for source in plan.get("sources", [])
                    )
                    note = (
                        str(plan.get("report") or "").strip()
                        + f" Donor xref(s): {source_xrefs}. "
                        "Existing target glyph programs, byte codes and resource "
                        "identity remain unchanged; the replacement remains searchable."
                    ).strip()
                    missing = []
                    fallback_mode = "adaptive_pdf_glyph"
                except Exception as exc:
                    adaptive_error = str(exc)

        if not candidate:
            candidate, note, missing = (
                self.font_resolver._existing_pdf_resource_candidate(
                    self.doc,
                    page,
                    self.selected_span,
                    replacement,
                )
            )
        if not candidate:
            composite_candidate, composite_note, composite_missing = (
                self.font_resolver.composite_pdf_resource_candidate(
                    self.doc,
                    page,
                    self.selected_span,
                    replacement,
                )
            )
            if composite_candidate:
                candidate = composite_candidate
                note = composite_note
                missing = []
            elif composite_missing:
                note = composite_note
                missing = composite_missing
        if not candidate:
            self.current_resolution = None
            self.approved_fallback = None
            if precise_cff_missing:
                missing = list(precise_cff_missing)
                note = (
                    "The original embedded CFF subset is missing the listed glyphs, "
                    "and no embedded resource could supply a geometrically compatible "
                    "outline after target-weight calibration. Use the database "
                    "completion option or upload/search the exact full font."
                )
                if adaptive_error:
                    note += " Calibration check: " + adaptive_error
            preview = " ".join(repr(ch) for ch in missing[:12])
            raise ValueError(
                note + (f" Missing glyphs: {preview}." if preview else "")
            )

        resolution = FontResolution(
            ok=True,
            candidate=candidate,
            exact_family=True,
            exact_weight=True,
            exact_style=True,
            glyphs_ok=True,
            source_note="Find Glyph Elsewhere in PDF: " + note,
        )
        self.current_resolution = resolution
        self.approved_fallback = {
            "mode": fallback_mode,
            "replacement": replacement,
            "token": secrets.token_urlsafe(16),
            "candidate": candidate,
            "resolution": resolution,
        }
        self.match_status = (
            (
                "✓ MISSING GLYPHS MATCHED TO TARGET WEIGHT\n"
                if fallback_mode == "adaptive_pdf_glyph"
                else "✓ GLYPHS FOUND ELSEWHERE IN PDF\n"
            )
            +
            f"Font: {candidate.display_name}\n"
            "No external font file will be added.\n"
            + note
        )
        self.match_tone = "success"
        self.status = (
            "Missing glyphs were calibrated to the selected font's existing outline weight."
            if fallback_mode == "adaptive_pdf_glyph"
            else "A verified same-family PDF font subset is ready."
        )
        return resolution

    def approve_database_fallback(
        self,
        *,
        candidate,
        replacement: str,
        record: dict,
        score: dict,
        mode: str,
    ):
        if not self.selected_span:
            raise ValueError("Select PDF text first.")
        if mode not in {"closest_font", "adaptive_font", "outline"}:
            raise ValueError("Unsupported font fallback mode.")

        # Keep the resolver's saved/manual candidate immutable. Fallback
        # preparation is approval-specific and must not leak into later edits.
        candidate = copy.copy(candidate)
        donor_display_name = candidate.display_name
        donor_weight = int(getattr(candidate, "weight", None) or 400)

        family_similarity = float(score.get("family_similarity", 0.0))
        family_exact = family_similarity >= 0.995
        weight_delta = int(score.get("weight_delta", 9999))
        italic_match = bool(score.get("italic_match"))
        subset_completion = False
        compatibility_report = None

        if mode == "closest_font" and (
            not family_exact
            or weight_delta > 100
            or not italic_match
        ):
            raise ValueError(
                "The database font was rejected before editing because it is not "
                "the same family, weight and style as the selected PDF font "
                f"(target weight {score.get('target_weight', 'unknown')}, "
                f"candidate weight {score.get('candidate_weight', 'unknown')}). "
                "The original PDF was not changed. Upload the exact full font face."
            )

        # Preferred path for the statement-style Type1C PDFs: retain the
        # selected PDF's actual font resource and append only glyph programs
        # that its subset does not already contain. Existing glyphs, byte codes,
        # widths and page resource names remain the original ones.
        if mode in {"closest_font", "adaptive_font"}:
            page = self.doc[self.page_index]
            prepared = self.font_resolver._cff_subset_extension_candidate(
                page,
                self.selected_span,
                candidate,
            )
            if getattr(prepared, "extend_original_cff", False):
                if mode == "adaptive_font":
                    plan = getattr(prepared, "adaptive_glyph_plan", None) or {}
                    if not plan:
                        raise ValueError(
                            "The database donor has no verified target-weight glyph plan."
                        )
                    if int(plan.get("target_font_xref", -1)) != int(
                        prepared.pdf_xref
                    ):
                        raise ValueError(
                            "The database glyph plan belongs to a different PDF font resource."
                        )
                    compatibility_report = {
                        "ok": True,
                        "reason": str(plan.get("report") or "").strip(),
                    }
                else:
                    compatibility_report = verify_donor_compatibility(
                        self.doc,
                        prepared,
                        self.selected_span.text,
                    )
                    if not compatibility_report.get("ok"):
                        raise ValueError(
                            compatibility_report.get("reason")
                            or "The donor font outline fingerprint does not match the original subset."
                        )
                candidate = prepared
                if mode == "adaptive_font":
                    target_family, profile_weight, target_italic = (
                        self.target_font_profile()
                    )
                    target_weight = int(
                        score.get("target_weight", profile_weight) or profile_weight
                    )
                    candidate.family = target_family
                    candidate.weight = target_weight
                    candidate.italic = target_italic
                candidate.pdf_unicode_widths = extension_unicode_widths(
                    self.doc,
                    candidate,
                    replacement,
                )
                subset_completion = True

        if mode in {"closest_font", "adaptive_font"} and not subset_completion:
            raise ValueError(
                "The selected PDF font cannot be completed safely in-place from "
                "this database face. No full-line font substitution was applied. "
                "Use an exact full font / AI exact-font search, or use vector "
                "outline mode when selectable text is not required."
            )

        source_label = (
            "MongoDB glyph donor calibrated to original PDF weight"
            if mode == "adaptive_font"
            else "MongoDB donor for original PDF subset completion"
            if subset_completion
            else "MongoDB vector-outline source"
        )
        candidate.source = source_label
        if mode == "adaptive_font":
            source_note = (
                f"Original PDF Type1C target-weight completion using "
                f"{donor_display_name} (donor metadata weight {donor_weight}; "
                f"target weight {candidate.weight}). The donor's shared glyphs were measured "
                "against the selected subset and only the requested missing outlines "
                "were emboldened/transformed before being appended. Existing glyphs, "
                "resource names and byte mappings remain untouched. "
                + str((compatibility_report or {}).get("reason") or "")
            )
        elif subset_completion:
            source_note = (
                f"Original PDF Type1C subset completion using {candidate.display_name}. "
                "Only missing glyph programs and their encoding/ToUnicode entries "
                "will be appended to the original /F# resource; existing glyphs "
                "remain untouched. "
                + str((compatibility_report or {}).get("reason") or "")
            )
        else:
            source_note = (
                f"Explicit {source_label}. Selected {candidate.display_name}; "
                f"family similarity {family_similarity:.3f}."
            )
        resolution = FontResolution(
            ok=True,
            candidate=candidate,
            exact_family=(True if mode == "adaptive_font" else family_exact),
            exact_weight=(True if mode == "adaptive_font" else weight_delta <= 100),
            exact_style=(True if mode == "adaptive_font" else italic_match),
            glyphs_ok=True,
            source_note=source_note,
        )
        self.current_resolution = resolution
        self.approved_fallback = {
            "mode": mode,
            "replacement": str(replacement or ""),
            "token": secrets.token_urlsafe(16),
            "candidate": candidate,
            "resolution": resolution,
            "font_id": str(record.get("_id", "")),
        }

        if mode == "outline":
            self.match_status = (
                "✓ VECTOR OUTLINE MODE READY\n"
                f"Outline source: {candidate.display_name}\n"
                + "The replacement will look like glyphs but will not be searchable "
                "or selectable as text. No replacement font resource will be embedded."
            )
            self.status = "Vector-outline fallback approved for this replacement only."
            self.match_tone = "warning"
        elif mode == "adaptive_font":
            plan = getattr(candidate, "adaptive_glyph_plan", None) or {}
            self.match_status = (
                "✓ MISSING GLYPHS CALIBRATED TO TARGET WEIGHT\n"
                f"Glyph source: {candidate.display_name}\n"
                f"Original resource: /{candidate.pdf_refname} "
                f"(xref {candidate.pdf_xref})\n"
                + str(plan.get("report") or "")
                + "\nOnly missing outlines will be added; existing target glyphs stay unchanged."
            )
            self.status = (
                "Database glyphs are calibrated to the selected PDF font's "
                "measured outline weight."
            )
            self.match_tone = "success"
        elif subset_completion:
            self.match_status = (
                "✓ ORIGINAL PDF SUBSET COMPLETION READY\n"
                f"Donor glyph source: {candidate.display_name}\n"
                f"Original resource: /{candidate.pdf_refname} "
                f"(xref {candidate.pdf_xref})\n"
                "Only characters missing from the original embedded subset will "
                "be appended. Existing glyphs will still use the original font."
            )
            self.status = "Missing-glyph completion is ready for the original PDF subset."
            self.match_tone = "success"
        return resolution

    def _record_clean_edit(
        self,
        original_span,
        updated_span,
        replacement,
        candidate,
        mode,
    ):

        if not self.edit_session:

            raise RuntimeError(
                "No active clean edit session."
            )

        self.edit_session.record_edit(
            page_number=
            self.page_index + 1,
            original_text=
            original_span.text,
            replacement_text=
            replacement,
            original_bbox=
            original_span.bbox,
            final_bbox=
            updated_span.bbox,
            origin=
            original_span.origin,
            font_family=
            candidate.family,
            font_face=
            candidate.display_name,
            font_source=
            candidate.source,
            font_size=
            original_span.size,
            color=
            original_span.color,
            mode=mode,
            subset_font_xref=(
                candidate.pdf_xref
                if getattr(candidate, "extend_original_cff", False)
                else None
            ),
            subset_added_chars=(
                getattr(candidate, "subset_added_chars", None)
                if getattr(candidate, "extend_original_cff", False)
                else None
            ),
        )

    def replace_selected_text(
        self,
        replacement: str,
        expansion_direction: str = "right",
        fallback_token: str | None = None,
    ):

        if (
            not self.doc
            or not self.selected_span
        ):

            raise ValueError(
                "Select text before "
                "replacing it."
            )

        expansion_direction = (
            str(expansion_direction or "right")
            .strip()
            .lower()
        )

        if expansion_direction not in {
            "right",
            "left",
        }:
            raise ValueError(
                "Expansion direction must be 'right' or 'left'."
            )

        approved_fallback = self.approved_fallback
        fallback_matches = bool(
            approved_fallback
            and approved_fallback.get("replacement") == str(replacement or "")
            and fallback_token
            and secrets.compare_digest(
                str(approved_fallback.get("token") or ""),
                str(fallback_token),
            )
            and approved_fallback.get("candidate")
            and approved_fallback.get("resolution")
        )

        if fallback_matches:
            self.current_resolution = approved_fallback["resolution"]
        else:
            self.approved_fallback = None
            self.resolve_selected_font(
                replacement
            )

        if (
            not self.current_resolution
            or not
            self.current_resolution.ok
        ):

            raise ValueError(
                "The replacement was not "
                "performed because the "
                "required font/style could "
                "not be verified."
            )

        selected_candidate = (
            self.current_resolution
            .candidate
        )

        fallback_mode = (
            approved_fallback.get("mode")
            if fallback_matches
            else None
        )

        span = self.selected_span

        page = self.doc[
            self.page_index
        ]

        new_width = (
            self.engine
            .replacement_width(
                selected_candidate,
                replacement,
                span.size,
            )
        )

        if new_width is None:

            raise ValueError(
                "The replacement text width "
                "could not be measured "
                "safely with the verified font."
            )

        horizontal_plan = (
            self.engine
            .plan_horizontal_expansion(
                page,
                self.spans,
                span,
                new_width,
                expansion_direction=
                expansion_direction,
            )
        )

        def span_key(item):
            return (
                item.block_index,
                item.line_index,
                item.span_index,
            )

        def resolve_unchanged_span(
            item,
        ):

            item_docx_ref = None

            if self.docx_index.loaded:

                x0, y0, _, _ = (
                    item.bbox
                )

                item_docx_ref = (
                    self.docx_index.find(
                        item.text,
                        pdf_size_pt=
                        item.size,
                        pdf_page_number=
                        self.page_index + 1,
                        pdf_x_pt=x0,
                        pdf_y_pt=y0,
                    )
                )

            item_resolution = (
                self.font_resolver.resolve(
                    self.doc,
                    page,
                    item,
                    item.text,
                    item_docx_ref,
                )
            )

            if (
                not item_resolution.ok
                or not
                item_resolution.candidate
            ):

                return None

            return (
                item_resolution.candidate
            )

        if horizontal_plan.get(
            "ok"
        ):

            shifted_items = []

            for (
                neighbor,
                dx,
            ) in horizontal_plan.get(
                "shifts",
                [],
            ):

                neighbor_candidate = (
                    resolve_unchanged_span(
                        neighbor
                    )
                )

                if (
                    neighbor_candidate
                    is None
                ):

                    raise ValueError(
                        "The replacement requires "
                        "moving following text, "
                        "but the exact font could "
                        "not be verified for: "
                        f"{neighbor.text}"
                    )

                shifted_items.append(
                    {
                        "span": neighbor,
                        "candidate":
                        neighbor_candidate,
                        "dx": dx,
                    }
                )

            try:

                snapshot = self._snapshot_pdf_bytes()

                self.undo_stack.append(
                    (
                        snapshot,
                        len(
                            self.edit_session.edits
                        )
                        if self.edit_session
                        else 0,
                    )
                )

                # Structural row moves use clipped Form XObjects so existing
                # subset-font text can be copied exactly.  PyMuPDF redaction
                # may rename / rewrite those wrappers, which would leave extra
                # reachable objects at Save As.  Flatten the verified wrappers
                # inside this Undo transaction before editing a cloned/moved
                # cell; the PDF text and pixels are checked by the flattener.
                if self.edit_session:
                    self.edit_session._inline_line_reflow_forms(self.doc)

                if fallback_mode == "outline":
                    updated_span, outline_bbox = (
                        self.engine.replace_with_outline_reflow(
                            page,
                            span,
                            replacement,
                            selected_candidate,
                            horizontal_plan["expanded_bbox"],
                            shifted_items,
                            replacement_origin=horizontal_plan.get(
                                "replacement_origin"
                            ),
                        )
                    )
                    self.edit_session.record_outline_edit(
                        page_number=self.page_index + 1,
                        original_text=span.text,
                        replacement_text=replacement,
                        original_bbox=span.bbox,
                        final_bbox=updated_span.bbox,
                        outline_bbox=outline_bbox,
                        origin=updated_span.origin,
                        font_family=selected_candidate.family,
                        font_face=selected_candidate.display_name,
                        font_source=selected_candidate.source,
                        font_size=span.size,
                        color=span.color,
                    )
                else:
                    updated_span = (
                        self.engine
                        .replace_with_horizontal_reflow(
                            page,
                            span,
                            replacement,
                            selected_candidate,
                            horizontal_plan[
                                "expanded_bbox"
                            ],
                            shifted_items,
                            replacement_origin=
                            horizontal_plan.get(
                                "replacement_origin"
                            ),
                        )
                    )

                    self._record_clean_edit(
                        span,
                        updated_span,
                        replacement,
                        selected_candidate,
                        (
                            "clean-redaction-horizontal-subset-completion"
                            if getattr(
                                selected_candidate,
                                "extend_original_cff",
                                False,
                            )
                            else "clean-redaction-horizontal-closest-font"
                            if fallback_mode == "closest_font"
                            else "clean-redaction-horizontal"
                        ),
                    )

                self.selected_span = (
                    updated_span
                )

                self.refresh_spans()

                self.approved_fallback = None

                moved_count = len(
                    shifted_items
                )

                self.match_status = (
                    (
                        "✓ VECTOR OUTLINE REPLACEMENT APPLIED\n"
                        if fallback_mode == "outline"
                        else "✓ CLEAN REPLACEMENT APPLIED\n"
                    )
                    + f"Font size kept at "
                    f"{span.size:.4f} pt.\n"
                    f"Container width: "
                    f"{span.bbox[2]-span.bbox[0]:.2f}"
                    " → "
                    f"{updated_span.bbox[2]-updated_span.bbox[0]:.2f}"
                    " pt\n"
                    + (
                        "Preceding spans shifted "
                        f"left: {moved_count}\n"
                        if expansion_direction == "left"
                        else
                        "Following spans shifted "
                        f"right: {moved_count}\n"
                    )
                    + (
                        "Searchable replacement: no (vector outline)\n"
                        if fallback_mode == "outline"
                        else ""
                    )
                    + "Font source: "
                    f"{selected_candidate.source}"
                )

                self.match_tone = (
                    "success"
                )

                self.status = (
                    horizontal_plan.get(
                        "reason",
                        "Replacement applied "
                        "with automatic width "
                        "expansion.",
                    )
                    + " Use Save As when ready."
                )

                if fallback_mode == "outline":
                    # Vector paths are intentionally not selectable PDF text.
                    # Drop the synthetic text selection so a later replacement
                    # cannot accidentally be layered over the existing paths.
                    self.selected_span = None
                    self.selected_line_spans = []
                    self.selected_row_info = None
                    self.selected_docx_ref = None
                    self.current_resolution = None

                return

            except Exception:

                self._rollback_last_snapshot()

                raise

        if expansion_direction == "left":
            raise ValueError(
                horizontal_plan.get(
                    "reason",
                    "There is not enough safe space to expand this text to the left.",
                )
            )

        if fallback_mode == "outline":
            raise ValueError(
                horizontal_plan.get(
                    "reason",
                    "The outline replacement cannot fit safely on this line.",
                )
                + " Vector-outline fallback does not perform multiline text reflow. "
                "Use a shorter replacement or free horizontal space."
            )

        paragraph_spans = (
            self.engine
            ._paragraph_reflow_spans(
                self.spans,
                span,
            )
        )

        if not paragraph_spans:

            raise ValueError(
                horizontal_plan.get(
                    "reason",
                    "",
                )
                + " The PDF does not expose "
                "enough paragraph structure "
                "for safe multiline wrapping."
            )

        candidate_by_span_key = {}

        for item in paragraph_spans:

            if self.engine._same_span(
                item,
                span,
            ):
                continue

            item_candidate = (
                resolve_unchanged_span(
                    item
                )
            )

            if item_candidate is None:

                raise ValueError(
                    "The exact font could not "
                    "be verified for paragraph "
                    "text: "
                    + item.text
                )

            candidate_by_span_key[
                span_key(item)
            ] = item_candidate

        multiline_plan = (
            self.engine
            .build_multiline_reflow_plan(
                page,
                self.spans,
                span,
                replacement,
                selected_candidate,
                candidate_by_span_key,
            )
        )

        if not multiline_plan.get(
            "ok"
        ):

            raise ValueError(
                multiline_plan.get(
                    "reason",
                    "The replacement cannot "
                    "be reflowed safely.",
                )
            )

        shifted_below_items = []

        for item in multiline_plan.get(
            "shift_below",
            [],
        ):

            item_candidate = (
                resolve_unchanged_span(
                    item
                )
            )

            if item_candidate is None:

                raise ValueError(
                    "The paragraph needs extra "
                    "vertical space, but the "
                    "exact font could not be "
                    "verified for following text: "
                    f"{item.text}"
                )

            shifted_below_items.append(
                {
                    "span": item,
                    "candidate":
                    item_candidate,
                    "dy":
                    multiline_plan[
                        "growth"
                    ],
                }
            )

        try:

            snapshot = self._snapshot_pdf_bytes()

            self.undo_stack.append(
                (
                    snapshot,
                    len(
                        self.edit_session.edits
                    )
                    if self.edit_session
                    else 0,
                )
            )

            if self.edit_session:
                self.edit_session._inline_line_reflow_forms(self.doc)

            updated_span = (
                self.engine
                .replace_with_multiline_reflow(
                    page,
                    span,
                    replacement,
                    multiline_plan,
                    shifted_below_items,
                )
            )

            self._record_clean_edit(
                span,
                updated_span,
                replacement,
                selected_candidate,
                (
                    "clean-redaction-multiline-subset-completion"
                    if getattr(
                        selected_candidate,
                        "extend_original_cff",
                        False,
                    )
                    else "clean-redaction-multiline-closest-font"
                    if fallback_mode == "closest_font"
                    else "clean-redaction-multiline"
                ),
            )

            self.selected_span = (
                updated_span
            )

            self.refresh_spans()

            self.approved_fallback = None

            self.match_status = (
                "✓ CLEAN REPLACEMENT APPLIED "
                "WITH MULTILINE REFLOW\n"
                f"Font size kept at "
                f"{span.size:.4f} pt.\n"
                "Container height increased by "
                f"{multiline_plan['growth']:.2f} "
                "pt\n"
                "Following text shifted down: "
                f"{len(shifted_below_items)} "
                "span(s)\n"
                "Font source: "
                f"{selected_candidate.source}"
            )

            self.match_tone = (
                "success"
            )

            self.status = (
                multiline_plan.get(
                    "reason",
                    "Replacement wrapped onto "
                    "additional line(s).",
                )
                + " Use Save As when ready."
            )

        except Exception:

            self._rollback_last_snapshot()

            raise

    def _rollback_last_snapshot(
        self,
    ):

        if not self.undo_stack:
            return

        previous, edit_state = (
            self.undo_stack.pop()
        )

        restore_page_index = self.page_index
        restored_spans = None
        if self.edit_session:
            if isinstance(edit_state, int):
                self.edit_session.truncate_edits(edit_state)
            elif isinstance(edit_state, dict):
                self.edit_session.edits = copy.deepcopy(edit_state.get("edits", []))
                restore_page_index = int(edit_state.get("page_index", self.page_index))
                restored_spans = copy.deepcopy(edit_state.get("spans"))
            else:
                self.edit_session.edits = copy.deepcopy(edit_state)

        if self.doc:
            self.doc.close()

        self.doc = pymupdf.open(
            stream=previous,
            filetype="pdf",
        )
        self.page_index = max(0, min(restore_page_index, len(self.doc) - 1))

        self.clear_selection()
        if restored_spans is None:
            self.refresh_spans()
        else:
            self.spans = restored_spans

    def undo_last(self):

        if not self.undo_stack:

            raise ValueError(
                "There is no edit "
                "to undo."
            )

        previous, edit_state = (
            self.undo_stack.pop()
        )

        restore_page_index = self.page_index
        restored_spans = None
        if self.edit_session:
            if isinstance(edit_state, int):
                self.edit_session.truncate_edits(edit_state)
            elif isinstance(edit_state, dict):
                self.edit_session.edits = copy.deepcopy(edit_state.get("edits", []))
                restore_page_index = int(edit_state.get("page_index", self.page_index))
                restored_spans = copy.deepcopy(edit_state.get("spans"))
            else:
                self.edit_session.edits = copy.deepcopy(edit_state)

        if self.doc:
            self.doc.close()

        self.doc = pymupdf.open(
            stream=previous,
            filetype="pdf",
        )
        self.page_index = max(0, min(restore_page_index, len(self.doc) - 1))

        self.clear_selection()
        if restored_spans is None:
            self.refresh_spans()
        else:
            self.spans = restored_spans

        self.last_output = None
        self.last_audit = None
        self.last_validation = None

        self.status = (
            "Last edit undone."
        )

    def export(self):

        if (
            not self.doc
            or not self.edit_session
        ):

            raise ValueError(
                "No PDF is open."
            )

        base = (
            Path(
                self.original_name
            ).stem
            or "document"
        )

        output = (
            self.session_dir
            / f"edited_clean_{base}.pdf"
        )

        (
            output_path,
            audit_path,
            report_path,
        ) = self.edit_session.export_clean(
            self.doc,
            str(output),
        )

        self.last_output = Path(
            output_path
        )

        self.last_audit = Path(
            audit_path
        )

        self.last_validation = Path(
            report_path
        )

        self.status = (
            "Saved clean edited copy: "
            f"{self.last_output.name}"
        )

        return self.last_output

    def _docx_data(self):

        ref = self.selected_docx_ref

        if not ref:

            return {
                "family": "-",
                "size": "-",
                "weight": "-",
                "italic": "-",
                "confidence": (
                    "No usable DOCX match — "
                    "PDF-only fallback"
                    if self.docx_index.loaded
                    else (
                        "DOCX not loaded — "
                        "PDF-only mode"
                    )
                ),
                "declared": "-",
                "reason": "-",
                "tone": "neutral",
            }

        if ref.bold is True:
            weight = (
                "Bold / target 700"
            )

        elif ref.bold is False:
            weight = (
                "Regular / target 400"
            )

        else:
            weight = "Unknown"

        if ref.italic is True:
            italic = "Yes"

        elif ref.italic is False:
            italic = "No"

        else:
            italic = "Unknown"

        declared = (
            f"{ref.declared_family} → "
            f"{ref.family}"
            if (
                ref.declared_family
                and (
                    ref.declared_family
                    != ref.family
                )
            )
            else (
                ref.declared_family
                or ref.family
                or "-"
            )
        )

        return {
            "family":
            ref.family
            or "Unknown",
            "size":
            (
                f"{ref.size_pt:.4f} pt"
                if ref.size_pt is not None
                else "Unknown"
            ),
            "weight":
            weight,
            "italic":
            italic,
            "confidence":
            ref.confidence,
            "declared":
            declared,
            "reason":
            ref.match_reason
            or "-",
            "tone":
            (
                "warning"
                if ref.confidence.startswith(
                    "Medium"
                )
                else "success"
            ),
        }

    def _resolution_data(self):

        resolution = (
            self.current_resolution
        )

        if (
            self.docx_index.loaded
            and self.selected_docx_ref
        ):

            mode = (
                "DOCX Reference Mode"
            )

        elif self.docx_index.loaded:

            mode = (
                "PDF-Only fallback "
                "(DOCX has no usable match)"
            )

        else:

            mode = (
                "PDF-Only Exact Font Mode"
            )

        if (
            not resolution
            or not resolution.ok
            or not resolution.candidate
        ):

            return {
                "mode": mode,
                "fallback_mode": None,
                "fallback_token": None,
                "family": "-",
                "face": "-",
                "weight": "-",
                "source": "-",
                "ok": False,
            }

        candidate = (
            resolution.candidate
        )

        return {
            "mode":
            mode,
            "fallback_mode": (
                self.approved_fallback.get("mode")
                if self.approved_fallback
                else None
            ),
            "fallback_token": (
                self.approved_fallback.get("token")
                if self.approved_fallback
                else None
            ),
            "family":
            (
                candidate.family
                or (
                    self.selected_docx_ref.family
                    if (
                        self.selected_docx_ref
                        and
                        self.selected_docx_ref.family
                    )
                    else "Verified PDF font"
                )
            ),
            "face":
            candidate.display_name,
            "weight":
            weight_label(
                candidate.weight
            ),
            "source":
            (
                f"{candidate.source}\n"
                f"{resolution.source_note}"
            ),
            "ok":
            True,
        }

    def _selection_data(self):

        span = self.selected_span

        if not span:
            return None

        line_selected = bool(self.selected_line_spans)
        if line_selected:
            bbox = self.engine.line_bbox(self.selected_line_spans) or span.bbox
            display_text = " ".join(
                item.text.strip()
                for item in self.selected_line_spans
                if item.text.strip()
            )
        else:
            bbox = span.bbox
            display_text = span.text

        x0, y0, x1, y1 = bbox

        family = (
            humanize_pdf_font_name(
                span.font
            )
        )

        if (
            self.current_resolution
            and
            self.current_resolution.candidate
        ):

            family = (
                self.current_resolution
                .candidate.family
                or family
            )

        if (
            self.selected_docx_ref
            and self.selected_docx_ref.family
        ):

            family = (
                self.selected_docx_ref.family
                or family
            )

        family = (
            family
            or strip_subset_prefix(
                span.font
            )
            or ""
        ).strip()

        search_url = (
            "https://www.google.com/search?q="
            + quote_plus(
                f'"{family}" font TTF OTF '
                "official licensed"
            )
            if family
            else ""
        )

        return {
            "text":
            display_text,
            "bbox":
            list(bbox),
            "line_selected":
            line_selected,
            "internal_font":
            (
                strip_subset_prefix(
                    span.font
                )
                or "-"
            ),
            "style":
            font_flags_text(
                span.flags
            ),
            "size":
            f"{span.size:.4f} pt",
            "size_value":
            span.size,
            "color":
            f"#{span.color:06X}",
            "position":
            (
                f"x={x0:.2f}, "
                f"y={y0:.2f}, "
                f"w={x1-x0:.2f}, "
                f"h={y1-y0:.2f}"
            ),
            "font_search_url":
            search_url,
        }

    def state(
        self,
    ) -> dict[str, Any]:

        return {
            "app_name":
            APP_NAME,
            "filename":
            self.original_name,
            "page_index":
            self.page_index,
            "page_number":
            self.page_index + 1,
            "page_count":
            self.page_count,
            "zoom":
            self.zoom,
            "selection":
            self._selection_data(),
            "line_selected":
            bool(self.selected_line_spans),
            "line_delete_options": {
                "remove_table_rule_default": False,
                "move_remaining_table_rules": True,
            },
            "row_insert": (
                self.selected_row_info
                or self._selected_table_row_info()
            ),
            "docx":
            self._docx_data(),
            "resolution":
            self._resolution_data(),
            "docx_label":
            self.docx_label,
            "status":
            self.status,
            "match_status":
            self.match_status,
            "match_tone":
            self.match_tone,
            "undo_available":
            bool(
                self.undo_stack
            ),
            "can_save":
            self.doc is not None,
            "audit_available":
            bool(
                self.last_audit
                and
                self.last_audit.exists()
            ),
            "validation_available":
            bool(
                self.last_validation
                and
                self.last_validation.exists()
            ),
        }


app = FastAPI(
    title=APP_NAME
)

app.mount(
    "/static",
    StaticFiles(
        directory=str(
            WEB_DIR / "static"
        )
    ),
    name="static",
)


_sessions: dict[
    str,
    WebEditorSession,
] = {}

_sessions_lock = (
    threading.RLock()
)


def _cleanup_old_sessions():

    now = time.time()

    expired = []

    with _sessions_lock:

        for (
            sid,
            session,
        ) in list(
            _sessions.items()
        ):

            if (
                now
                - session.last_access
                > SESSION_TTL_SECONDS
            ):

                expired.append(
                    (
                        sid,
                        session,
                    )
                )

                del _sessions[sid]

    for _, session in expired:

        session.close()

        shutil.rmtree(
            session.session_dir,
            ignore_errors=True,
        )


def _get_session(
    sid: str,
    user: UserContext,
) -> WebEditorSession:

    with _sessions_lock:

        session = _sessions.get(
            sid
        )

    if not session:

        raise HTTPException(
            status_code=404,
            detail=(
                "Editor session not found. "
                "Upload the PDF again."
            ),
        )

    if (
        session.owner_id
        != user.user_id
        and not user.is_admin
    ):

        raise HTTPException(
            status_code=404,
            detail=(
                "Editor session not found. "
                "Upload the PDF again."
            ),
        )

    session.touch()

    return session


async def _save_upload(
    upload: UploadFile,
    path: Path,
    max_bytes: int,
):

    total = 0

    with path.open(
        "wb"
    ) as fh:

        while True:

            chunk = await upload.read(
                1024 * 1024
            )

            if not chunk:
                break

            total += len(chunk)

            if total > max_bytes:

                fh.close()

                path.unlink(
                    missing_ok=True
                )

                raise HTTPException(
                    status_code=413,
                    detail=(
                        "Uploaded file is too large."
                    ),
                )

            fh.write(
                chunk
            )

    await upload.close()


@app.get("/")
def index():

    return FileResponse(
        WEB_DIR / "index.html"
    )


@app.get("/health")
def health():

    return {
        "ok": True,
        "app": APP_NAME,
    }


@app.post(
    "/api/auth/login"
)
def login(
    req: LoginRequest,
):

    store = _get_auth_store()

    user = store.authenticate(
        req.username,
        req.password,
    )

    if not user:

        raise HTTPException(
            status_code=401,
            detail=(
                "Invalid username or password."
            ),
        )

    token = _create_auth_session(
        user
    )

    response = JSONResponse(
        {
            "user":
            _public_user(
                user
            )
        }
    )

    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite="lax",
        max_age=
        AUTH_SESSION_TTL_SECONDS,
        path="/",
    )

    return response


@app.post(
    "/api/auth/logout"
)
def logout(
    request: Request,
):

    _destroy_auth_session(
        request.cookies.get(
            AUTH_COOKIE_NAME
        )
    )

    response = JSONResponse(
        {
            "ok": True
        }
    )

    response.delete_cookie(
        AUTH_COOKIE_NAME,
        path="/",
    )

    return response


@app.get(
    "/api/auth/me"
)
def current_user(
    user: UserContext = Depends(
        _require_user
    ),
):

    return {
        "user":
        user.public_data()
    }


@app.get(
    "/api/admin/users"
)
def list_users(
    admin: UserContext = Depends(
        _require_admin
    ),
):

    return {
        "users":
        _get_auth_store()
        .list_users()
    }


@app.post(
    "/api/admin/users"
)
def create_user(
    req: CreateUserRequest,
    admin: UserContext = Depends(
        _require_admin
    ),
):

    try:

        user = (
            _get_auth_store()
            .create_user(
                req.username,
                req.password,
            )
        )

        return JSONResponse(
            {
                "user":
                _public_user(
                    user
                )
            },
            status_code=201,
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


@app.post(
    "/api/pdf"
)
async def upload_pdf(
    file: UploadFile = File(
        ...
    ),
    user: UserContext = Depends(
        _require_user
    ),
):

    _cleanup_old_sessions()

    name = Path(
        file.filename
        or "document.pdf"
    ).name

    if not name.lower().endswith(
        ".pdf"
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "Please upload a PDF file."
            ),
        )

    sid = uuid.uuid4().hex

    session_dir = (
        RUNTIME_DIR / sid
    )

    session_dir.mkdir(
        parents=True,
        exist_ok=False,
    )

    source = (
        session_dir / "source.pdf"
    )

    try:

        await _save_upload(
            file,
            source,
            MAX_PDF_BYTES,
        )

        session = WebEditorSession(
            source,
            name,
            session_dir,
            owner_id=
            user.user_id,
            owner_username=
            user.username,
        )

    except Exception:

        shutil.rmtree(
            session_dir,
            ignore_errors=True,
        )

        raise

    with _sessions_lock:

        _sessions[sid] = (
            session
        )

    return {
        "session_id":
        sid,
        "state":
        session.state(),
    }


@app.post(
    "/api/{sid}/docx"
)
async def upload_docx(
    sid: str,
    file: UploadFile = File(
        ...
    ),
    user: UserContext = Depends(
        _require_user
    ),
):

    session = _get_session(
        sid,
        user,
    )

    name = Path(
        file.filename
        or "reference.docx"
    ).name

    if not name.lower().endswith(
        ".docx"
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "Please upload a DOCX file."
            ),
        )

    path = (
        session.session_dir
        / (
            "reference_"
            f"{uuid.uuid4().hex}"
            ".docx"
        )
    )

    await _save_upload(
        file,
        path,
        MAX_DOCX_BYTES,
    )

    try:

        with session.lock:

            count = (
                session.load_docx(
                    path,
                    name,
                )
            )

            state = (
                session.state()
            )

        return {
            "count":
            count,
            "state":
            state,
        }

    except Exception as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


@app.post(
    "/api/{sid}/font"
)
async def upload_font(
    sid: str,
    file: UploadFile = File(
        ...
    ),
    user: UserContext = Depends(
        _require_user
    ),
):

    session = _get_session(
        sid,
        user,
    )

    name = Path(
        file.filename
        or "font.ttf"
    ).name

    if not name.lower().endswith(
        (
            ".ttf",
            ".otf",
        )
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "Please upload a TTF "
                "or OTF font file."
            ),
        )

    path = (
        session.session_dir
        / (
            "font_"
            f"{uuid.uuid4().hex}"
            f"{Path(name).suffix.lower()}"
        )
    )

    await _save_upload(
        file,
        path,
        MAX_FONT_BYTES,
    )

    try:

        with session.lock:

            candidate = (
                session.load_manual_font(
                    path
                )
            )

        font_record, created = (
            _get_font_store().save_font(
                path,
                candidate,
                user,
                name,
            )
        )

        with session.lock:
            session.status = (
                f"Loaded exact font: {candidate.display_name}. "
                + (
                    "Saved to MongoDB font library."
                    if created
                    else "This font already exists in the MongoDB font library."
                )
            )
            state = session.state()

        return {
            "font": {
                "family":
                candidate.family
                or "-",
                "face":
                candidate.display_name,
                "weight":
                weight_label(
                    candidate.weight
                ),
            },
            "database": {
                "saved": True,
                "created": created,
                "font": _public_font_record(font_record),
            },
            "state":
            state,
        }

    except Exception as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


@app.get("/api/fonts")
def list_saved_fonts(
    user: UserContext = Depends(_require_user),
):
    return {
        "fonts": _get_font_store().list_fonts(),
    }


@app.post("/api/{sid}/font-db/{font_id}")
def load_saved_font(
    sid: str,
    font_id: str,
    req: TextRequest,
    user: UserContext = Depends(_require_user),
):
    session = _get_session(sid, user)

    try:
        path, record = _get_font_store().materialize_font(
            font_id,
            session.session_dir,
        )

        with session.lock:
            candidate = session.load_manual_font(path)
            resolution = session.resolve_selected_font(req.replacement)

            strict_ok = bool(
                resolution
                and resolution.ok
                and resolution.candidate
                and resolution.exact_family
                and resolution.exact_weight
                and resolution.exact_style
                and resolution.glyphs_ok
            )

            chosen_path = (
                str(getattr(resolution.candidate, "path", "") or "")
                if resolution and resolution.candidate
                else ""
            )
            selected_db_font_used = False
            if chosen_path:
                try:
                    selected_db_font_used = (
                        Path(chosen_path).resolve() == Path(path).resolve()
                    )
                except Exception:
                    selected_db_font_used = chosen_path == str(path)

            # Match the behavior of the local "Load Exact TTF/OTF Font" path.
            # Loading a saved font registers it as a manual candidate. The normal
            # resolver is still authoritative. If the current replacement can
            # already be rendered by the original PDF font resource, the resolver
            # may correctly prefer that resource before the manual candidate.
            # That is not a database-font failure: the saved font remains loaded
            # and will be considered when a later replacement actually needs it.
            accepted = strict_ok

            if accepted:
                if selected_db_font_used:
                    session.status = (
                        "Saved MongoDB font loaded and passed exact verification: "
                        f"{candidate.display_name}"
                    )
                    session.match_status += (
                        "\n\nDatabase verification:\n"
                        "✓ selected MongoDB font is the verified replacement font"
                    )
                else:
                    session.status = (
                        f"Saved MongoDB font loaded: {candidate.display_name}. "
                        "The current replacement already passes with an existing exact "
                        "PDF font resource, so that resource remains preferred."
                    )
                    session.match_status += (
                        "\n\nDatabase verification:\n"
                        "✓ saved font loaded and available as a manual exact-font candidate\n"
                        "ℹ current text was verified with the existing PDF font resource, "
                        "so the database font was not needed for this text"
                    )
                session.match_tone = "success"
            else:
                previous = session.match_status
                session.match_status = (
                    "✗ SAVED FONT REJECTED\n"
                    f"Selected database font: {candidate.display_name}\n\n"
                    + previous
                )
                session.match_tone = "error"
                session.status = (
                    f"Saved font '{candidate.display_name}' was loaded, but the normal "
                    "exact-font verification still failed for the requested text."
                )

            return {
                "accepted": accepted,
                "font": _public_font_record(record),
                "state": session.state(),
            }

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not load saved font: {exc}") from exc


@app.post("/api/{sid}/font-find-pdf")
def find_font_glyphs_in_pdf(
    sid: str,
    req: TextRequest,
    user: UserContext = Depends(_require_user),
):
    session = _get_session(sid, user)
    try:
        with session.lock:
            session.find_glyph_elsewhere_in_pdf(req.replacement)
            return session.state()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _prepare_database_fallback(
    session: WebEditorSession,
    req: TextRequest,
    *,
    mode: str,
):
    if mode == "outline" and not str(req.replacement or "").strip():
        raise ValueError("Vector-outline fallback requires visible replacement text.")
    family, weight, italic = session.target_font_profile()

    def candidate_from_path(path: Path) -> FontCandidate:
        meta = read_font_metadata_from_path(str(path))
        if not meta:
            raise ValueError("Saved font metadata could not be read safely.")
        return FontCandidate(
            path=str(path),
            family=meta.get("family"),
            subfamily=meta.get("subfamily"),
            full_name=meta.get("full_name"),
            postscript=meta.get("postscript"),
            weight=meta.get("weight"),
            italic=bool(meta.get("italic")),
            source="Saved MongoDB font",
        )

    required_glyphs = str(req.replacement or "")
    candidate_validator = None
    accepted_completion: dict[str, dict[str, Any]] = {}
    if mode == "closest_font":
        page = session.doc[session.page_index]
        record = session.font_resolver.find_page_font(page, session.selected_span)
        if record and int(record.get("xref", 0) or 0) > 0:
            _already_available, missing, _widths = inspect_existing_cff(
                session.doc,
                int(record["xref"]),
                required_glyphs,
            )
            missing_text = "".join(dict.fromkeys(missing))
            if missing_text:
                required_glyphs = missing_text

        def candidate_validator(path, _record, _score):
            proposed = candidate_from_path(path)
            prepared = session.font_resolver._cff_subset_extension_candidate(
                page,
                session.selected_span,
                proposed,
            )
            if not getattr(prepared, "extend_original_cff", False):
                return False, "The selected PDF font is not safely extendable in place."
            similarity = float(_score.get("family_similarity", 0.0))
            candidate_weight = int(_score.get("candidate_weight", 400))
            target_weight = int(_score.get("target_weight", 400))
            italic_match = bool(_score.get("italic_match"))
            if similarity < 0.65:
                return False, (
                    "Font family is too different for missing-glyph weight calibration "
                    f"(similarity {similarity:.3f})."
                )
            if not italic_match:
                return False, "Italic/upright style does not match the selected PDF font."

            report = verify_donor_compatibility(
                session.doc,
                prepared,
                session.selected_span.text,
            )
            path_key = str(Path(path).resolve())
            strict_metadata = bool(
                float(_score.get("family_similarity", 0.0)) >= 0.995
                and int(_score.get("weight_delta", 9999)) <= 100
                and italic_match
            )
            if strict_metadata and report.get("ok"):
                accepted_completion[path_key] = {
                    "mode": "closest_font",
                    "report": str(report.get("reason") or ""),
                }
                return True, str(report.get("reason") or "")

            if candidate_weight > target_weight:
                return False, (
                    "The donor is heavier than the target; this safe path only "
                    "emboldens compatible lighter outlines."
                )

            try:
                plan = build_adaptive_external_glyph_plan(
                    session.doc,
                    int(prepared.pdf_xref),
                    prepared,
                    required_glyphs,
                    session.selected_span.text,
                )
            except Exception as exc:
                return False, str(exc)
            accepted_completion[path_key] = {
                "mode": "adaptive_font",
                "plan": plan,
                "report": str(plan.get("report") or ""),
            }
            return True, str(plan.get("report") or "")

    path, record, score = _get_font_store().best_compatible_font(
        replacement=required_glyphs,
        target_family=family,
        target_weight=weight,
        target_italic=italic,
        destination_dir=session.session_dir,
        require_exact_face=False,
        candidate_validator=candidate_validator,
    )
    candidate = candidate_from_path(path)
    approval_mode = mode
    completion = accepted_completion.get(str(Path(path).resolve()))
    if mode == "closest_font" and completion:
        approval_mode = str(completion.get("mode") or mode)
        if approval_mode == "adaptive_font":
            candidate.adaptive_glyph_plan = completion.get("plan")
    session.approve_database_fallback(
        candidate=candidate,
        replacement=req.replacement,
        record=record,
        score=score,
        mode=approval_mode,
    )
    score = dict(score)
    score["completion_mode"] = approval_mode
    return {
        "font": _public_font_record(record),
        "score": score,
        "state": session.state(),
    }


@app.post("/api/{sid}/font-db-closest")
def use_closest_database_font(
    sid: str,
    req: TextRequest,
    user: UserContext = Depends(_require_user),
):
    session = _get_session(sid, user)
    try:
        with session.lock:
            return _prepare_database_fallback(session, req, mode="closest_font")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not complete the original PDF font subset: {exc}",
        ) from exc


@app.post("/api/{sid}/font-outline")
def prepare_vector_outline(
    sid: str,
    req: TextRequest,
    user: UserContext = Depends(_require_user),
):
    session = _get_session(sid, user)
    try:
        with session.lock:
            return _prepare_database_fallback(session, req, mode="outline")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not prepare the vector outline: {exc}",
        ) from exc


@app.get(
    "/api/{sid}/state"
)
def state(
    sid: str,
    user: UserContext = Depends(
        _require_user
    ),
):

    session = _get_session(
        sid,
        user,
    )

    with session.lock:

        return session.state()


@app.get(
    "/api/{sid}/page.png"
)
def page_image(
    sid: str,
    zoom: float = 1.35,
    user: UserContext = Depends(
        _require_user
    ),
):

    session = _get_session(
        sid,
        user,
    )

    try:

        with session.lock:

            data = (
                session.render_png(
                    zoom
                )
            )

        return Response(
            content=data,
            media_type="image/png",
            headers={
                "Cache-Control":
                "no-store, max-age=0"
            },
        )

    except Exception as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


@app.post(
    "/api/{sid}/page"
)
def change_page(
    sid: str,
    req: PageRequest,
    user: UserContext = Depends(
        _require_user
    ),
):

    session = _get_session(
        sid,
        user,
    )

    try:

        with session.lock:

            session.set_page(
                req.page_index
            )

            return (
                session.state()
            )

    except Exception as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


@app.post(
    "/api/{sid}/select"
)
def select_text(
    sid: str,
    req: PointRequest,
    user: UserContext = Depends(
        _require_user
    ),
):

    session = _get_session(
        sid,
        user,
    )

    try:

        with session.lock:

            session.select_at(
                req.x,
                req.y,
            )

            return (
                session.state()
            )

    except Exception as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


@app.post(
    "/api/{sid}/page/add"
)
def add_page(
    sid: str,
    user: UserContext = Depends(_require_user),
):
    session = _get_session(sid, user)
    try:
        with session.lock:
            session.add_blank_page_after_current()
            return session.state()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete(
    "/api/{sid}/page/current"
)
def delete_page(
    sid: str,
    user: UserContext = Depends(_require_user),
):
    session = _get_session(sid, user)
    try:
        with session.lock:
            session.delete_current_page()
            return session.state()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/{sid}/line/select"
)
def select_full_line(
    sid: str,
    user: UserContext = Depends(_require_user),
):
    session = _get_session(sid, user)
    try:
        with session.lock:
            session.select_full_line()
            return session.state()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/{sid}/line/delete"
)
def delete_full_line(
    sid: str,
    req: DeleteLineRequest | None = None,
    user: UserContext = Depends(_require_user),
):
    session = _get_session(sid, user)
    try:
        with session.lock:
            session.delete_selected_line(
                remove_table_rule=(
                    req.remove_table_rule
                    if req is not None
                    else False
                )
            )
            return session.state()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/{sid}/line/add-row"
)
def add_table_row(
    sid: str,
    req: InsertTableRowRequest | None = None,
    user: UserContext = Depends(_require_user),
):
    session = _get_session(sid, user)
    try:
        with session.lock:
            session.add_selected_row_below(
                position=(req.position if req is not None else "below")
            )
            return session.state()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/{sid}/table-line/delete"
)
def delete_table_line(
    sid: str,
    req: PointRequest,
    user: UserContext = Depends(_require_user),
):
    session = _get_session(sid, user)
    try:
        with session.lock:
            session.delete_table_line_at(req.x, req.y)
            return session.state()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------
# NORMAL RE-CHECK
# NO EXPLICIT OPENAI CALL
# ---------------------------------------------------------

@app.post(
    "/api/{sid}/resolve"
)
def resolve_font(
    sid: str,
    req: TextRequest,
    user: UserContext = Depends(
        _require_user
    ),
):

    session = _get_session(
        sid,
        user,
    )

    try:

        with session.lock:

            session.resolve_selected_font(
                req.replacement
            )

            return (
                session.state()
            )

    except Exception as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------
# AI FONT SEARCH
# OPENAI RUNS ONLY WHEN USER CLICKS AI BUTTON
# ---------------------------------------------------------

@app.post(
    "/api/{sid}/ai-font-search"
)
def ai_font_search(
    sid: str,
    req: TextRequest,
    user: UserContext = Depends(
        _require_user
    ),
):

    session = _get_session(
        sid,
        user,
    )

    try:

        with session.lock:

            session.search_selected_font_with_ai(
                req.replacement
            )

            return (
                session.state()
            )

    except Exception as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


@app.post(
    "/api/{sid}/replace"
)
def replace_text(
    sid: str,
    req: TextRequest,
    user: UserContext = Depends(
        _require_user
    ),
):

    session = _get_session(
        sid,
        user,
    )

    try:

        with session.lock:

            session.replace_selected_text(
                req.replacement,
                expansion_direction=
                req.direction,
                fallback_token=
                req.fallback_token,
            )

            return (
                session.state()
            )

    except Exception as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


@app.post(
    "/api/{sid}/undo"
)
def undo(
    sid: str,
    user: UserContext = Depends(
        _require_user
    ),
):

    session = _get_session(
        sid,
        user,
    )

    try:

        with session.lock:

            session.undo_last()

            return (
                session.state()
            )

    except Exception as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


@app.get(
    "/api/{sid}/export"
)
def export_pdf(
    sid: str,
    user: UserContext = Depends(
        _require_user
    ),
):

    session = _get_session(
        sid,
        user,
    )

    try:

        with session.lock:

            path = (
                session.export()
            )

        return FileResponse(
            path,
            media_type=
            "application/pdf",
            filename=
            path.name,
            headers={
                "Cache-Control":
                "no-store"
            },
        )

    except Exception as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


@app.get(
    "/api/{sid}/audit"
)
def download_audit(
    sid: str,
    user: UserContext = Depends(
        _require_user
    ),
):

    session = _get_session(
        sid,
        user,
    )

    with session.lock:

        if (
            not session.last_audit
            or not
            session.last_audit.exists()
        ):

            raise HTTPException(
                status_code=404,
                detail=(
                    "Export the PDF first."
                ),
            )

        return FileResponse(
            session.last_audit,
            media_type=
            "application/json",
            filename=
            session.last_audit.name,
        )


@app.get(
    "/api/{sid}/validation"
)
def download_validation(
    sid: str,
    user: UserContext = Depends(
        _require_user
    ),
):

    session = _get_session(
        sid,
        user,
    )

    with session.lock:

        if (
            not session.last_validation
            or not
            session.last_validation.exists()
        ):

            raise HTTPException(
                status_code=404,
                detail=(
                    "Export the PDF first."
                ),
            )

        return FileResponse(
            session.last_validation,
            media_type=
            "application/json",
            filename=
            session.last_validation.name,
        )


@app.post(
    "/api/{sid}/close"
)
def close_session_beacon(
    sid: str,
    user: UserContext = Depends(
        _require_user
    ),
):

    with _sessions_lock:

        session = _sessions.get(
            sid
        )

        if (
            session
            and (
                session.owner_id
                == user.user_id
                or user.is_admin
            )
        ):

            session = _sessions.pop(
                sid,
                None,
            )

        else:

            session = None

    if session:

        session.close()

        shutil.rmtree(
            session.session_dir,
            ignore_errors=True,
        )

    return JSONResponse(
        {
            "ok": True
        }
    )


@app.delete(
    "/api/{sid}"
)
def close_session(
    sid: str,
    user: UserContext = Depends(
        _require_user
    ),
):

    with _sessions_lock:

        session = _sessions.get(
            sid
        )

        if (
            session
            and (
                session.owner_id
                == user.user_id
                or user.is_admin
            )
        ):

            session = _sessions.pop(
                sid,
                None,
            )

        else:

            session = None

    if session:

        session.close()

        shutil.rmtree(
            session.session_dir,
            ignore_errors=True,
        )

    return JSONResponse(
        {
            "ok": True
        }
    )
