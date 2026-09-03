import platform
import sys


print("=" * 60)
print("PDF Text Style Editor V3.19 Multi-Document Metadata Passthrough - Environment Check")
print("=" * 60)
print("OS:", platform.platform())
print("Python:", sys.version)
print()

checks = [
    ("pymupdf", "PyMuPDF"),
    ("PySide6", "PySide6"),
    ("fontTools", "fontTools"),
    ("docx", "python-docx"),
    ("PyInstaller", "PyInstaller"),
]

all_ok = True

for import_name, label in checks:
    try:
        module = __import__(import_name)
        version = getattr(module, "__version__", None)

        if import_name == "pymupdf":
            version = getattr(module, "VersionBind", version)

        print(f"OK   {label}: {version or 'installed'}")
    except Exception as exc:
        all_ok = False
        print(f"FAIL {label}: {exc}")

print()
if all_ok:
    print("Everything looks good.")
    print("Run: python main.py")
else:
    print("Install dependencies with:")
    print("pip install -r requirements.txt")
