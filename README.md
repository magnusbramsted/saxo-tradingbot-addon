# Saxo tradingbot (Home Assistant add-on)

To bøger mod Saxo OpenAPI – swing og daytrading – **kun simulering** (`environment: sim`).

**Regler:** Køb når lukkekurs > SMA200 og RSI(2) < 10. Sælg ved luk over SMA5, efter maks. 10 dage eller ved −8 % nødstop. Scanner hver børs 15–5 min før lukketid på hverdage. Botten fører et virtuelt budget (standard 10.000 kr.) med Saxo Classic-kurtage, så afkastet svarer til en lille konto, selvom SIM-kontoen er større.

**Daytrading (egen bog, 10.000 kr.):** Hvert 5. min. fra 30 min. efter åbning: køb når kursen er ≥1 % under dagens VWAP, 5-min RSI(2) < 10 og aktien er over SMA50. Sælg ved VWAP, −1 % stop eller senest 15 min. før lukning. Maks. 6 køb om dagen, aldrig positioner natten over.

**Opsætning**
1. Tilføj `https://github.com/magnusbramsted/saxo-tradingbot-addon` under Add-on store → Repositories.
2. Installér *Saxo tradingbot*, indsæt App Key fra developer.saxo under Konfiguration, og start.
3. Åbn *Tradingbot* i sidepanelet, klik **Log ind**, og indsæt adressen fra den fejlede localhost-side.

**Sensorer:** `sensor.tradingbot_status` og pr. bog (`swing`/`day`): `sensor.tradingbot_<bog>_egenkapital`, `_afkast`, `_positioner`, `_handler`, `_seneste_handel`. Push-besked ved handel: sæt `notify_service`, fx `notify.mobile_app_iphone`.

Botten skal holde sig kørende: står den stille mere end ca. en time, udløber login, og så skal du logge ind igen.
