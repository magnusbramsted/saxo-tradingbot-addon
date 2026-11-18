# Saxo tradingbot (Home Assistant add-on)

Daglig RSI(2)-pullback-bot mod Saxo OpenAPI – **kun simulering** (`environment: sim`).

**Regler:** Køb når lukkekurs > SMA200 og RSI(2) < 10. Sælg ved luk over SMA5, efter maks. 10 dage eller ved −8 % nødstop. Scanner hver børs 15–5 min før lukketid på hverdage. Botten fører et virtuelt budget (standard 10.000 kr.) med Saxo Classic-kurtage, så afkastet svarer til en lille konto, selvom SIM-kontoen er større.

**Opsætning**
1. Tilføj `https://github.com/magnusbramsted/saxo-tradingbot-addon` under Add-on store → Repositories.
2. Installér *Saxo tradingbot*, indsæt App Key fra developer.saxo under Konfiguration, og start.
3. Åbn *Tradingbot* i sidepanelet, klik **Log ind**, og indsæt adressen fra den fejlede localhost-side.

**Sensorer:** `sensor.tradingbot_status`, `_egenkapital`, `_kontanter`, `_afkast`, `_positioner`, `_seneste_handel`. Push-besked ved handel: sæt `notify_service`, fx `notify.mobile_app_iphone`.

Botten skal holde sig kørende: står den stille mere end ca. en time, udløber login, og så skal du logge ind igen.
