"""Saxo-tradingbot: to bøger (swing + daytrading) på én SIM-konto, sensorer i Home Assistant."""
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
from strategy import (commission_dkk, day_entry_signal, day_exit_reason, entry_signal,
                      exit_reason)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tradingbot")

OPTIONS = "/data/options.json"
STATE = "/data/state.json"
# Børs -> (tidszone, åbning, lukning)
SESSIONS = {
    "xcse": ("Europe/Copenhagen", (9, 0), (17, 0)), "xsto": ("Europe/Stockholm", (9, 0), (17, 30)),
    "xhel": ("Europe/Helsinki", (10, 0), (18, 30)), "xosl": ("Europe/Oslo", (9, 0), (16, 20)),
    "xetr": ("Europe/Berlin", (9, 0), (17, 30)), "xnas": ("America/New_York", (9, 30), (16, 0)),
    "xnys": ("America/New_York", (9, 30), (16, 0)),
}
BOOKS = {"swing": "Swing", "day": "Daytrading"}
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


def exchange_of(sym):
    return sym.split(":")[1].lower() if ":" in sym else ""


def new_book(budget):
    return {"start_dkk": float(budget), "cash_dkk": float(budget), "positions": {}, "trades": []}


class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.saxo = Saxo(cfg["app_key"], cfg.get("environment", "sim"))
        st = load_json(STATE, {})
        books = st.setdefault("books", {})
        if "positions" in st and "swing" not in books:  # migrering fra v0.1
            books["swing"] = {k: st.pop(k) for k in ("start_dkk", "cash_dkk", "positions", "trades") if k in st}
        books.setdefault("swing", new_book(cfg["budget_dkk"]))
        books.setdefault("day", new_book(cfg["day_budget_dkk"]))
        books["day"].setdefault("trades_today", {})
        for k in ("instruments", "last_run", "prices", "day_slot", "day_flat"):
            st.setdefault(k, {})
        self.state = st
        self.status = "Starter"
        self.account_key = None
        self.login_notified = False
        self.daily_cache = {}
        self.fx = {"DKK": 1.0, "USD": float(cfg["usd_dkk"]), "EUR": float(cfg["eur_dkk"])}

    # ---------- hjælpere ----------
    def save(self):
        save_json(STATE, self.state)

    def book(self, name):
        return self.state["books"][name]

    def equity(self, name):
        b = self.book(name)
        val = b["cash_dkk"]
        for sym, p in b["positions"].items():
            price = self.state["prices"].get(sym, p["entry_price"])
            val += p["qty"] * price * self.fx.get(p["currency"], 1.0)
        return val

    def ensure_session(self):
        if not self.saxo.logged_in:
            self.status = "Mangler login"
            if not self.login_notified:
                self.notify("Tradingbot", "Botten mangler login – åbn Tradingbot i Home Assistant og log ind.")
                self.login_notified = True
            return False
        self.login_notified = False
        if not self.account_key:
            self.account_key = self.saxo.client()["DefaultAccountKey"]
        for sym in self.cfg["universe"]:
            if sym not in self.state["instruments"]:
                try:
                    self.state["instruments"][sym] = self.saxo.find_instrument(sym)
                except SaxoError as e:
                    log.warning("%s", e)
        today = dt.date.today().isoformat()
        if self.state.get("delay_checked") != today and self.state["instruments"]:
            try:
                uic = next(iter(self.state["instruments"].values()))["uic"]
                self.state["data_delay"] = self.saxo.data_delay(self.account_key, uic)
                self.state["delay_checked"] = today
            except SaxoError as e:
                log.warning("Kunne ikke læse kursforsinkelse: %s", e)
        self.save()
        return True

    def daily(self, sym, uic):
        today = dt.date.today().isoformat()
        hit = self.daily_cache.get(sym)
        if not hit or hit[0] != today:
            hit = (today, [c for _, c in self.saxo.daily_closes(uic)])
            self.daily_cache[sym] = hit
        return hit[1]

    def open_entries(self, name, candidates, place_orders, out):
        """candidates: [(sorteringsnøgle, sym, inst, pris, årsag)]. Åbner så mange som der er plads til."""
        b, maxpos = self.book(name), self.cfg["max_positions" if name == "swing" else "day_max_positions"]
        free = maxpos - len(b["positions"])
        skip = self.excluded()
        for c in [c for c in candidates if c[1].split(":")[0].lower() in skip]:
            out.append("%s: springes over (nyhedsfilter)" % c[1])
        candidates = [c for c in candidates if c[1].split(":")[0].lower() not in skip]
        for _, sym, inst, price, reason in sorted(candidates)[:max(free, 0)]:
            fx = self.fx.get(inst["currency"], 1.0)
            slot = min(self.equity(name) / maxpos, b["cash_dkk"])
            qty = math.floor(slot * 0.99 / (price * fx))
            if qty < 1:
                out.append("%s: for dyr til en position (%.0f kr. ledig)" % (sym, slot))
                continue
            out.append(self.trade(name, sym, inst, True, qty, price, reason, place_orders))

    # ---------- swing ----------
    def evaluate_swing(self, symbols, place_orders=True):
        cfg, out, candidates = self.cfg, [], []
        b = self.book("swing")
        for sym in symbols:
            inst = self.state["instruments"].get(sym)
            if not inst:
                continue
            bars = self.saxo.daily_closes(inst["uic"])
            if len(bars) < cfg["sma_trend"] + 1:
                out.append("%s: for lidt kurshistorik (%d dage)" % (sym, len(bars)))
                continue
            tz = ZoneInfo(SESSIONS[exchange_of(sym)][0])
            if place_orders and bars[-1][0] != dt.datetime.now(tz).date().isoformat():
                out.append("%s: ingen handel i dag (lukket børs?)" % sym)
                continue
            closes = [c for _, c in bars]
            self.state["prices"][sym] = closes[-1]
            pos = b["positions"].get(sym)
            if pos:
                held = sum(1 for d, _ in bars if d > pos["entry_date"])
                reason = exit_reason(closes, pos, held, cfg)
                if reason:
                    out.append(self.trade("swing", sym, inst, False, pos["qty"], closes[-1], reason, place_orders))
                else:
                    out.append("%s: hold (dag %d)" % (sym, held))
            else:
                ok, r = entry_signal(closes, cfg)
                out.append("%s: RSI2=%s %s" % (sym, "-" if r is None else "%.1f" % r, "KØBSSIGNAL" if ok else ""))
                if ok:
                    candidates.append((r, sym, inst, closes[-1], "RSI2 %.1f" % r))
        self.open_entries("swing", candidates, place_orders, out)
        self.save()
        return out

    # ---------- daytrading ----------
    def evaluate_day(self, symbols, force_close=False, place_orders=True):
        cfg, out, candidates = self.cfg, [], []
        b = self.book("day")
        today = dt.date.today().isoformat()
        for sym in symbols:
            inst = self.state["instruments"].get(sym)
            if not inst:
                continue
            tz = ZoneInfo(SESSIONS[exchange_of(sym)][0])
            local_today = dt.datetime.now(tz).date()
            bars = [x for x in self.saxo.bars(inst["uic"], 5, 120)
                    if dt.datetime.fromisoformat(x["time"].replace("Z", "+00:00")).astimezone(tz).date() == local_today]
            if not bars:
                out.append("%s: ingen kurser i dag" % sym)
                continue
            price = bars[-1]["close"]
            self.state["prices"][sym] = price
            pos = b["positions"].get(sym)
            if pos:
                reason = "dagens lukning" if force_close else day_exit_reason(bars, pos, cfg)
                if reason:
                    out.append(self.trade("day", sym, inst, False, pos["qty"], price, reason, place_orders))
                else:
                    out.append("%s: hold" % sym)
            elif not force_close:
                close_h, close_m = SESSIONS[exchange_of(sym)][2]
                now_l = dt.datetime.now(tz)
                cutoff = now_l.replace(hour=close_h, minute=close_m, second=0, microsecond=0) \
                    - dt.timedelta(minutes=cfg.get("day_entry_cutoff_min", 60))
                if place_orders and now_l >= cutoff:
                    out.append("%s: ingen nye køb i sidste time" % sym)
                    continue
                if sym in b.get("stopped", {}).get(today, []):
                    out.append("%s: stoppet ud i dag – køber ikke igen" % sym)
                    continue
                ok, dev = day_entry_signal(bars, self.daily(sym, inst["uic"]), cfg)
                out.append("%s: %s fra VWAP %s" % (sym, "-" if dev is None else "%+.2f %%" % dev,
                                                    "KØBSSIGNAL" if ok else ""))
                if ok:
                    candidates.append((dev, sym, inst, price, "%.2f %% under VWAP" % -dev))
        if candidates and b["trades_today"].get(today, 0) >= cfg["day_max_trades"]:
            out.append("Dagens maks. %d handler er nået" % cfg["day_max_trades"])
        elif candidates:
            self.open_entries("day", candidates, place_orders, out)
        self.save()
        return out

    # ---------- handel ----------
    def trade(self, name, sym, inst, buy, qty, price, reason, place_orders):
        b = self.book(name)
        ccy = inst["currency"]
        fx = self.fx.get(ccy, 1.0)
        value = qty * price
        fee = commission_dkk(value, ccy, fx)
        text = "[%s] %s %d %s @ %.2f %s (%s, kurtage ca. %.0f kr.)" % (
            BOOKS[name], "KØB" if buy else "SÆLG", qty, sym, price, ccy, reason, fee)
        if not place_orders:
            return "[signal] " + text
        if not self.cfg.get("trade_enabled", True):
            return "[handel slået fra] " + text
        self.saxo.place_market_order(self.account_key, inst["uic"], buy, qty)
        today = dt.date.today().isoformat()
        if buy:
            b["cash_dkk"] -= value * fx + fee
            b["positions"][sym] = {
                "qty": qty, "entry_price": price, "currency": ccy, "exchange": exchange_of(sym),
                "entry_date": today, "entry_cost_dkk": value * fx + fee}
            if name == "day":
                b["trades_today"] = {today: b["trades_today"].get(today, 0) + 1}
            pnl = None
        else:
            pos = b["positions"].pop(sym)
            proceeds = value * fx - fee
            b["cash_dkk"] += proceeds
            pnl = proceeds - pos["entry_cost_dkk"]
            text += ", resultat %+.0f kr." % pnl
            if name == "day" and reason == "stop":
                b["stopped"] = {today: b.get("stopped", {}).get(today, []) + [sym]}
        b["trades"] = (b["trades"] + [{
            "time": dt.datetime.now().isoformat(timespec="minutes"), "text": text, "pnl": pnl}])[-100:]
        log.info(text)
        self.notify("Tradingbot", text)
        return text

    # ---------- planlægning ----------
    def schedule(self):
        """Returnerer liste af (type, børs, symboler) der skal køres nu."""
        jobs, by_ex = [], {}
        for sym in self.cfg["universe"]:
            if exchange_of(sym) in SESSIONS:
                by_ex.setdefault(exchange_of(sym), []).append(sym)
        for ex, syms in by_ex.items():
            tzname, (oh, om), (ch, cm) = SESSIONS[ex]
            now = dt.datetime.now(ZoneInfo(tzname))
            if now.weekday() >= 5:
                continue
            day, opened = now.date().isoformat(), now.replace(hour=oh, minute=om, second=0, microsecond=0)
            close = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
            in_close_window = close - dt.timedelta(minutes=15) <= now <= close - dt.timedelta(minutes=5)
            if self.cfg.get("swing_enabled", True) and in_close_window and self.state["last_run"].get(ex) != day:
                jobs.append(("swing", ex, syms))
            if not self.cfg.get("day_enabled", True):
                continue
            if ex not in self.cfg.get("day_exchanges", ["xcse"]):
                pass
            elif opened + dt.timedelta(minutes=30) <= now < close - dt.timedelta(minutes=15):
                bucket = "%sT%02d:%d" % (day, now.hour, now.minute // 5)
                if self.state["day_slot"].get(ex) != bucket:
                    jobs.append(("day", ex, syms))
            elif in_close_window and self.state["day_flat"].get(ex) != day:
                held = [s for s, p in self.book("day")["positions"].items() if p.get("exchange") == ex]
                jobs.append(("flat", ex, held))
        return jobs

    def tick(self):
        with LOCK:
            try:
                self.saxo.refresh_if_needed()
                if not self.ensure_session():
                    return
                self.status = "Kører"
                for kind, ex, syms in self.schedule():
                    tzname = SESSIONS[ex][0]
                    now = dt.datetime.now(ZoneInfo(tzname))
                    if kind == "swing":
                        lines = self.evaluate_swing(syms)
                        self.state["last_run"][ex] = now.date().isoformat()
                    elif kind == "day":
                        lines = self.evaluate_day(syms)
                        self.state["day_slot"][ex] = "%sT%02d:%d" % (now.date().isoformat(), now.hour, now.minute // 5)
                    else:
                        lines = self.evaluate_day(syms, force_close=True) if syms else []
                        self.state["day_flat"][ex] = now.date().isoformat()
                    for line in lines:
                        if "KØB" in line or "SÆLG" in line or kind == "swing":
                            log.info(line)
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

    def excluded(self):
        """Tickers fra input_text.tradingbot_udeluk (kommasepareret, fx 'NOVOb,AAPL'), som ikke må købes i dag."""
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return set()
        req = urllib.request.Request("http://supervisor/core/api/states/input_text.tradingbot_udeluk",
                                     headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                val = json.load(r).get("state", "")
        except Exception:
            return set()
        if val in ("", "unknown", "unavailable", "ingen"):
            return set()
        return {t.strip().split(":")[0].lower() for t in val.split(",") if t.strip()}

    def notify(self, title, msg):
        svc = (self.cfg.get("notify_service") or "").strip()
        if svc.startswith("notify."):
            self.ha("POST", "/services/notify/" + svc.split(".", 1)[1], {"title": title, "message": msg})

    def publish(self):
        def put(key, state, **attrs):
            self.ha("POST", "/states/sensor.tradingbot_" + key, {"state": state, "attributes": attrs})

        put("status", self.status, friendly_name="Tradingbot status", icon="mdi:robot",
            kursforsinkelse_min=self.state.get("data_delay"))
        for name, label in BOOKS.items():
            b, eq = self.book(name), self.equity(name)
            closed = [t["pnl"] for t in b["trades"] if t.get("pnl") is not None]
            put(name + "_egenkapital", round(eq, 2), friendly_name="%s egenkapital" % label,
                unit_of_measurement="DKK", state_class="measurement", icon="mdi:cash")
            put(name + "_afkast", round((eq / b["start_dkk"] - 1) * 100, 2), friendly_name="%s afkast" % label,
                unit_of_measurement="%", state_class="measurement", icon="mdi:chart-line")
            put(name + "_positioner", len(b["positions"]), friendly_name="%s positioner" % label,
                icon="mdi:briefcase", positions=b["positions"])
            put(name + "_handler", len(closed), friendly_name="%s lukkede handler" % label, icon="mdi:counter",
                vundne=sum(1 for p in closed if p > 0), tabte=sum(1 for p in closed if p <= 0),
                seneste=[t["time"] + " " + t["text"] for t in reversed(b["trades"][-10:])])
            last = b["trades"][-1]["text"] if b["trades"] else "Ingen endnu"
            put(name + "_seneste_handel", last[:250], friendly_name="%s seneste handel" % label,
                icon="mdi:swap-horizontal")


# ---------- web-UI (ingress) ----------
def make_handler(bot):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def page(self, body):
            doc = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                   "<title>Tradingbot</title><style>body{font-family:sans-serif;background:#111;color:#eee;"
                   "max-width:820px;margin:auto;padding:16px}a,button{color:#fff;background:#1f5fd6;border:0;"
                   "padding:8px 14px;border-radius:6px;text-decoration:none;cursor:pointer;margin:2px}"
                   "input{width:100%%;padding:8px}td{padding:3px 8px}pre{white-space:pre-wrap;background:#222;"
                   "padding:8px}</style>%s") % body
            data = doc.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            with LOCK:
                parts = []
                for name, label in BOOKS.items():
                    b, eq = bot.book(name), bot.equity(name)
                    rows = "".join("<tr><td>%s</td><td>%d</td><td>%.2f %s</td><td>%s</td></tr>" % (
                        html.escape(s), p["qty"], p["entry_price"], p["currency"], p["entry_date"])
                        for s, p in b["positions"].items()) or "<tr><td>Ingen positioner</td></tr>"
                    trades = "".join("<li>%s – %s</li>" % (t["time"], html.escape(t["text"]))
                                     for t in reversed(b["trades"][-10:])) or "<li>Ingen handler endnu</li>"
                    parts.append("<h2>%s: %.0f kr. (%+.2f %%)</h2><table>%s</table><ul>%s</ul>" % (
                        label, eq, (eq / b["start_dkk"] - 1) * 100, rows, trades))
                login = ""
                if not bot.saxo.logged_in:
                    login = ("<h2>Log ind på Saxo (simulering)</h2><p>1. <a target=_blank href='%s'>Log ind</a>"
                             "</p><p>2. Siden ender på en fejl (localhost). Kopiér hele adressen og indsæt her:</p>"
                             "<form method=post action=login><input name=url placeholder='http://localhost/callback?code=...'>"
                             "<p><button>Gem login</button></p></form>") % html.escape(bot.saxo.login_url())
                delay = bot.state.get("data_delay")
                delay_txt = "ukendt" if delay is None else ("realtid" if delay == 0 else "%s min. forsinket" % delay)
            self.page("<h1>Tradingbot</h1><p>Status: <b>%s</b> · Kurser: %s</p>%s%s"
                      "<form method=post action=signals><button>Swing-signaler nu</button></form>"
                      "<form method=post action=daysignals><button>Daytrading-signaler nu</button></form>" % (
                          html.escape(bot.status), delay_txt, login, "".join(parts)))

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            form = urllib.parse.parse_qs(self.rfile.read(n).decode())
            with LOCK:
                try:
                    if self.path.endswith("login"):
                        bot.saxo.complete_login(form.get("url", [""])[0])
                        bot.ensure_session()
                        msg = "Login gemt. Botten kører nu."
                    elif self.path.endswith("daysignals") and bot.ensure_session():
                        msg = "\n".join(bot.evaluate_day(bot.cfg["universe"], place_orders=False))
                    elif self.path.endswith("signals") and bot.ensure_session():
                        msg = "\n".join(bot.evaluate_swing(bot.cfg["universe"], place_orders=False))
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
        if time.time() - last_pub > 120:
            bot.publish()
            last_pub = time.time()
        time.sleep(30)


if __name__ == "__main__":
    main()
