# Kostenloser externer TradingBot-Scheduler

Status: **vorbereitet, aber NICHT aktiviert**. `main` laeuft weiterhin unveraendert. Das GitHub-Backup `backup/pre-scheduler-fix-2026-11-20` bleibt erhalten.

## Idee

Cloudflare Workers **Free** loest den bestehenden GitHub-Actions-Workflow von ausserhalb GitHubs aus. Der Python-TradingBot bleibt auf GitHub; Cloudflare fuehrt nur einen kurzen HTTP-POST an GitHub aus.

Drei Cron Triggers im kostenlosen Tarif (Maximum aktuell: 5 pro Account):
- `39 4,5 * * MON-FRI`: Segment 1, 00:39 New York
- `29 9,10 * * MON-FRI`: Segment 2, 05:29 New York
- `49 14,15 * * MON-FRI`: Segment 3, 10:49 New York

Jeder Trigger feuert im UTC-Winter- und Sommerzeitfenster. Der Worker prueft die **Zeitzone America/New_York** und ignoriert die unpassende Ausloesung. Damit wird die Zeitumstellung korrekt behandelt. Die eigentlichen Segmentstarts im Python-Bot bleiben 00:50, 05:40 und 11:00 New York. Umstellungen in Europa und den USA liegen nicht immer am selben Datum; Schweizer Uhrzeiten koennen in diesen Wochen abweichen.

## Einrichtung (Benutzeraktion erforderlich)

1. Kostenloses Konto unter https://dash.cloudflare.com anlegen. **Workers Free** waehlen; kein Paid-Upgrade.
2. Dieses Repository auf den Branch `feature/free-cloudflare-scheduler` auschecken.
3. Im Verzeichnis `scheduler/cloudflare` folgende Befehle ausfuehren:

   ```powershell
   npx wrangler login
   npx wrangler deploy
   ```

   `wrangler.jsonc` aktiviert zunaechst **nichts** (`ENABLED=false`).

4. Auf https://github.com/settings/personal-access-tokens/new einen **fine-grained token** mit Zugriff **nur auf DevV1ista/TradingBot** erstellen. Repository permissions: **Actions = Read and write**, sonst keine zusaetzlichen Schreibrechte. Token nicht committen und nicht im Chat teilen.
5. Im gleichen Ordner den Token als Cloudflare-Secret hinterlegen:

   ```powershell
   npx wrangler secret put GITHUB_TOKEN
   ```

6. Sicherer lokaler Code-Test ohne Token und ohne echte Trading-Ausfuehrung:

   ```powershell
   node test.mjs
   ```

## Sicherheitsbewusster Wechsel zum externen Scheduler

- Die Workflow-Aenderung von diesem Branch muss erst auf GitHub `main` landen, damit `workflow_dispatch` externe Segmentstarts versteht **und den Zustand in `state/` speichert**.
- In Cloudflare `PING_ONLY=true` und `ENABLED=true` einstellen, um zuerst nur den bereits vorhandenen Telegram-Ping zu testen. Keine Trading-Segmente werden in diesem Modus gestartet. Die Cloudflare-Cron-Events und die GitHub-Actions-Runs pruefen.
- Erst wenn das funktioniert: die alten `on.schedule`-Eintraege in GitHub `signals.yml` entfernen, **ohne den Workflow zu deaktivieren**. Sonst koennten GitHub und Cloudflare beide denselben Handelsabschnitt starten.
- Zum Live-Betrieb `PING_ONLY=false` setzen und neu deployen, `ENABLED=true` beibehalten.
- Den ersten echten externen `workflow_dispatch`-Run sowie das Speichern von `state/` in GitHub pruefen. Ein erfolgreicher HTTP-POST beweist nur, dass GitHub den Dispatch angenommen hat, nicht den erfolgreichen Trading-Lauf.

## Wichtige Hinweise

- Das Cloudflare-Token wird als Cloudflare-Secret gespeichert, **nicht im Repository**.
- Der Worker bietet keinen oeffentlich aufrufbaren Start-Endpunkt.
- GitHub Actions braucht weiterhin freie Laufzeit/Runner und kann Ausfuehrungen verzoegern.
- Ohne Cloudflare-Konto und den dort hinterlegten GitHub-Token ist diese Loesung **noch nicht produktiv**.
- Der Token sollte bei Nichtgebrauch widerrufen werden.
