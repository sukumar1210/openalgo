"""
Where a Python strategy's orders go: its own mode, not the platform's switch.

A Python strategy runs in one of two modes:

* **Live** (the default, and what every strategy did before modes existed):
  its orders follow Analyzer mode. With Analyzer mode on, the whole platform
  is in the Sandbox, this strategy included; with it off, orders reach the
  broker. Analyzer mode stays the platform's master switch.
* **Sandbox**: its orders go to the Sandbox whatever Analyzer mode says.

The host enforces Sandbox mode through the key it hands the script
(``OPENALGO_API_KEY``). Instead of the user's API key, the script gets a
**Sandbox key** derived from it. Any API request that carries a Sandbox key is
answered as if Analyzer mode were on, because ``get_analyze_mode()`` asks
``request_is_sandbox_only()`` first, and every order, cancel, position and
funds path decides its route through ``get_analyze_mode()``. The key only
ever restricts: a request presenting a forged one is routed to the Sandbox
and then refused by ``verify_api_key``, so the prefix needs no verification
to be safe.

A Sandbox key is the real key's HMAC under the API key pepper. Nothing new is
stored, it cannot be produced without the real key and the pepper, and
regenerating the API key retires it with the old one (both are cleared from
the same caches).

The limit, stated rather than hidden: a script that ignores the injected key
and uses the real one by some other route escapes this. The host refuses to
start a Sandbox-mode script whose file contains the real key, which is the
common way that happens.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re

#: Real API keys are hex, so they never start with this.
SANDBOX_KEY_PREFIX = "sandbox-"

SANDBOX = "sandbox"
LIVE = "live"
MODES = (SANDBOX, LIVE)
#: A strategy saved before modes existed followed Analyzer mode, as Live does.
DEFAULT_MODE = LIVE
#: A host entry that is a Run's source (services/run_service.py), not a trader's strategy.
SOURCE_KIND = "source"


def is_source_config(config: dict) -> bool:
    return config.get("kind") == SOURCE_KIND


def mode_of(config: dict) -> str:
    """A strategy config's mode; Live for one saved before modes existed."""
    mode = config.get("mode")
    return mode if mode in MODES else DEFAULT_MODE


#: An account key: ``sandbox-<token>-<hmac>``, naming one Sandbox account.
_ACCOUNT_KEY = re.compile(r"sandbox-([0-9a-f]{8})-[0-9a-f]{64}")


def sandbox_key_for(api_key: str, token: str | None = None) -> str:
    """The Sandbox key derived from a user's API key.

    With ``token``, the key of one of the user's Sandbox accounts
    (sandbox/accounts.py): the token is bound into the HMAC, so a key cannot
    be moved to another account by editing its prefix.
    """
    pepper = os.environ["API_KEY_PEPPER"].encode()
    if token is None:
        return SANDBOX_KEY_PREFIX + hmac.new(pepper, api_key.encode(), hashlib.sha256).hexdigest()
    mac = hmac.new(pepper, f"{api_key}|{token}".encode(), hashlib.sha256).hexdigest()
    return f"{SANDBOX_KEY_PREFIX}{token}-{mac}"


def account_token(key) -> str | None:
    """The Sandbox account an account key names; None for any other key."""
    match = _ACCOUNT_KEY.fullmatch(key) if isinstance(key, str) else None
    return match.group(1) if match else None


def accounts_enabled() -> bool:
    """Whether Sandbox accounts are on (sandbox/accounts.py); off unless set."""
    return os.getenv("SANDBOX_ACCOUNTS_ENABLED", "false").strip().lower() == "true"


def is_sandbox_key(key) -> bool:
    return isinstance(key, str) and key.startswith(SANDBOX_KEY_PREFIX)


def request_is_sandbox_only() -> bool:
    """Whether the current API request carries a Sandbox key, wherever it put it.

    Looks at the header, the query string and the JSON body, the three places
    an API key reaches ``/api/``. A body sent with another content type is
    still read, from the raw bytes Flask keeps, without changing how the
    endpoint itself will parse it. Outside a request, and outside ``/api/``,
    the answer is no.
    """
    from flask import g, has_request_context, request

    if not has_request_context() or not request.path.startswith("/api/"):
        return False
    cached = g.get("_sandbox_only")
    if cached is not None:
        return cached
    found = is_sandbox_key(request.headers.get("X-API-KEY")) or is_sandbox_key(
        request.args.get("apikey")
    )
    if not found:
        body = request.get_json(silent=True) if request.is_json else None
        if body is None:
            try:
                body = json.loads(request.get_data(cache=True) or b"null")
            except ValueError:
                body = None
        found = isinstance(body, dict) and is_sandbox_key(body.get("apikey"))
    g._sandbox_only = found
    return found
