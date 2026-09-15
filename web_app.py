from __future__ import annotations

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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
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
from app.docx_reference import DocxReferenceIndex
from app.env import load_env_file
from app.font_resolver import FontResolver, humanize_pdf_font_name
from app.online_font_resolver import OnlineFontResolver
from app.font_utils import (
    font_flags_text,
    strip_subset_prefix,
    weight_label,
    weight_from_flags,
    italic_from_flags,
)
from app.pdf_engine import PdfEngine


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

        self.selected_docx_ref = None

        self.current_resolution = None

        self.undo_stack: list[
            tuple[bytes, int]
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

    def clear_selection(self):

        self.selected_span = None

        self.selected_docx_ref = None

        self.current_resolution = None

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

        self.selected_span = span

        self.populate_docx_reference()

        # Normal PDF/font check only.
        # No AI is intentionally called here.
        self.resolve_selected_font(
            span.text
        )

        self.status = (
            "Text selected."
        )

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
        )

    def replace_selected_text(
        self,
        replacement: str,
        expansion_direction: str = "right",
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

                try:

                    snapshot = (
                        self.doc.tobytes(
                            garbage=3,
                            deflate=True,
                        )
                    )

                except TypeError:

                    snapshot = (
                        self.doc.tobytes()
                    )

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
                    "clean-redaction-horizontal",
                )

                self.selected_span = (
                    updated_span
                )

                self.refresh_spans()

                moved_count = len(
                    shifted_items
                )

                self.match_status = (
                    "✓ CLEAN REPLACEMENT APPLIED\n"
                    f"Font size kept at "
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

            try:

                snapshot = (
                    self.doc.tobytes(
                        garbage=3,
                        deflate=True,
                    )
                )

            except TypeError:

                snapshot = (
                    self.doc.tobytes()
                )

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
                "clean-redaction-multiline",
            )

            self.selected_span = (
                updated_span
            )

            self.refresh_spans()

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

        previous, edit_count = (
            self.undo_stack.pop()
        )

        if self.edit_session:

            self.edit_session.truncate_edits(
                edit_count
            )

        if self.doc:
            self.doc.close()

        self.doc = pymupdf.open(
            stream=previous,
            filetype="pdf",
        )

        self.clear_selection()

        self.refresh_spans()

    def undo_last(self):

        if not self.undo_stack:

            raise ValueError(
                "There is no replacement "
                "to undo."
            )

        previous, edit_count = (
            self.undo_stack.pop()
        )

        if self.edit_session:

            self.edit_session.truncate_edits(
                edit_count
            )

        if self.doc:
            self.doc.close()

        self.doc = pymupdf.open(
            stream=previous,
            filetype="pdf",
        )

        self.clear_selection()

        self.refresh_spans()

        self.status = (
            "Last replacement undone."
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

        x0, y0, x1, y1 = (
            span.bbox
        )

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
            span.text,
            "bbox":
            list(span.bbox),
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