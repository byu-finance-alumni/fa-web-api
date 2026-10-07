"""ENVIRONMENT fails closed to production when unset (#597).

It used to default to "development", so a deploy that forgot the variable would
publish /docs, /redoc and /openapi.json (a recon map of every route), allow SQL
echo and trust localhost CORS origins. The default is now "production"; anything
that wants development behaviour sets it explicitly (local .env, tests/conftest,
the CI test job, the dev Vercel API project).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from app.core.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_settings_default_is_production(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert Settings(_env_file=None).environment == "production"


def test_app_built_without_environment_hides_docs_and_schema(tmp_path):
    """Import the real app in a clean process with ENVIRONMENT unset (and no
    .env — cwd is an empty temp dir) and confirm the docs surfaces are gone."""
    env = {k: v for k, v in os.environ.items() if k != "ENVIRONMENT"}
    env["DATABASE_URL"] = ""
    env["PYTHONPATH"] = str(_REPO_ROOT)
    probe = (
        "import json\n"
        "from app.main import app\n"
        "from app.core.config import get_settings\n"
        "paths = sorted({getattr(r, 'path', '') for r in app.routes})\n"
        "print(json.dumps({'env': get_settings().environment,"
        " 'openapi_url': app.openapi_url, 'paths': paths}))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["env"] == "production"
    assert result["openapi_url"] is None
    for hidden in ("/docs", "/redoc", "/openapi.json"):
        assert hidden not in result["paths"]
