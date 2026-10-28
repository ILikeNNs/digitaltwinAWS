"""Builds the Lambda deployment zip for the serverless digital twin.

1. Embeds the resume chunks once (skipped while linkedin.pdf is unchanged) -> serverless/embeddings.json
2. Installs the locked openai SDK + Starlette/uvicorn for Lambda's arm64 / Python 3.13 runtime with uv (no Docker needed)
3. Zips dependencies + app code (served by the Lambda Web Adapter via run.sh); the file name carries a content
   hash so CloudFormation sees a new key per change

Usage: uv run python serverless/package.py <output-dir>   -> prints the zip path
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

from dotenv import load_dotenv
from openai import OpenAI
from pypdf import PdfReader

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from digitaltwin.rag_engine import RAGEngine  # noqa: E402  (reuse the exact chunking used by the Gradio app)
from retriever import EMBEDDING_MODEL, INDEX_PATH  # noqa: E402

PDF_PATH = os.path.join(ROOT, "linkedin.pdf")
LAMBDA_PLATFORM = "aarch64-manylinux2014"
LAMBDA_PYTHON = "3.13"
RUNTIME_PACKAGES = ["openai", "starlette", "uvicorn"]
STARTUP_SCRIPT = "run.sh"
# (source, path inside the zip): server.py imports digitaltwin.helpers, so it keeps its package folder
CODE_FILES = [
    (os.path.join(HERE, "server.py"), "server.py"),
    (os.path.join(HERE, "retriever.py"), "retriever.py"),
    (os.path.join(HERE, STARTUP_SCRIPT), STARTUP_SCRIPT),
    (INDEX_PATH, "embeddings.json"),
    (os.path.join(ROOT, "digitaltwin", "__init__.py"), "digitaltwin/__init__.py"),
    (os.path.join(ROOT, "digitaltwin", "helpers.py"), "digitaltwin/helpers.py"),
]


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def pdf_sha256():
    """Identifies the resume version; embeddings are cached under this hash so unchanged resumes are never re-embedded."""
    return _sha256(PDF_PATH)


def index_is_stale():
    """True when embeddings.json is missing or was built from a different linkedin.pdf (embedding needs the API key)."""
    if not os.path.exists(INDEX_PATH):
        return True
    with open(INDEX_PATH, encoding="utf-8") as f:
        return json.load(f).get("pdf_sha256") != pdf_sha256()


def build_index():
    if not index_is_stale():
        print("embeddings.json is up to date", file=sys.stderr)
        return

    pdf_hash = pdf_sha256()
    reader = PdfReader(PDF_PATH)
    full_text = "".join([(page.extract_text() or "") + "\n" for page in reader.pages])
    chunks = RAGEngine._chunk_text(full_text)
    response = OpenAI().embeddings.create(model=EMBEDDING_MODEL, input=chunks)
    index = {
        "pdf_sha256": pdf_hash,
        "model": EMBEDDING_MODEL,
        # 6 decimals keeps ranking intact while shrinking the file ~3x
        "chunks": [{"text": c, "embedding": [round(x, 6) for x in d.embedding]} for c, d in zip(chunks, response.data)],
    }
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f)
    print(f"Embedded {len(chunks)} chunks into embeddings.json", file=sys.stderr)


def locked_version(package):
    with open(os.path.join(ROOT, "uv.lock"), encoding="utf-8") as f:
        match = re.search(rf'^name = "{package}"\nversion = "([^"]+)"', f.read(), re.MULTILINE)
    if not match:
        raise SystemExit(f"{package} not found in uv.lock")
    return match.group(1)


def write_lock_constraints(path):
    """Exports uv.lock as pip constraints, so openai's transitive dependencies are pinned too
    (otherwise each build would pull whatever httpx/pydantic/... release is newest — a supply-chain risk)."""
    subprocess.run(
        ["uv", "export", "--frozen", "--no-dev", "--no-hashes", "--no-emit-project",
         "--format", "requirements.txt", "--output-file", path],
        cwd=ROOT, check=True, stdout=subprocess.DEVNULL,
    )


def validate_startup_script(content):
    """run.sh is the Lambda handler; if it is broken the function fails at init, so refuse to ship it."""
    if not content.startswith(b"#!/bin/bash\n"):
        raise SystemExit(f"{STARTUP_SCRIPT} must start with '#!/bin/bash' and use LF line endings")
    if b"\r" in content:
        raise SystemExit(f"{STARTUP_SCRIPT} has CRLF line endings; bash on Lambda cannot run it")
    if b"uvicorn" not in content or b"server:app" not in content:
        raise SystemExit(f"{STARTUP_SCRIPT} does not start the app (expected uvicorn ... server:app)")


def build_zip(out_dir):
    build_dir = tempfile.mkdtemp(prefix="digitaltwin-lambda-")
    try:
        constraints = os.path.join(build_dir, "constraints.txt")
        write_lock_constraints(constraints)
        subprocess.run(
            ["uv", "pip", "install", "--quiet", "--target", build_dir,
             "--python-platform", LAMBDA_PLATFORM, "--python-version", LAMBDA_PYTHON,
             "--only-binary", ":all:", "--constraint", constraints,
             *[f"{package}=={locked_version(package)}" for package in RUNTIME_PACKAGES]],
            check=True,
        )
        os.remove(constraints)
        for source, destination in CODE_FILES:
            target = os.path.join(build_dir, *destination.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)
        with open(os.path.join(build_dir, STARTUP_SCRIPT), "rb") as f:
            validate_startup_script(f.read())

        digest = hashlib.sha256()
        files = []
        for dirpath, _, filenames in os.walk(build_dir):
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, build_dir).replace(os.sep, "/")
                if "__pycache__" in rel or rel == ".lock":  # .lock is uv's install marker
                    continue
                files.append((rel, full))
        for rel, full in sorted(files):
            digest.update(rel.encode())
            digest.update(_sha256(full).encode())

        os.makedirs(out_dir, exist_ok=True)
        zip_path = os.path.join(out_dir, f"chat-{digest.hexdigest()[:12]}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel, full in sorted(files):
                # Windows has no exec bit, so set Unix permissions explicitly: Lambda must be able to run run.sh
                info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = (0o100755 if rel == STARTUP_SCRIPT else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(full, "rb") as f:
                    zf.writestr(info, f.read())
        return zip_path
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    load_dotenv(os.path.join(ROOT, ".env"), override=True)
    build_index()
    print(build_zip(sys.argv[1]))
