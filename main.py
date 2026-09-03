import os
from pathlib import Path

import uvicorn

from app.env import load_env_file


if __name__ == "__main__":
    load_env_file(Path(__file__).resolve().parent / ".env")
    host = os.environ.get("PDF_EDITOR_HOST", "0.0.0.0")
    port = int(os.environ.get("PDF_EDITOR_PORT", "8000"))
    uvicorn.run("web_app:app", host=host, port=port, reload=False)
