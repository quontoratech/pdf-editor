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

The first screen is a login page. MongoDB settings and the initial admin login are read from `.env`:

```text
MONGO_URI=...
MONGO_DB_NAME=pdf_text_browser
ADMIN_USERNAME=...
ADMIN_PASSWORD=...
```

Admins see `Make User` and `All Users` buttons in the editor toolbar. New users created there are normal users and only see the PDF tool after login.

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
- Load exact TTF/OTF font
- Re-check exact font match
- Clean text replacement using the existing `PdfEngine`
- Horizontal and multiline reflow using the existing engine
- Undo last replacement
- Save/export PDF using the existing `CleanEditSession`
- Download audit and validation JSON sidecars
- Browser full-screen mode

## VPS deployment

Recommended Ubuntu setup:

```bash
sudo apt update
sudo apt install -y python3 python3-venv nginx
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
