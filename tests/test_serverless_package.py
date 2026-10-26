import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVERLESS = os.path.join(ROOT, "serverless")


def _read(*parts):
    with open(os.path.join(*parts), "rb") as f:
        return f.read()


# ======================= Lambda startup script =======================
# run.sh is the Lambda handler under the Web Adapter; a broken one only fails at Lambda init, so check it here.

def test_startup_script_is_a_bash_script_with_lf_endings():
    content = _read(SERVERLESS, "run.sh")
    assert content.startswith(b"#!/bin/bash\n")
    assert b"\r" not in content


def test_startup_script_starts_the_server_app():
    content = _read(SERVERLESS, "run.sh").decode()
    match = re.search(r"uvicorn\b.*\b(\w+):(\w+)\s*$", content, re.MULTILINE)
    assert match, "run.sh must launch uvicorn with <module>:<app>"
    module, app_name = match.groups()
    source = _read(SERVERLESS, f"{module}.py").decode()
    assert re.search(rf"^{app_name} = Starlette\(", source, re.MULTILINE)


def test_startup_script_is_marked_executable_and_lf_in_git():
    # .gitattributes keeps run.sh LF on every platform, so Windows checkouts still build a working zip
    attributes = _read(ROOT, ".gitattributes").decode()
    assert re.search(r"^\*\.sh\s+text\s+eol=lf\s*$", attributes, re.MULTILINE)
