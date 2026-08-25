#!/bin/bash
# Pre-deploy checks. Fast, offline, and no credentials — safe to run anywhere.
#
# The JS check exists because the whole dashboard is one inline <script> in a Jinja
# template: nothing compiles it, Flask serves it happily, and a stray newline inside a
# quoted string renders a page with a working header and no data at all. That failure is
# invisible to `python3 -c "import server"` and to every test that only checks the API.
set -euo pipefail
cd "$(dirname "$0")/.."

for f in *.py tools/*.py history-service/*.py; do
  python3 -c "import ast,io,sys; ast.parse(io.open('$f').read())" || exit 1
done
echo "python syntax ok"

if command -v node >/dev/null 2>&1; then
  python3 - <<'PY'
import io, re, subprocess, sys, tempfile
s = io.open("templates/index.html").read()
blocks = re.findall(r"<script>(.*?)</script>", s, re.S)
if not blocks:
    sys.exit("no inline <script> found in templates/index.html — did the page change shape?")
# Jinja only ever injects literals into this page, so a stub keeps it parseable.
js = re.sub(r"\{\{[^}]*\}\}", "null", "\n".join(blocks))
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
    f.write(js); path = f.name
r = subprocess.run(["node", "--check", path])
sys.exit(r.returncode)
PY
  echo "inline js syntax ok"
else
  echo "node not installed — SKIPPING the inline JS check (a syntax error here ships a blank page)"
fi
