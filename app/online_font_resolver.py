from __future__ import annotations

import hashlib
import json
import os
import re
import time
import ipaddress
import socket
from html.parser import HTMLParser
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fontTools.ttLib import TTFont

from .font_utils import (
    normalize_font_name,
    read_font_metadata_from_path,
    safe_file_component,
)


FONTSOURCE_API = "https://api.fontsource.org/v1"
OPENAI_RESPONSES_API = "https://api.openai.com/v1/responses"
_ALLOWED_DOWNLOAD_HOSTS = {"cdn.jsdelivr.net"}
_TRUSTED_OPEN_FONT_HOSTS = {
    "cdn.jsdelivr.net",
    "fonts.gstatic.com",
    "raw.githubusercontent.com",
    "github.com",
    "api.fontsource.org",
}


class _FontLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.stylesheets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.casefold() == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag.casefold() == "link" and attrs.get("href"):
            rel = str(attrs.get("rel") or "").casefold()
            if "stylesheet" in rel:
                self.stylesheets.append(attrs["href"])


@dataclass
class OnlineFontResult:
    path: str
    provider: str
    font_id: str
    family: str
    weight: int
    italic: bool
    subset: str
    source_url: str
    sha256: str
    cached: bool = False


class OnlineFontResolver:
    """
    Automatic open-font search / download / cache layer with OpenAI web search.

    The default provider is Fontsource. By default we only accept Fontsource
    records whose type is ``google``. Those fonts are publicly distributed via
    the Google Fonts ecosystem. Set ``AUTO_FONT_ALLOW_FONTSOURCE_OTHER=1`` only
    if your application has a reason to accept Fontsource's non-Google catalog.

    Important safety / fidelity rule:
    an online font is accepted only after exact family-name verification,
    weight/style verification, and Unicode cmap coverage of the requested text.
    The caller should embed the downloaded font as a normal font resource; it
    should not silently splice online glyphs into an unrelated existing subset.
    """

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        enabled: Optional[bool] = None,
        timeout_seconds: float = 10.0,
        max_font_bytes: int = 15 * 1024 * 1024,
    ):
        if enabled is None:
            enabled = os.environ.get("AUTO_FONT_DOWNLOAD", "1").strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }

        self.enabled = bool(enabled)
        self.timeout_seconds = float(timeout_seconds)
        self.max_font_bytes = int(max_font_bytes)
        self.allow_other = os.environ.get(
            "AUTO_FONT_ALLOW_FONTSOURCE_OTHER", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}

        self.openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.openai_model = os.environ.get(
            "OPENAI_FONT_SEARCH_MODEL", "gpt-5.6-luna"
        ).strip() or "gpt-5.6-luna"
        try:
            self.openai_max_output_tokens = max(300, min(4000, int(
                os.environ.get("OPENAI_FONT_SEARCH_MAX_OUTPUT_TOKENS", "1200")
            )))
        except Exception:
            self.openai_max_output_tokens = 1200
        try:
            self.openai_max_retries = max(0, min(5, int(
                os.environ.get("OPENAI_FONT_SEARCH_MAX_RETRIES", "3")
            )))
        except Exception:
            self.openai_max_retries = 3
        self.font_search_provider = os.environ.get(
            "FONT_SEARCH_PROVIDER", "openai"
        ).strip().casefold()
        self.web_search_enabled = os.environ.get(
            "AUTO_FONT_WEB_SEARCH", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
        self.web_download_enabled = os.environ.get(
            "AUTO_FONT_WEB_DOWNLOAD", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
        self.allow_unknown_license = os.environ.get(
            "AUTO_FONT_ALLOW_UNKNOWN_LICENSE", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        try:
            self.web_max_results = max(5, min(60, int(os.environ.get("AUTO_FONT_WEB_MAX_RESULTS", "30"))))
        except Exception:
            self.web_max_results = 30

        default_cache = Path.home() / ".cache" / "pdf_text_browser" / "fonts"
        configured = cache_dir or os.environ.get("FONT_CACHE_DIR")
        self.cache_dir = Path(configured).expanduser() if configured else default_cache
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Prevent repeated provider hits when a family is not available.
        self._negative_cache: dict[tuple, tuple[float, str]] = {}
        self.negative_ttl_seconds = 10 * 60

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_exact(
        self,
        family: str,
        weight: int,
        italic: bool,
        required_text: str,
        *,
        allow_ai: bool = False,
    ) -> tuple[Optional[OnlineFontResult], str]:
        """Resolve an exact font without silently invoking AI.

        ``allow_ai`` defaults to False so ordinary font re-checks never spend
        OpenAI API calls. The explicit "Find Exact Font Online" action should
        pass ``allow_ai=True`` (or call ``find_exact_with_ai`` below).
        """
        if not self.enabled:
            return None, "Automatic open-font download is disabled."

        family = (family or "").strip()
        if not family:
            return None, "No readable font family name is available for online lookup."

        style = "italic" if italic else "normal"
        weight = int(weight or 400)
        notes = []

        for search_family in self._family_search_names(family):
            cache_hit = self._find_cached(
                search_family,
                weight,
                italic,
                required_text,
            )
            if cache_hit:
                return cache_hit, (
                    f"Automatic font lookup: reused verified cached font "
                    f"'{cache_hit.family}' ({cache_hit.weight}, {style})."
                )

            # Keep manual/local checks and explicit AI searches in separate
            # negative-cache buckets. Otherwise a failed non-AI re-check could
            # suppress a later user-requested AI search for ten minutes.
            key = (
                normalize_font_name(search_family),
                weight,
                bool(italic),
                bool(allow_ai),
            )
            cached_failure = self._negative_cache.get(key)
            if cached_failure and time.time() - cached_failure[0] < self.negative_ttl_seconds:
                notes.append(cached_failure[1])
                continue

            provider_notes = []

            try:
                result, note = self._find_from_fontsource(
                    search_family,
                    weight,
                    italic,
                    required_text,
                )
            except Exception as exc:
                note = f"Fontsource lookup failed for '{search_family}': {exc}"
                result = None

            if result:
                return result, note
            if note:
                provider_notes.append(note)

            # AI/web search is opt-in only. Normal "Re-check Font Match" calls
            # arrive here with allow_ai=False and therefore never call OpenAI.
            if allow_ai and self.web_search_enabled:
                try:
                    web_result, web_note = self._find_from_web_search(
                        search_family,
                        weight,
                        italic,
                        required_text,
                    )
                except Exception as exc:
                    web_result = None
                    web_note = f"Web font search failed for '{search_family}': {exc}"

                if web_result:
                    return web_result, web_note
                if web_note:
                    provider_notes.append(web_note)

            combined_note = " ".join(dict.fromkeys(n for n in provider_notes if n))
            self._negative_cache[key] = (time.time(), combined_note)
            notes.append(combined_note)

        return None, " ".join(dict.fromkeys(note for note in notes if note))

    def find_exact_with_ai(
        self,
        family: str,
        weight: int,
        italic: bool,
        required_text: str,
    ) -> tuple[Optional[OnlineFontResult], str]:
        """Explicit user-triggered online/AI lookup."""
        return self.find_exact(
            family,
            weight,
            italic,
            required_text,
            allow_ai=True,
        )

    # ------------------------------------------------------------------
    # Fontsource provider
    # ------------------------------------------------------------------

    def _find_from_fontsource(
        self,
        family: str,
        weight: int,
        italic: bool,
        required_text: str,
    ) -> tuple[Optional[OnlineFontResult], str]:
        query = urllib.parse.urlencode({"family": family})
        listing = self._get_json(f"{FONTSOURCE_API}/fonts?{query}")

        if not isinstance(listing, list):
            return None, "Fontsource returned an unexpected search response."

        wanted = normalize_font_name(family)
        exact = [
            record
            for record in listing
            if normalize_font_name(str(record.get("family") or "")) == wanted
        ]

        if not exact:
            return None, f"Fontsource has no exact family named '{family}'."

        allowed_types = {"google"}
        if self.allow_other:
            allowed_types.add("other")

        exact = [
            record for record in exact
            if str(record.get("type") or "").casefold() in allowed_types
        ]
        if not exact:
            return None, (
                f"Fontsource found '{family}', but it is outside the currently "
                "allowed open-font catalog."
            )

        style_name = "italic" if italic else "normal"

        # Prefer a record that explicitly advertises the exact requested static
        # weight and style. Do not silently substitute a neighboring weight.
        usable = []
        for record in exact:
            weights = {int(v) for v in (record.get("weights") or []) if str(v).isdigit()}
            styles = {str(v).casefold() for v in (record.get("styles") or [])}
            if weight in weights and style_name in styles:
                usable.append(record)

        if not usable:
            return None, (
                f"Fontsource has '{family}', but not the exact requested "
                f"weight/style ({weight}, {style_name})."
            )

        record = usable[0]
        font_id = str(record.get("id") or "").strip()
        if not font_id:
            return None, f"Fontsource record for '{family}' has no usable font ID."

        details = self._get_json(
            f"{FONTSOURCE_API}/fonts/{urllib.parse.quote(font_id, safe='')}"
        )
        if not isinstance(details, dict):
            return None, f"Fontsource details for '{family}' were unavailable."

        # Re-check family and catalog type on the detailed record too.
        details_family = str(details.get("family") or "").strip()
        if normalize_font_name(details_family) != wanted:
            return None, (
                "Fontsource detail response did not preserve the exact family name; "
                "the download was rejected."
            )

        detail_type = str(details.get("type") or record.get("type") or "").casefold()
        if detail_type not in allowed_types:
            return None, "Fontsource font type is not permitted by this resolver."

        urls = self._variant_ttf_urls(details, weight, style_name)
        if not urls:
            return None, (
                f"Fontsource has '{family}' {weight} {style_name}, but exposes no "
                "static TTF URL for that variant."
            )

        last_failure = ""
        for subset, url in urls:
            try:
                result, failure = self._download_and_verify(
                    url=url,
                    font_id=font_id,
                    provider_family=details_family,
                    expected_family=family,
                    weight=weight,
                    italic=italic,
                    subset=subset,
                    required_text=required_text,
                )
            except Exception as exc:
                result = None
                failure = f"{subset}: {exc}"

            if result:
                return result, (
                    f"Automatic font lookup: downloaded and verified open font "
                    f"'{result.family}' from Fontsource; subset={result.subset}, "
                    f"weight={result.weight}, style={style_name}."
                )
            last_failure = failure or last_failure

        return None, (
            f"Fontsource found '{family}', but none of its TTF subsets contained "
            f"all required replacement glyphs with the requested weight/style. "
            + (last_failure or "")
        ).strip()

    def _variant_ttf_urls(self, details: dict, weight: int, style: str):
        variants = details.get("variants") or {}
        weight_node = variants.get(str(weight)) or variants.get(weight) or {}
        style_node = weight_node.get(style) or {}
        if not isinstance(style_node, dict):
            return []

        default_subset = str(details.get("defSubset") or "").strip()
        priority = [default_subset, "latin", "latin-ext"]

        def subset_rank(name: str):
            try:
                return priority.index(name)
            except ValueError:
                return len(priority) + 1

        results = []
        for subset, payload in style_node.items():
            if not isinstance(payload, dict):
                continue
            url_data = payload.get("url") or {}
            if not isinstance(url_data, dict):
                continue
            ttf_url = str(url_data.get("ttf") or "").strip()
            if not ttf_url:
                continue
            if not self._download_host_allowed(ttf_url):
                continue
            results.append((str(subset), ttf_url))

        results.sort(key=lambda item: (subset_rank(item[0]), item[0]))
        return results

    # ------------------------------------------------------------------
    # Broad web discovery (OpenAI Responses API web search + shallow crawler)
    # ------------------------------------------------------------------

    def _find_from_web_search(
        self,
        family: str,
        weight: int,
        italic: bool,
        required_text: str,
    ) -> tuple[Optional[OnlineFontResult], str]:
        if self.font_search_provider != "openai":
            return None, (
                "Broad web font search provider is not set to OpenAI. "
                "Set FONT_SEARCH_PROVIDER=openai."
            )
        if not self.openai_api_key:
            return None, (
                "Broad web font search is enabled, but OPENAI_API_KEY is not set."
            )
        if not self.web_download_enabled:
            return None, (
                "Broad web search is enabled, but automatic web font download is disabled."
            )

        style = "italic" if italic else "normal"

        try:
            result_urls, search_note = self._openai_web_search(
                family=family,
                weight=weight,
                italic=italic,
                required_text=required_text,
                max_sources=self.web_max_results,
            )
        except Exception as exc:
            return None, f"OpenAI web font search failed for '{family}': {exc}"

        if not result_urls:
            return None, (
                f"OpenAI web search found no candidate pages or font files for '{family}'. "
                + (search_note or "")
            ).strip()

        candidate_urls = []
        candidate_seen = set()

        def add_candidate(url):
            if not url or url in candidate_seen:
                return
            if not self._public_url_allowed(url):
                return
            lower = urllib.parse.urlparse(url).path.casefold()
            if not lower.endswith((".ttf", ".otf")):
                return
            candidate_seen.add(url)
            candidate_urls.append(url)

        for url in result_urls:
            direct = self._normalize_direct_font_url(url)
            if direct:
                add_candidate(direct)
                continue

            try:
                for discovered in self._crawl_page_for_font_links(url):
                    add_candidate(discovered)
                    if len(candidate_urls) >= self.web_max_results:
                        break
            except Exception:
                continue

            if len(candidate_urls) >= self.web_max_results:
                break

        if not candidate_urls:
            return None, (
                f"OpenAI web search found {len(result_urls)} source page(s) for '{family}', "
                "but no safe direct TTF/OTF links could be discovered without executing "
                "site scripts. "
                + (search_note or "")
            ).strip()

        failures = []
        for index, url in enumerate(candidate_urls[: self.web_max_results], start=1):
            try:
                result, note = self._download_and_verify_web(
                    url=url,
                    expected_family=family,
                    weight=weight,
                    italic=italic,
                    required_text=required_text,
                    candidate_index=index,
                )
            except Exception as exc:
                result = None
                note = f"{urllib.parse.urlparse(url).hostname or 'candidate'}: {exc}"

            if result:
                return result, (
                    f"OpenAI web search found, downloaded, license-checked and verified "
                    f"'{result.family}' ({result.weight}, {style}) from {result.provider}."
                )
            if note:
                failures.append(note)

        preview = " | ".join(failures[:4])
        return None, (
            f"OpenAI web search inspected {len(candidate_urls)} direct font candidate(s) for "
            f"'{family}', but none passed exact family, weight/style, glyph and license checks. "
            + preview
        ).strip()

    def _openai_web_search(
        self,
        *,
        family: str,
        weight: int,
        italic: bool,
        required_text: str,
        max_sources: int = 30,
    ):
        """Use the OpenAI Responses API web_search tool and return source URLs.

        The API key stays server-side in OPENAI_API_KEY. We request the complete
        web_search source list, then this resolver crawls those public source pages
        for direct TTF/OTF links and applies the existing verification policy.
        """
        if not self.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")

        style = "italic" if italic else "normal"
        required_preview = "".join(dict.fromkeys(ch for ch in required_text if not ch.isspace()))[:80]
        prompt = (
            "Search the public web for the exact font family below. Find legitimate source "
            "pages or direct .ttf/.otf files. Prioritize the font designer/foundry, official "
            "project repositories, Google Fonts/Fontsource, GitHub repositories, and other "
            "sources that clearly identify the font and its license. Do not invent URLs.\n\n"
            f"Font family: {family}\n"
            f"Requested weight: {int(weight)}\n"
            f"Requested style: {style}\n"
            f"Required characters: {required_preview!r}\n\n"
            "Search for exact-name matches first. Include pages that may contain downloadable "
            "TTF or OTF files. The application will independently verify family metadata, "
            "weight, style, glyph coverage, embedding policy, and license before using a file."
        )

        payload = {
            "model": self.openai_model,
            "tools": [{"type": "web_search"}],
            "tool_choice": "auto",
            "include": ["web_search_call.action.sources"],
            # This lookup only needs a short answer plus source URLs. Limiting
            # output prevents a small search from reserving tens of thousands
            # of TPM and triggering avoidable HTTP 429 errors.
            "max_output_tokens": self.openai_max_output_tokens,
            "reasoning": {"effort": "low"},
            "input": prompt,
        }
        raw_payload = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            OPENAI_RESPONSES_API,
            data=raw_payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.openai_api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "pdf-text-browser/auto-font-resolver",
            },
        )

        # Web search can take longer than a normal JSON lookup. Give it a sane
        # floor while still respecting a larger configured timeout.
        request_timeout = max(30.0, self.timeout_seconds)
        raw = None

        for attempt in range(self.openai_max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=request_timeout) as response:
                    raw = response.read(8 * 1024 * 1024 + 1)
                break

            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read(8192)
                except Exception:
                    pass

                detail = ""
                if body:
                    try:
                        error_payload = json.loads(body.decode("utf-8", errors="ignore"))
                        detail = str(
                            (error_payload.get("error") or {}).get("message") or ""
                        ).strip()
                    except Exception:
                        detail = body.decode("utf-8", errors="ignore").strip()

                # Retry rate limits and transient server errors. Prefer the
                # server's Retry-After header; otherwise use the delay included
                # in the OpenAI error text, then exponential backoff.
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if retryable and attempt < self.openai_max_retries:
                    delay = None
                    try:
                        header_value = exc.headers.get("Retry-After")
                        if header_value:
                            delay = float(header_value)
                    except Exception:
                        delay = None

                    if delay is None and detail:
                        match = re.search(
                            r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*(ms|s)",
                            detail,
                            flags=re.I,
                        )
                        if match:
                            delay = float(match.group(1))
                            if match.group(2).casefold() == "ms":
                                delay /= 1000.0

                    if delay is None:
                        delay = min(8.0, 1.0 * (2 ** attempt))

                    time.sleep(max(0.25, min(10.0, delay + 0.15)))
                    continue

                suffix = f": {detail}" if detail else ""
                raise RuntimeError(
                    f"OpenAI Responses API HTTP {exc.code}{suffix}"
                ) from exc

            except urllib.error.URLError as exc:
                if attempt < self.openai_max_retries:
                    time.sleep(min(8.0, 1.0 * (2 ** attempt)))
                    continue
                raise RuntimeError(
                    f"OpenAI Responses API network error: {exc.reason}"
                ) from exc

        if raw is None:
            raise RuntimeError("OpenAI Responses API returned no response after retries")

        if len(raw) > 8 * 1024 * 1024:
            raise RuntimeError("OpenAI Responses API response was unexpectedly large")

        try:
            response_payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("OpenAI Responses API returned invalid JSON") from exc

        urls = []
        seen = set()

        def add_url(value):
            value = str(value or "").strip()
            if not value or value in seen:
                return
            parsed = urllib.parse.urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return
            seen.add(value)
            urls.append(value)

        # Preferred source list requested by include=[web_search_call.action.sources].
        for item in response_payload.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "web_search_call":
                action = item.get("action") or {}
                for source in action.get("sources") or []:
                    if isinstance(source, dict):
                        add_url(source.get("url"))

            # Also collect URL citations from the assistant message as a fallback
            # because SDK/API versions can expose slightly different source shapes.
            if item.get("type") == "message":
                for content in item.get("content") or []:
                    if not isinstance(content, dict):
                        continue
                    for annotation in content.get("annotations") or []:
                        if not isinstance(annotation, dict):
                            continue
                        if annotation.get("type") == "url_citation":
                            add_url(annotation.get("url"))
                            citation = annotation.get("url_citation")
                            if isinstance(citation, dict):
                                add_url(citation.get("url"))

        max_sources = max(1, min(int(max_sources or 30), 60))
        urls = urls[:max_sources]

        note = (
            f"OpenAI web search ({self.openai_model}) returned {len(urls)} source URL(s) "
            f"for '{family}'."
        )
        return urls, note

    @staticmethod
    def _normalize_direct_font_url(url: str) -> Optional[str]:
        try:
            parsed = urllib.parse.urlparse(url)
            path = parsed.path or ""
            if path.casefold().endswith((".ttf", ".otf")):
                if parsed.hostname and parsed.hostname.casefold() == "github.com" and "/blob/" in path:
                    parts = path.strip("/").split("/")
                    if len(parts) >= 5 and parts[2] == "blob":
                        org, repo, _, branch = parts[:4]
                        rest = "/".join(parts[4:])
                        return f"https://raw.githubusercontent.com/{org}/{repo}/{branch}/{rest}"
                return url
        except Exception:
            pass
        return None

    def _crawl_page_for_font_links(self, page_url: str):
        if not self._public_url_allowed(page_url):
            return []

        raw, final_url, content_type = self._fetch_public_resource(
            page_url,
            max_bytes=1_500_000,
            accepted_prefixes=("text/html", "text/css", "text/plain"),
        )
        text = raw.decode("utf-8", errors="ignore")
        base = final_url
        found = []

        pattern = r'''(?i)(?:url\(|href\s*=\s*["'])([^"')\s>]+\.(?:ttf|otf)(?:\?[^"')\s>]*)?)'''
        for match in re.findall(pattern, text):
            found.append(urllib.parse.urljoin(base, match.strip(" '\"")))

        if content_type.startswith("text/html"):
            parser = _FontLinkParser()
            try:
                parser.feed(text)
            except Exception:
                pass
            for href in parser.links:
                absolute = urllib.parse.urljoin(base, href)
                if urllib.parse.urlparse(absolute).path.casefold().endswith((".ttf", ".otf")):
                    found.append(absolute)

            for href in parser.stylesheets[:4]:
                css_url = urllib.parse.urljoin(base, href)
                if not self._public_url_allowed(css_url):
                    continue
                try:
                    css_raw, css_final, _ = self._fetch_public_resource(
                        css_url,
                        max_bytes=750_000,
                        accepted_prefixes=("text/css", "text/plain"),
                    )
                    css = css_raw.decode("utf-8", errors="ignore")
                    css_pattern = r'''(?i)url\(([^)]+\.(?:ttf|otf)(?:\?[^)]*)?)\)'''
                    for match in re.findall(css_pattern, css):
                        value = match.strip().strip("'\"")
                        found.append(urllib.parse.urljoin(css_final, value))
                except Exception:
                    pass

        output = []
        seen = set()
        for url in found:
            direct = self._normalize_direct_font_url(url) or url
            if direct in seen or not self._public_url_allowed(direct):
                continue
            if urllib.parse.urlparse(direct).path.casefold().endswith((".ttf", ".otf")):
                seen.add(direct)
                output.append(direct)
        return output

    def _download_and_verify_web(
        self,
        *,
        url: str,
        expected_family: str,
        weight: int,
        italic: bool,
        required_text: str,
        candidate_index: int,
    ) -> tuple[Optional[OnlineFontResult], str]:
        if not self._public_url_allowed(url):
            return None, "Web candidate URL was not a public HTTPS/HTTP address."

        parsed = urllib.parse.urlparse(url)
        ext = Path(parsed.path).suffix.casefold()
        if ext not in {".ttf", ".otf"}:
            return None, "Web candidate was not a direct TTF/OTF file."

        family_id = safe_file_component(normalize_font_name(expected_family) or expected_family)
        family_dir = self.cache_dir / "web" / family_id
        family_dir.mkdir(parents=True, exist_ok=True)
        style = "italic" if italic else "normal"
        destination = family_dir / f"candidate-{candidate_index}-{weight}-{style}{ext}"

        self._download_public_file(url, destination)
        ok, note, sha = self._verify_font_file(
            destination,
            expected_family,
            weight,
            italic,
            required_text,
            require_open_license=True,
            source_url=url,
        )
        if not ok:
            try:
                destination.unlink()
            except Exception:
                pass
            return None, note

        host = (parsed.hostname or "web").casefold()
        result = OnlineFontResult(
            path=str(destination),
            provider=f"Web/{host}",
            font_id=f"web-{sha[:16]}",
            family=expected_family,
            weight=weight,
            italic=italic,
            subset="web",
            source_url=url,
            sha256=sha,
            cached=False,
        )
        self._write_sidecar(result)
        return result, note

    def _fetch_public_resource(self, url: str, *, max_bytes: int, accepted_prefixes):
        if not self._public_url_allowed(url):
            raise RuntimeError("URL is not an allowed public web address")
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 pdf-text-browser-font-bot/1.0",
                "Accept": "text/html,text/css,text/plain,*/*;q=0.2",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            final_url = response.geturl()
            if not self._public_url_allowed(final_url):
                raise RuntimeError("redirect left the public web")
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
            if accepted_prefixes and not any(content_type.startswith(prefix) for prefix in accepted_prefixes):
                raise RuntimeError(f"unexpected content type: {content_type or 'unknown'}")
            raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise RuntimeError("web resource exceeded size limit")
        return raw, final_url, content_type

    def _download_public_file(self, url: str, destination: Path):
        if not self._public_url_allowed(url):
            raise RuntimeError("download URL is not public")
        part = destination.with_suffix(destination.suffix + ".part")
        total = 0
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 pdf-text-browser-font-bot/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                final_url = response.geturl()
                if not self._public_url_allowed(final_url):
                    raise RuntimeError("download redirect left the public web")
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > self.max_font_bytes:
                            raise RuntimeError("font file is larger than the configured limit")
                    except ValueError:
                        pass
                with part.open("wb") as fh:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.max_font_bytes:
                            raise RuntimeError("font file exceeded the configured size limit")
                        fh.write(chunk)
            magic = part.read_bytes()[:4]
            if magic not in {b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"}:
                raise RuntimeError("downloaded file is not a recognizable TTF/OTF font")
            part.replace(destination)
        finally:
            if part.exists():
                try:
                    part.unlink()
                except Exception:
                    pass

    def _public_url_allowed(self, url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in {"https", "http"}:
                return False
            host = (parsed.hostname or "").strip().casefold()
            if not host or host in {"localhost", "localhost.localdomain"}:
                return False
            addresses = []
            try:
                addresses.append(ipaddress.ip_address(host))
            except ValueError:
                try:
                    for info in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM):
                        addresses.append(ipaddress.ip_address(info[4][0]))
                except Exception:
                    return False
            for addr in addresses:
                if (
                    addr.is_private
                    or addr.is_loopback
                    or addr.is_link_local
                    or addr.is_multicast
                    or addr.is_reserved
                    or addr.is_unspecified
                ):
                    return False
            return True
        except Exception:
            return False

    def _font_license_status(self, path: Path, source_url: str = ""):
        font = None
        try:
            font = TTFont(str(path), lazy=True)
            fs_type = 0
            if "OS/2" in font:
                fs_type = int(getattr(font["OS/2"], "fsType", 0) or 0)
            if fs_type & 0x0002:
                return False, "font declares restricted-license embedding (OS/2 fsType bit 1)"

            name_records = []
            if "name" in font:
                for record in font["name"].names:
                    if int(getattr(record, "nameID", -1)) not in {13, 14, 0, 7}:
                        continue
                    try:
                        value = record.toUnicode()
                    except Exception:
                        try:
                            value = record.string.decode("utf-8", errors="ignore")
                        except Exception:
                            value = ""
                    if value:
                        name_records.append(value)
            license_text = " ".join(name_records).casefold()
            open_markers = (
                "open font license",
                "sil open font",
                "apache license",
                "apache 2.0",
                "ubuntu font licence",
                "ubuntu font license",
                "public domain",
                "mit license",
            )
            if any(marker in license_text for marker in open_markers):
                return True, "font metadata declares an open/redistributable license"

            host = (urllib.parse.urlparse(source_url).hostname or "").casefold()
            normalized_url = source_url.casefold()
            if host == "fonts.gstatic.com":
                return True, "Google Fonts delivery host"
            if host == "raw.githubusercontent.com" and "/google/fonts/" in normalized_url:
                return True, "Google Fonts GitHub repository"
            if host == "cdn.jsdelivr.net" and ("@fontsource/" in normalized_url or "/fontsource/" in normalized_url):
                return True, "Fontsource CDN package"

            if self.allow_unknown_license:
                return True, "license metadata was unknown, allowed by AUTO_FONT_ALLOW_UNKNOWN_LICENSE=1"
            return False, "font license could not be verified as open/redistributable"
        except Exception as exc:
            return False, f"font license metadata could not be inspected: {exc}"
        finally:
            if font is not None:
                try:
                    font.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Verification / cache
    # ------------------------------------------------------------------

    def _download_and_verify(
        self,
        *,
        url: str,
        font_id: str,
        provider_family: str,
        expected_family: str,
        weight: int,
        italic: bool,
        subset: str,
        required_text: str,
    ) -> tuple[Optional[OnlineFontResult], str]:
        if not self._download_host_allowed(url):
            return None, "Provider returned a download URL on an unapproved host."

        style = "italic" if italic else "normal"
        family_dir = self.cache_dir / safe_file_component(font_id or expected_family)
        family_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{safe_file_component(subset)}-{weight}-{style}.ttf"
        destination = family_dir / filename

        if destination.exists():
            ok, note, sha = self._verify_font_file(
                destination,
                expected_family,
                weight,
                italic,
                required_text,
            )
            if ok:
                return OnlineFontResult(
                    path=str(destination),
                    provider="Fontsource",
                    font_id=font_id,
                    family=provider_family,
                    weight=weight,
                    italic=italic,
                    subset=subset,
                    source_url=url,
                    sha256=sha,
                    cached=True,
                ), note
            try:
                destination.unlink()
            except Exception:
                pass

        self._download_file(url, destination)
        ok, note, sha = self._verify_font_file(
            destination,
            expected_family,
            weight,
            italic,
            required_text,
        )
        if not ok:
            try:
                destination.unlink()
            except Exception:
                pass
            return None, note

        result = OnlineFontResult(
            path=str(destination),
            provider="Fontsource",
            font_id=font_id,
            family=provider_family,
            weight=weight,
            italic=italic,
            subset=subset,
            source_url=url,
            sha256=sha,
            cached=False,
        )
        self._write_sidecar(result)
        return result, note

    def _find_cached(
        self,
        expected_family: str,
        weight: int,
        italic: bool,
        required_text: str,
    ) -> Optional[OnlineFontResult]:
        wanted = normalize_font_name(expected_family)
        style = "italic" if italic else "normal"

        for sidecar in self.cache_dir.rglob("*.json"):
            try:
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                continue

            if normalize_font_name(str(payload.get("family") or "")) != wanted:
                continue
            if int(payload.get("weight") or 0) != int(weight):
                continue
            if bool(payload.get("italic")) != bool(italic):
                continue

            path = sidecar.with_suffix(".ttf")
            if not path.exists():
                continue

            ok, _, sha = self._verify_font_file(
                path,
                expected_family,
                weight,
                italic,
                required_text,
            )
            if not ok:
                continue

            return OnlineFontResult(
                path=str(path),
                provider=str(payload.get("provider") or "Fontsource"),
                font_id=str(payload.get("font_id") or path.parent.name),
                family=str(payload.get("family") or expected_family),
                weight=weight,
                italic=italic,
                subset=str(payload.get("subset") or "cached"),
                source_url=str(payload.get("source_url") or ""),
                sha256=sha,
                cached=True,
            )

        return None

    def _verify_font_file(
        self,
        path: Path,
        expected_family: str,
        expected_weight: int,
        expected_italic: bool,
        required_text: str,
        require_open_license: bool = False,
        source_url: str = "",
    ) -> tuple[bool, str, str]:
        try:
            meta = read_font_metadata_from_path(str(path)) or {}
        except Exception as exc:
            return False, f"Downloaded TTF metadata could not be read: {exc}", ""

        actual_family = str(meta.get("family") or "").strip()
        if normalize_font_name(actual_family) != normalize_font_name(expected_family):
            return False, (
                f"Downloaded font family mismatch: expected '{expected_family}', "
                f"got '{actual_family or '?'}'."
            ), ""

        actual_weight = int(meta.get("weight") or 400)
        if abs(actual_weight - int(expected_weight)) > 100:
            return False, (
                f"Downloaded font weight mismatch: expected {expected_weight}, "
                f"got {actual_weight}."
            ), ""

        actual_italic = bool(meta.get("italic"))
        if actual_italic != bool(expected_italic):
            return False, (
                f"Downloaded font style mismatch: expected "
                f"{'italic' if expected_italic else 'normal'}."
            ), ""

        glyphs_ok, missing = self._font_has_text(path, required_text)
        if not glyphs_ok:
            preview = " ".join(repr(ch) for ch in missing[:15])
            return False, f"Downloaded TTF/OTF is missing required glyphs: {preview}", ""

        if require_open_license:
            license_ok, license_note = self._font_license_status(path, source_url)
            if not license_ok:
                return False, f"Downloaded font rejected: {license_note}.", ""

        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        return True, "Downloaded font passed family, weight, style, cmap and required policy checks.", sha

    @staticmethod
    def _font_has_text(path: Path, text: str):
        if text == "":
            return True, []

        font = None
        try:
            font = TTFont(str(path), lazy=True)
            codepoints = set()
            if "cmap" in font:
                for table in font["cmap"].tables:
                    try:
                        if table.isUnicode():
                            codepoints.update((table.cmap or {}).keys())
                    except Exception:
                        continue
            if not codepoints:
                codepoints.update((font.getBestCmap() or {}).keys())

            missing = []
            seen = set()
            for ch in text:
                if ch.isspace() or ch in "\r\n\t" or ch in seen:
                    continue
                seen.add(ch)
                if ord(ch) not in codepoints:
                    missing.append(ch)
            return not missing, missing
        except Exception:
            missing = [
                ch for ch in dict.fromkeys(text)
                if not ch.isspace() and ch not in "\r\n\t"
            ]
            return False, missing
        finally:
            if font is not None:
                try:
                    font.close()
                except Exception:
                    pass

    def _write_sidecar(self, result: OnlineFontResult):
        path = Path(result.path)
        payload = {
            "provider": result.provider,
            "font_id": result.font_id,
            "family": result.family,
            "weight": result.weight,
            "italic": result.italic,
            "subset": result.subset,
            "source_url": result.source_url,
            "sha256": result.sha256,
            "downloaded_at_unix": int(time.time()),
        }
        try:
            path.with_suffix(".json").write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            # Cache metadata is useful but not required for the verified font itself.
            pass

    # ------------------------------------------------------------------
    # HTTP / name helpers
    # ------------------------------------------------------------------

    def _get_json(self, url: str):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "pdf-text-browser/auto-font-resolver",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                if int(getattr(response, "status", 200)) != 200:
                    raise RuntimeError(f"HTTP {getattr(response, 'status', '?')}")
                raw = response.read(5 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"network error: {exc.reason}") from exc

        if len(raw) > 5 * 1024 * 1024:
            raise RuntimeError("provider JSON response was unexpectedly large")
        return json.loads(raw.decode("utf-8"))

    def _download_file(self, url: str, destination: Path):
        if not self._download_host_allowed(url):
            raise RuntimeError("download host is not approved")

        request = urllib.request.Request(
            url,
            headers={"User-Agent": "pdf-text-browser/auto-font-resolver"},
        )
        part = destination.with_suffix(destination.suffix + ".part")
        total = 0
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                final_url = response.geturl()
                if not self._download_host_allowed(final_url):
                    raise RuntimeError("download redirect left the approved CDN")

                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > self.max_font_bytes:
                            raise RuntimeError("font file is larger than the configured limit")
                    except ValueError:
                        pass

                with part.open("wb") as fh:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.max_font_bytes:
                            raise RuntimeError("font file exceeded the configured size limit")
                        fh.write(chunk)
            part.replace(destination)
        finally:
            if part.exists():
                try:
                    part.unlink()
                except Exception:
                    pass

    @staticmethod
    def _download_host_allowed(url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            return parsed.scheme == "https" and (parsed.hostname or "").casefold() in _ALLOWED_DOWNLOAD_HOSTS
        except Exception:
            return False

    @staticmethod
    def _family_search_names(family: str):
        value = re.sub(r"\s+", " ", (family or "").replace("_", " ")).strip()
        candidates = [value]

        # PDF BaseFont names often append style to the family name while normal
        # TTF metadata stores style separately. Try only conservative trailing
        # style removals; do not broaden to unrelated families.
        stripped = re.sub(
            r"(?i)(?:[-\s]+)(?:thin|extralight|ultralight|light|regular|book|"
            r"medium|semibold|demibold|bold|extrabold|ultrabold|black|heavy|"
            r"italic|oblique)$",
            "",
            value,
        ).strip()
        if stripped and normalize_font_name(stripped) != normalize_font_name(value):
            candidates.append(stripped)

        # Some producer names concatenate the style suffix, e.g. RobotoBold.
        compact = re.sub(
            r"(?i)(thin|extralight|ultralight|light|regular|medium|semibold|"
            r"demibold|bold|extrabold|ultrabold|black|heavy|italic|oblique)$",
            "",
            value,
        ).strip(" -_")
        if compact and all(
            normalize_font_name(compact) != normalize_font_name(item)
            for item in candidates
        ):
            candidates.append(compact)

        return candidates
