"""Delta Exchange India REST helper. Paper mode never hits the order endpoint."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import requests

import config


class DeltaClient:
    def __init__(self):
        self.base = str(config.DELTA_BASE_URL).rstrip("/")
        self.api_key = str(config.DELTA_API_KEY or "").strip()
        self.api_secret = str(config.DELTA_API_SECRET or "").strip()
        self.session = requests.Session()

    def _sign(self, method, path, query="", body=""):
        timestamp = str(int(time.time()))
        message = method.upper() + timestamp + path + query + body
        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "api-key": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
            "Content-Type": "application/json",
            "User-Agent": "hedge-delta-desk",
        }

    def public_get(self, path, params=None):
        url = self.base + path
        response = self.session.get(url, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload

    def private_get(self, path, params=None):
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Delta API key/secret are not set.")
        query = urlencode(params or {})
        signed_query = ("?" + query) if query else ""
        headers = self._sign("GET", path, signed_query, "")
        url = self.base + path + signed_query
        response = self.session.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json()

    def private_post(self, path, body: dict):
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Delta API key/secret are not set.")
        raw = json.dumps(body, separators=(",", ":"))
        headers = self._sign("POST", path, "", raw)
        response = self.session.post(
            self.base + path, headers=headers, data=raw, timeout=20
        )
        response.raise_for_status()
        return response.json()

    def ticker(self, symbol="BTCUSD"):
        return self.public_get("/v2/tickers/" + symbol)

    def candles(self, symbol="BTCUSD", resolution="5m", limit=200):
        end = int(time.time())
        seconds = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "1h": 3600,
            "1d": 86400,
        }.get(resolution, 300)
        start = end - seconds * max(int(limit), 50)
        return self.public_get(
            "/v2/history/candles",
            {
                "symbol": symbol,
                "resolution": resolution,
                "start": start,
                "end": end,
            },
        )

    def products(self, contract_types="call_options,put_options"):
        collected = []
        for page in range(1, 6):
            chunk = self.public_get(
                "/v2/products",
                {
                    "contract_types": contract_types,
                    "states": "live",
                    "page_size": 100,
                    "page_no": page,
                },
            )
            if isinstance(chunk, dict):
                chunk = chunk.get("result") or []
            if not chunk:
                break
            collected.extend(chunk)
            if len(chunk) < 100:
                break
        return collected

    def wallet(self):
        return self.private_get("/v2/wallet/balances")

    def place_order(self, product_id, size, side, order_type="market_order"):
        if config.DELTA_PAPER or not config.DELTA_TRADING_ENABLED:
            return {
                "paper": True,
                "product_id": product_id,
                "size": size,
                "side": side,
                "order_type": order_type,
            }
        return self.private_post(
            "/v2/orders",
            {
                "product_id": int(product_id),
                "size": int(size),
                "side": side,
                "order_type": order_type,
            },
        )
