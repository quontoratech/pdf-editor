# PDF Text Style Editor — Web Only

This package is browser-only. It does not start or require the old desktop GUI.

## Run locally in a browser

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Then open:

http://127.0.0.1:8000

`python main.py` only starts the FastAPI/Uvicorn web server. The editor UI itself runs in your browser.

## VPS

Run the same application on the VPS and put Nginx in front of port 8000. See README_WEB.md for deployment notes.

## Web files

- `main.py` — starts the web server
- `web_app.py` — FastAPI bridge to the existing PDF engine
- `web/index.html` — browser frontend
- `web/static/style.css` — browser styling
- `web/static/editor.js` — browser interactions
- `app/` — existing PDF processing backend, with the desktop UI file removed
