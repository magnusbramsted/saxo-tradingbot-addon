"""Saxo OpenAPI-klient med PKCE-login og automatisk fornyelse af token."""
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

ENV = {
    "sim": {
        "auth": "https://sim.logonvalidation.net",
        "gateway": "https://gateway.saxobank.com/sim/openapi",
    },
    "live": {
        "auth": "https://live.logonvalidation.net",
        "gateway": "https://gateway.saxobank.com/openapi",
    },
}
REDIRECT_URI = "http://localhost/callback"
TOKEN_FILE = "/data/tokens.json"


class SaxoError(Exception):
    pass


class Saxo:
    def __init__(self, app_key, env="sim", token_file=TOKEN_FILE):
        self.app_key = app_key
        self.urls = ENV[env]
        self.token_file = token_file
        self.tokens = self._load()
        self._pending = None  # (state, verifier, url)

    # ---------- tokens ----------
    def _load(self):
        try:
            with open(self.token_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = self.token_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.tokens, f)
        os.replace(tmp, self.token_file)

    def _store(self, resp, verifier):
        now = time.time()
        self.tokens = {
            "access_token": resp["access_token"],
            "refresh_token": resp.get("refresh_token"),
            "expires_at": now + int(resp.get("expires_in", 1200)),
            "refresh_expires_at": now + int(resp.get("refresh_token_expires_in", 3600)),
            "verifier": verifier,
        }
        self._save()

    @property
    def logged_in(self):
        return bool(self.tokens) and time.time() < self.tokens.get("refresh_expires_at", 0)

    def login_url(self):
        if self._pending:
            return self._pending[2]
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        q = urllib.parse.urlencode({
            "response_type": "code", "client_id": self.app_key, "state": state,
            "redirect_uri": REDIRECT_URI, "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        url = "%s/authorize?%s" % (self.urls["auth"], q)
        self._pending = (state, verifier, url)
        return url

    def complete_login(self, pasted_url):
        if not self._pending:
            raise SaxoError("Start login igen – siden er genindlæst.")
        params = urllib.parse.parse_qs(urllib.parse.urlparse(pasted_url.strip()).query)
        code, state = params.get("code", [None])[0], params.get("state", [None])[0]
        if not code:
            raise SaxoError("Fandt ingen 'code' i adressen.")
        if state != self._pending[0]:
            raise SaxoError("State passer ikke – start login igen.")
        verifier = self._pending[1]
        resp = self._token_request({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT_URI, "client_id": self.app_key,
            "code_verifier": verifier,
        })
        self._store(resp, verifier)
        self._pending = None

    def refresh_if_needed(self, margin=120):
        if not self.tokens or time.time() < self.tokens["expires_at"] - margin:
            return
        if not self.logged_in:
            raise SaxoError("Login udløbet – log ind igen.")
        resp = self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": self.tokens["refresh_token"],
            "client_id": self.app_key,
            "code_verifier": self.tokens.get("verifier", ""),
        })
        self._store(resp, self.tokens.get("verifier", ""))

    def _token_request(self, data):
        req = urllib.request.Request(
            self.urls["auth"] + "/token",
            data=urllib.parse.urlencode(data).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise SaxoError("Token-fejl %s: %s" % (e.code, e.read()[:300].decode(errors="ignore")))

    # ---------- API ----------
    def request(self, method, path, params=None, body=None):
        self.refresh_if_needed()
        url = self.urls["gateway"] + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self.tokens.get("access_token", ""),
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise SaxoError("%s %s -> %s: %s" % (
                method, path, e.code, e.read()[:400].decode(errors="ignore")))

    def client(self):
        return self.request("GET", "/port/v1/clients/me")

    def find_instrument(self, symbol):
        """symbol i Saxo-format, fx 'NOVOb:xcse' eller 'AAPL:xnas'."""
        res = self.request("GET", "/ref/v1/instruments",
                           {"Keywords": symbol, "AssetTypes": "Stock"})
        for d in res.get("Data", []):
            if d.get("Symbol", "").lower() == symbol.lower():
                return {"uic": d["Identifier"], "currency": d.get("CurrencyCode"),
                        "exchange": d.get("ExchangeId"), "name": d.get("Description")}
        raise SaxoError("Instrument ikke fundet: " + symbol)

    def daily_closes(self, uic, count=260):
        """Dagslys (seneste bar kan være dagens, ufuldstændige). Returnerer [(dato, close)]."""
        params = {"AssetType": "Stock", "Uic": uic, "Horizon": 1440, "Count": count}
        try:
            res = self.request("GET", "/chart/v3/charts", params)
        except SaxoError:
            res = self.request("GET", "/chart/v1/charts", params)
        out = []
        for b in res.get("Data", []):
            close = b.get("Close", b.get("CloseBid"))
            if close is not None:
                out.append((b["Time"][:10], float(close)))
        return out

    def place_market_order(self, account_key, uic, buy, amount):
        return self.request("POST", "/trade/v2/orders", body={
            "AccountKey": account_key, "Uic": uic, "AssetType": "Stock",
            "BuySell": "Buy" if buy else "Sell", "Amount": amount,
            "OrderType": "Market", "OrderDuration": {"DurationType": "DayOrder"},
            "ManualOrder": False,
        })
