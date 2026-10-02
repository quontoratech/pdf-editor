# PDF Text Style Editor — Browser/VPS Edition

This package keeps the existing PDF engine modules under `app/` unchanged and replaces the runtime frontend with a browser interface.

## Local run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Open:

```text
http://127.0.0.1:8000
```

The first screen is a login page. MongoDB connection settings are read from `.env`:

```text
MONGO_URI=...
MONGO_DB_NAME=pdf_text_browser
```

Login users are read from the MongoDB `users` collection. A document with `role: "admin"` sees `Make User` and `All Users` in the editor toolbar. Normal users only see the PDF tool after login.

The server listens on `0.0.0.0:8000` by default. Change it with:

```bash
PDF_EDITOR_PORT=9000 python main.py
```

## Browser features

- Open/upload PDF
- Render pages in the browser
- Click PDF text to select it
- Previous / next page
- Zoom 60–250%
- Load DOCX reference
- Upload an exact TTF/OTF font and persist it in the MongoDB font library
- Find verified same-family glyphs in one or more subsets elsewhere in the PDF
- Complete an original Type1C/CFF subset from PDF or MongoDB glyph donors, with measured target-weight calibration
- Convert missing-glyph replacements to audited vector outlines
- Re-check exact font match
- Clean text replacement using the existing `PdfEngine`
- Horizontal and multiline reflow using the existing engine
- Full-row deletion that moves independent horizontal table rules with the remaining rows
- **Add Full Row Below** clones a selected table row as an exact template; 2, 3, 4, 5, or more columns are detected automatically and retain their original widths, alignment, searchable text, font resources, and separator style
- Standalone **Delete Table Line** mode for removing safe horizontal rules after their text is already gone
- Undo last replacement
- Save/export PDF using the existing `CleanEditSession`
- Download audit and validation JSON sidecars

To insert a row, click any cell in the row whose layout should be reused, choose
**Select Full Line**, then choose **Add Full Row Below**. The button shows the
detected column count. The cloned cells deliberately start with the template
values so their exact embedded/subset-font content remains selectable; click
each cloned cell and use the normal clean replacement workflow to enter the new
values. Later rows and safe horizontal rules move down by the table's measured
row height. A fixed footer or later unrelated section is not moved.

Structural row edits keep a validated in-memory hit-test index instead of
recursively rebuilding a text dictionary through every clipped full-page Form.
This removes the multi-minute **Deleting full text line…** wait seen on smaller
VPS CPUs while preserving the same immediate searchable-text and final PDF
structure checks.
- Browser full-screen mode

Vector-outline fallback is intentionally non-searchable and non-selectable. It
redacts the source text, draws filled glyph contours from a compatible saved
TTF/OTF, and does not embed a replacement font resource. Catalog/Info/XMP, xref
style, compression policy, and untouched pages remain covered by the normal
structure-preservation checks.

For embedded Type1C/CFF fonts, an exact-family MongoDB donor is not substituted
for the whole line. The editor appends only glyph programs missing from the
original subset and writes the replacement through that original `/F#` resource.
Before changing the font stream, an exact donor must pass an outline fingerprint
against glyphs already present in the original subset. If an exact missing glyph
is unavailable, a compatible lighter donor from another embedded PDF CFF or the
font database can be calibrated against at least eight shared target glyphs.
FreeType emboldens only the missing outline, while measured scale, position,
shape moments, ink density and advance-width mapping make it match the target
subset's actual geometry (for example, a 700-weight target). Donors outside the
strict geometry thresholds are rejected before PDF mutation. Existing target
glyph programs are never redrawn, and glyphs already present but not yet encoded
use their intrinsic CFF advance widths rather than widths borrowed from a donor.
When no safe donor exists, obtain the licensed full TTF/OTF for the exact face
and weight, upload it with **Upload Exact Font**, and run **Re-check Font Match**.
The file must contain the reported missing glyphs and pass the embedded-subset
outline fingerprint; renaming a similar font does not make it compatible.
The explicit OpenAI exact-font search follows the same path when it finds and
license-verifies the correct full face; OpenAI is not used to invent arbitrary
font outlines.

## VPS deployment

Recommended Ubuntu setup:

```bash
sudo apt update
sudo apt install -y python3 python3-venv libfreetype6 nginx
cd /var/www/pdf_text
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Test:

```bash
python main.py
```

For a persistent service, create `/etc/systemd/system/pdf-editor.service`:

```ini
[Unit]
Description=PDF Text Style Editor Web
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/pdf_text
Environment=PDF_EDITOR_HOST=127.0.0.1
Environment=PDF_EDITOR_PORT=8000
ExecStart=/var/www/pdf_text/.venv/bin/python /var/www/pdf_text/main.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pdf-editor
```

Nginx example:

```nginx
server {
    listen 80;
    server_name editor.example.com;
    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Use Certbot / your normal TLS setup for HTTPS.

## Files added/changed for web mode

- `main.py` — starts Uvicorn/FastAPI instead of Qt
- `web_app.py` — thin browser controller/API around the existing engine
- `web/index.html` — browser interface
- `web/static/style.css` — responsive frontend styles
- `web/static/editor.js` — browser interactions
- `requirements.txt` — headless/web dependencies
- `run_web.sh` — quick server launcher

The original desktop entrypoint and requirements are retained as:

- `desktop_main.py`
- `requirements_desktop.txt`

The existing modules in `app/` are not modified by the web conversion.

## Important production note

The current browser session store is in-memory and is intended for a single Uvicorn worker (`python main.py`). For a multi-worker/high-traffic deployment, session state should be moved to a shared store or job system.
