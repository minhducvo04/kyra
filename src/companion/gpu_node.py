"""Small Ollama wire client. Only tailnet IPs (or loopback tests), never proxies or redirects."""
import ipaddress
import math
from urllib.parse import urlsplit

import httpx

STARTER_MODELS = (
    "gemma3:12b-it-q4_K_M",
    "gemma3:27b-it-q4_K_M",
    "devstral:24b-small-2505-q4_K_M",
)
DEFAULT_MODEL = STARTER_MODELS[0]
PROBE_TIMEOUT = 3.0


class NodeUnavailable(Exception):
    """Content-free error: never log transport URLs, prompts or server error bodies."""


class OllamaNode:
    def __init__(self, url, timeout=120):
        try:
            parts = urlsplit(url)
            address = ipaddress.ip_address(parts.hostname or "")
            allowed = address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")
            if (parts.scheme not in {"http", "https"} or not allowed or parts.username is not None
                    or parts.password is not None or parts.path not in {"", "/"}
                    or parts.query or parts.fragment or parts.port == 0):
                raise ValueError
            if not math.isfinite(timeout) or not 0 < timeout <= 600:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Use an HTTP(S) Tailscale IP URL without credentials, path or query.") from None
        self.url, self.timeout = url.rstrip("/"), timeout

    def _request(self, path, payload=None, *, timeout=None):
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout or self.timeout) as client:
                response = client.request("GET" if payload is None else "POST", self.url + path, json=payload)
                response.raise_for_status()
                result = response.json()
            if not isinstance(result, dict) or "error" in result:
                raise ValueError
            return result
        except (httpx.HTTPError, ValueError):
            raise NodeUnavailable("Ollama unavailable or returned an invalid response") from None

    def models(self):
        """GET /api/tags with a short timeout; preserve quantization metadata."""
        result = self._request("/api/tags", timeout=min(PROBE_TIMEOUT, self.timeout))
        rows = result.get("models")
        if (not isinstance(rows, list) or any(not isinstance(row, dict)
                or not isinstance(row.get("name"), str) for row in rows)):
            raise NodeUnavailable("Invalid Ollama model list")
        return rows

    @staticmethod
    def require_model(model, rows):
        if model not in STARTER_MODELS or not any(
            row["name"] == model and isinstance(row.get("details"), dict)
            and row["details"].get("quantization_level") == "Q4_K_M" for row in rows
        ):
            raise NodeUnavailable("Approved 4-bit model is missing")

    def benchmark(self, model, rows):
        """Only this fixed synthetic prompt is available to the coordinator's check."""
        self.require_model(model, rows)
        result = self._request("/api/generate", {
            "model": model, "prompt": "Explain how merge sort works in simple steps.",
            "stream": False, "keep_alive": 0,
            "options": {"num_predict": 32, "num_ctx": 4096, "temperature": 0},
        })
        count, duration = result.get("eval_count"), result.get("eval_duration")
        if (result.get("done") is not True or type(count) is not int or count <= 0
                or type(duration) is not int or duration <= 0):
            raise NodeUnavailable("Ollama returned no speed measurement")
        return count / (duration / 1_000_000_000)
