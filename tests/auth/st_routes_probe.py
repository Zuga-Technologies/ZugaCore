"""Start SuperTokens through our own init and report what its HTTP routes do.

Run as a separate process by the route tests (test_closed_doors.py): SuperTokens keeps
process-wide singletons that its own reset() does not fully clear, so each
probe gets a fresh interpreter. Nothing real is contacted: the core URI points
at a closed local port, and the in-process function that would create an
account is replaced by a recorder. Prints one JSON line.
"""
import asyncio
import json
import os

os.environ["SUPERTOKENS_CONNECTION_URI"] = "http://127.0.0.1:9"  # nothing listens there
os.environ.setdefault("API_DOMAIN", "http://localhost:8000")
os.environ.setdefault("WEBSITE_DOMAIN", "http://localhost:5173")
# Production has Google and GitHub configured, which also turns on the
# third-party recipe; mirror that so the route list matches production.
for _name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"):
    os.environ[_name] = "dummy"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from supertokens_python import Supertokens  # noqa: E402
from supertokens_python.framework.fastapi import get_middleware  # noqa: E402
from supertokens_python.recipe.emailpassword.asyncio import sign_up  # noqa: E402
from supertokens_python.recipe.emailpassword.recipe import EmailPasswordRecipe  # noqa: E402

from core.auth.supertokens_init import init_supertokens  # noqa: E402

init_supertokens()

calls: list[str] = []


async def _recording_sign_up(**kwargs):
    calls.append(kwargs["email"])
    return "created"


EmailPasswordRecipe.get_instance().recipe_implementation.sign_up = _recording_sign_up

st = Supertokens.get_instance()
base = st.app_info.api_base_path.get_as_string_dangerous()
routes_on: dict[str, bool] = {}
for recipe in st.recipe_modules:
    for api in recipe.get_apis_handled():
        key = f"{api.method.upper()} {base}{api.path_without_api_base_path.get_as_string_dangerous()}"
        routes_on[key] = routes_on.get(key, False) or not api.disabled

app = FastAPI()
app.add_middleware(get_middleware())
client = TestClient(app, raise_server_exceptions=False)
form = {"formFields": [
    {"id": "email", "value": "someone@example.com"},
    {"id": "password", "value": "a-long-password-1"},
]}
answers = {}
for path in ("/signup", "/signin", "/session/refresh", "/signout"):
    empty = client.post(f"{base}{path}", json={})
    filled = client.post(f"{base}{path}", json=form)
    answers[path] = {
        "empty": [empty.status_code, empty.text[:200]],
        "filled": [filled.status_code, filled.text[:200]],
    }
http_calls = list(calls)

# What POST /api/auth/register uses: the in-process function, not the route.
in_process = asyncio.run(sign_up("public", "via-register@example.com", "a-long-password-1"))

print(json.dumps({
    "routes_on": routes_on,
    "answers": answers,
    "sign_up_calls_from_http": http_calls,
    "in_process_sign_up": {"result": in_process, "calls": calls[len(http_calls):]},
}))
