"""Saxo-tradingbot: daglig scanning før lukketid, ordrer i SIM, sensorer i Home Assistant."""
import datetime as dt
import html
import json
import logging
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from saxo import Saxo, SaxoError
from strategy import commission_dkk, entry_signal, exit_reason, rsi

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tradingbot")

OPTIONS = "/data/options.json"
STATE = "/data/state.json"
# Børs -> (tidszone, lukketid). Botten handler 15-5 min før lukning.
CLOSES = {
    "xcse": ("Europe/Copenhagen", 17, 0), "xsto": ("Europe/Stockholm", 17, 30),
    "xhel": ("Europe/Helsinki", 18, 30), "xosl": ("Europe/Oslo", 16, 20),
    "xetr": ("Europe/Berlin", 17, 30), "xnas": ("America/New_York", 16, 0),
    "xnys": ("America/New_York", 16, 0),
}
LOCK = threading.RLock()


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.saxo = Saxo(cfg["app_key"], cfg.get("environment", "sim"))
        self.state = load_json(STATE, {})
        self.state.setdefault("start_dkk", float(cfg["budget_dkk"]))
        self.state.setdefault("cash_dkk", float(cfg["budget_dkk"]))
        for k in ("positions", "instruments", "last_run", "prices"):
            self.state.setdefault(k, {})
        self.state.setdefault("trades", [])
        self.status = "Starter"
        self.account_key = None
        self.fx = {"DKK": 1.0, "USD": float(cfg["usd_dkk"]), "EUR": float(cfg["eur_dkk"])}

    # ---------- hjælpere ----------
    def save(self):
        save_json(STATE, self.state)

    def equity(self):
        val = self.state["cash_dkk"]
        for sym, p in self.state["positions"].items():
            price = self.state["prices"].get(sym, p["entry_price"])
            val += p["qty"] * price * self.fx.get(p["currency"], 1.0)
        return val

    def ensure_session(self):
        if not self.saxo.logged_in:
            self.status = "Mangler login"
            return False
        if not self.account_key:
            self.account_key = self.saxo.client()["DefaultAccountKey"]
        for sym in self.cfg["universe"]:
            if sym not in self.state["instruments"]:
                try:
                    self.state["instruments"][sym] = self.saxo.find_instrument(sym)
                except SaxoError as e:
                    log.warning("%s", e)
        self.save()
        return True

    # ---------- strategi ----------
    def evaluate(self, symbols, place_orders=True):
        """Kører exits og derefter entries for symbolerne. Returnerer logtekster."""
        cfg, out = self.cfg, []
        candidates = []
        for sym in symbols:
            inst = self.state["instruments"].get(sym)
            if not inst:
                continue
            bars = self.saxo.daily_closes(inst["uic"])
            if len(bars) < cfg["sma_trend"] + 1:
                out.append("%s: for lidt kurshistorik (%d dage)" % (sym, len(bars)))
                continue
            tz = ZoneInfo(CLOSES[sym.split(":")[1].lower()][0])
            if place_orders and bars[-1][0] != dt.datetime.now(tz).date().isoformat():
                out.append("%s: ingen handel i dag (lukket børs?)" % sym)
                continue
            closes = [c for _, c in bars]
            self.state["prices"][sym] = closes[-1]
            pos = self.state["positions"].get(sym)
            if pos:
                held = sum(1 for d, _ in bars if d > pos["entry_date"])
                reason = exit_reason(closes, pos, held, cfg)
                if reason:
                    out.append(self.trade(sym, inst, False, pos["qty"], closes[-1], reason, place_orders))
                else:
                    out.append("%s: hold (dag %d)" % (sym, held))
            else:
                ok, r = entry_signal(closes, cfg)
                out.append("%s: RSI2=%s %s" % (sym, "-" if r is None else "%.1f" % r,
                                               "KØBSSIGNAL" if ok else ""))
                if ok:
                    candidates.append((r, sym, inst, closes[-1]))
        free = cfg["max_positions"] - len(self.state["positions"])
        for r, sym, inst, price in sorted(candidates)[:max(free, 0)]:
            fx = self.fx.get(inst["currency"], 1.0)
            slot = min(self.equity() / cfg["max_positions"], self.state["cash_dkk"])
            qty = math.floor(slot * 0.99 / (price * fx))
            if qty < 1:
                out.append("%s: for dyr til en position (%.0f kr. ledig)" % (sym, slot))
                continue
            out.append(self.trade(sym, inst, True, qty, price, "RSI2 %.1f" % r, place_orders))
        self.save()
        return out

    def trade(self, sym, inst, buy, qty, price, reason, place_orders):
        ccy = inst["currency"]
        fx = self.fx.get(ccy, 1.0)
        value = qty * price
        fee = commission_dkk(value, ccy, fx)
        text = "%s %d %s @ %.2f %s (%s, kurtage ca. %.0f kr.)" % (
            "KØB" if buy else "SÆLG", qty, sym, price, ccy, reason, fee)
        if not place_orders:
            return "[signal] " + text
        if not self.cfg.get("trade_enabled", True):
            return "[handel slået fra] " + text
        self.saxo.place_market_order(self.account_key, inst["uic"], buy, qty)
        if buy:
            self.state["cash_dkk"] -= value * fx + fee
            self.state["positions"][sym] = {
                "qty": qty, "entry_price": price, "currency": ccy,
                "entry_date": dt.date.today().isoformat(), "entry_cost_dkk": value * fx + fee}
            pnl = None
        else:
            pos = self.state["positions"].pop(sym)
            proceeds = value * fx - fee
            self.state["cash_dkk"] += proceeds
            pnl = proceeds - pos["entry_cost_dkk"]
            text += ", resultat %+.0f kr." % pnl
        self.state["trades"] = (self.state["trades"] + [{
            "time": dt.datetime.now().isoformat(timespec="minutes"), "text": text, "pnl": pnl}])[-100:]
        log.info(text)
        self.notify("Tradingbot", text)
        return text

    # ---------- planlægning ----------
    def due_exchanges(self):
        due = {}
        for sym in self.cfg["universe"]:
            ex = sym.split(":")[1].lower() if ":" in sym else None
            if ex not in CLOSES:
                continue
            tzname, h, m = CLOSES[ex]
            now = dt.datetime.now(ZoneInfo(tzname))
            close = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now.weekday() < 5 and close - dt.timedelta(minutes=15) <= now <= close - dt.timedelta(minutes=5) \
                    and self.state["last_run"].get(ex) != now.date().isoformat():
                due.setdefault(ex, []).append(sym)
        return due

    def tick(self):
        with LOCK:
            try:
                self.saxo.refresh_if_needed()
                if not self.ensure_session():
                    return
                self.status = "Kører"
                for ex, syms in self.due_exchanges().items():
                    log.info("Scanner %s: %s", ex, ", ".join(syms))
                    for line in self.evaluate(syms):
                        log.info(line)
                    self.state["last_run"][ex] = dt.date.today().isoformat()
                    self.save()
            except SaxoError as e:
                self.status = "Fejl: %s" % str(e)[:120]
                log.error("%s", e)

    # ---------- Home Assistant ----------
    def ha(self, method, path, body=None):
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return
        req = urllib.request.Request(
            "http://supervisor/core/api" + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:  # HA-fejl må aldrig stoppe botten
            log.warning("HA-kald fejlede: %s", e)

    def notify(self, title, msg):
        svc = (self.cfg.get("notify_service") or "").strip()
        if svc.startswith("notify."):
            self.ha("POST", "/services/notify/" + svc.split(".", 1)[1], {"title": title, "message": msg})

    def publish(self):
        eq, start = self.equity(), self.state["start_dkk"]
        last = self.state["trades"][-1]["text"] if self.state["trades"] else "Ingen endnu"
        base = {"icon": "mdi:chart-line"}
        sensors = {
            "status": (self.status, {"friendly_name": "Tradingbot status", "icon": "mdi:robot"}),
            "egenkapital": (round(eq, 2), {"friendly_name": "Tradingbot egenkapital",
                                           "unit_of_measurement": "DKK", "state_class": "measurement"}),
            "kontanter": (round(self.state["cash_dkk"], 2), {"friendly_name": "Tradingbot kontanter",
                                                             "unit_of_measurement": "DKK"}),
            "afkast": (round((eq / start - 1) * 100, 2), {"friendly_name": "Tradingbot afkast",
                                                          "unit_of_measurement": "%", "state_class": "measurement"}),
            "positioner": (len(self.state["positions"]), {"friendly_name": "Tradingbot positioner",
                                                          "positions": self.state["positions"]}),
            "seneste_handel": (last[:250], {"friendly_name": "Tradingbot seneste handel",
                                            "icon": "mdi:swap-horizontal"}),
        }
        for key, (st, attrs) in sensors.items():
            self.ha("POST", "/states/sensor.tradingbot_" + key,
                    {"state": st, "attributes": {**base, **attrs}})


# ---------- web-UI (ingress) ----------
def make_handler(bot):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def page(self, body):
            doc = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                   "<title>Tradingbot</title><style>body{font-family:sans-serif;background:#111;color:#eee;"
                   "max-width:820px;margin:auto;padding:16px}a,button{color:#fff;background:#1f5fd6;border:0;"
                   "padding:8px 14px;border-radius:6px;text-decoration:none;cursor:pointer}input{width:100%%;"
                   "padding:8px}td{padding:3px 8px}pre{white-space:pre-wrap;background:#222;padding:8px}</style>%s"
                   ) % body
            data = doc.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            with LOCK:
                eq, st = bot.equity(), bot.state
                rows = "".join("<tr><td>%s</td><td>%d</td><td>%.2f %s</td><td>%s</td></tr>" % (
                    html.escape(s), p["qty"], p["entry_price"], p["currency"], p["entry_date"])
                    for s, p in st["positions"].items()) or "<tr><td>Ingen</td></tr>"
                trades = "".join("<li>%s – %s</li>" % (t["time"], html.escape(t["text"]))
                                 for t in reversed(st["trades"][-15:])) or "<li>Ingen endnu</li>"
                login = ""
                if not bot.saxo.logged_in:
                    login = ("<h2>Log ind på Saxo (simulering)</h2><p>1. <a target=_blank href='%s'>Log ind</a>"
                             "</p><p>2. Siden ender på en fejl (localhost). Kopiér hele adressen og indsæt her:</p>"
                             "<form method=post action=login><input name=url placeholder='http://localhost/callback?code=...'>"
                             "<p><button>Gem login</button></p></form>") % html.escape(bot.saxo.login_url())
            self.page("<h1>Tradingbot</h1><p>Status: <b>%s</b></p>%s<p>Egenkapital: <b>%.0f kr.</b> "
                      "(%+.2f %%) · Kontanter %.0f kr.</p><h2>Positioner</h2><table>%s</table>"
                      "<h2>Seneste handler</h2><ul>%s</ul><form method=post action=signals>"
                      "<button>Vis signaler nu (uden handel)</button></form>" % (
                          html.escape(bot.status), login, eq, (eq / st["start_dkk"] - 1) * 100,
                          st["cash_dkk"], rows, trades))

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            form = urllib.parse.parse_qs(self.rfile.read(n).decode())
            with LOCK:
                try:
                    if self.path.endswith("login"):
                        bot.saxo.complete_login(form.get("url", [""])[0])
                        bot.ensure_session()
                        msg = "Login gemt. Botten kører nu."
                    elif self.path.endswith("signals") and bot.ensure_session():
                        msg = "\n".join(bot.evaluate(bot.cfg["universe"], place_orders=False))
                    else:
                        msg = "Log ind først."
                except SaxoError as e:
                    msg = "Fejl: %s" % e
            self.page("<pre>%s</pre><p><a href='./'>Tilbage</a></p>" % html.escape(msg))
    return H


def main():
    cfg = load_json(OPTIONS, {})
    if not cfg.get("app_key"):
        log.error("Udfyld app_key i add-on'ens konfiguration.")
        while True:
            time.sleep(3600)
    bot = Bot(cfg)
    server = ThreadingHTTPServer(("0.0.0.0", 8099), make_handler(bot))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Tradingbot startet (%s). Univers: %d aktier.", cfg.get("environment", "sim"), len(cfg["universe"]))
    last_pub = 0
    while True:
        bot.tick()
        if time.time() - last_pub > 300:
            bot.publish()
            last_pub = time.time()
        time.sleep(30)


if __name__ == "__main__":
    main()
