from __future__ import annotations

import time

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:
    from requests.packages.urllib3.util.retry import Retry

try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

USER_AGENT = "TrackTerrain/0.1 (local terrain tool for track building)"


def session() -> requests.Session:
    client = requests.Session()
    client.headers["User-Agent"] = USER_AGENT
    kwargs = {
        "total": 4,
        "connect": 4,
        "read": 3,
        "backoff_factor": 1.2,
        "status_forcelist": (429, 500, 502, 503, 504),
        "raise_on_status": False,
    }
    try:
        retry = Retry(**kwargs, allowed_methods=frozenset(["GET"]))
    except TypeError:
        retry = Retry(**kwargs, method_whitelist=frozenset(["GET"]))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    client.mount("https://", adapter)
    client.mount("http://", adapter)
    return client


def fetch_bytes(client, url, *, params=None, timeout=90, attempts=4) -> bytes:
    last_error = None
    for attempt in range(attempts):
        try:
            response = client.get(url, params=params, timeout=timeout)
            if response.status_code in (400, 401, 403, 404):
                return b""
            response.raise_for_status()
            return response.content
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError, OSError) as exc:
            last_error = exc
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"Stažení z ČÚZK selhalo: {last_error}") from last_error
