#!/usr/bin/with-contenv bashio
bashio::log.info "Tradingbot starter."
cd /app
while true; do
  python3 /app/bot.py
  bashio::log.warning "Botten stoppede. Genstarter om 30 s."
  sleep 30
done
