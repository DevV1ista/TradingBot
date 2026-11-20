# Trading-Bot: Einmal starten, automatisch fortsetzen

## Starten

1. Gehe zu [GitHub Actions / trading signals](https://github.com/DevV1ista/TradingBot/actions/workflows/signals.yml).
2. Klicke **Run workflow** und waehle den Branch **main**.
3. Lass das Feld **job** auf `start` stehen und starte den Run.
4. Du bekommst eine Telegram-Nachricht beim ersten Start und zu Beginn jedes neuen Handelstags (wenn die aktive Handelszeit beginnt).

Dein PC muss nicht laufen. Der GitHub-Workflow laeuft ca. vier Stunden, speichert die Handelsdaten in `state/` und startet ueber `GITHUB_TOKEN` automatisch den naechsten Run. Dafuer brauchst du keinen externen Account und keinen eigenen Token.

## Verhalten

- Alle Zeiten werden in `America/New_York` ausgewertet; Sommer-/Winterzeit ist beruecksichtigt.
- Der Bot laeuft waehrend der Handelszeiten in den drei bisherigen Segmenten (00:50–05:40, 05:40–11:00, 11:00–16:05 New York).
- Ausserhalb dieser Zeiten und am Wochenende pausiert er auf dem GitHub-Runner und wartet auf den naechsten relevanten Zeitpunkt.
- Bestehende Boersen-Feiertagsregeln und Handelssignale bleiben in `watcher.py` erhalten.
- Der Zustand wird mindestens am Ende jedes Runs und waehrenddessen alle fuenf Minuten (falls geaendert) committed.
- Ein `job=ping` prueft Telegram, ohne den Bot dauerhaft zu starten; `job=replay` ist der bisherige Rueckblick.
- `job=continue` ist nur fuer die automatische Fortsetzung vorgesehen.

## Fehler und Neustart (Stand 2026-11-20 abends)

- **Datenausfall** (Yahoo / Coinbase / Swissquote): Nach ca. 5 Minuten ohne Daten kommt eine Telegram-Warnung. Die Kette laeuft weiter, und sobald wieder Daten da sind, kommt "Kursdaten wieder da". Der Bot holt verpasste Minuten nach.
- **Telegram kurz nicht erreichbar**: Die Kette laeuft weiter, nur die Statusmeldung fehlt.
- **Selbstheilung** (`trading failure alert`, ereignisbasiert, funktioniert ohne Cron):
  - Endet ein Ketten-Run mit Fehler oder Zeitlimit und laeuft kein anderer Run, startet die Kette automatisch neu. Das passiert hoechstens 3x pro Stunde, danach kommt eine Warnung.
  - Wird ein Run abgebrochen, weil ein neuer `start` ihn ersetzt, kommt keine Warnung (kein Fehlalarm).
  - Wird ein Run von dir abgebrochen und kein anderer laeuft, kommt "Bot gestoppt", aber kein Neustart.
- **Waechter** (`job=watchdog`): Er startet die Kette nur, wenn sie nicht laeuft, und darf beliebig oft aufgerufen werden. Feste GitHub-Zeiten (cron) loesen in diesem Repo nachweislich nicht aus (Tests am 20.11.: 0 Ausloesungen in ueber 1 Stunde). Deshalb kann ihn ein kostenloser externer Zeitdienst aufrufen, siehe unten.

## Optional: externer Waechter (cron-job.org, kostenlos)

1. GitHub → Settings → Developer settings → Fine-grained tokens → **Generate new token**:
   - Repository: nur `TradingBot`
   - Permission: **Actions: Read and write**
   - Ablauf: z.B. 1 Jahr
2. Bei cron-job.org (kostenloses Konto) einen Cronjob anlegen:
   - URL `https://api.github.com/repos/DevV1ista/TradingBot/actions/workflows/signals.yml/dispatches`
   - Methode **POST**, alle 30 Minuten
   - Header `Authorization: Bearer <dein Token>`, `Accept: application/vnd.github+json`, `Content-Type: application/json`
   - Body `{"ref":"main","inputs":{"job":"watchdog"}}`

## Stoppen

Um die Kette anzuhalten: In GitHub Actions den laufenden `trading signals`-Run mit **Cancel workflow** abbrechen. Falls der naechste Run bereits angelegt ist, diesen ebenfalls abbrechen. Ein abgebrochener Run loest keine neue Fortsetzung aus, und auch der Waechter startet ihn danach nicht neu. Die technische Alternative ist **Disable workflow**, wodurch zukuenftige Workflow-Ausfuehrungen blockiert werden (spaeter wieder aktivieren).

## Grenzen

- GitHub-hosted Jobs koennen maximal sechs Stunden laufen; darum sind alle automatischen Abschnitte auf vier Stunden begrenzt.
- Die Kette kann unterbrochen werden, wenn GitHub keine neuen Runs startet, ein Runner hart abbricht, GitHub die Ausfuehrung einschränkt oder die API nicht antwortet.
- Ein fehlender Run kann **nicht sicher von GitHub Actions selbst** erkannt oder gemeldet werden. Fuer eine verlaessliche 24/7-Ueberwachung braucht es einen von GitHub unabhaengigen Kontrollprozess.
- Ein dauerhaft im Hintergrund laufender Handelsbot kann **GitHubs Nutzungsregeln fuer Actions** verletzen, die fuer Softwareentwicklung und -tests ausgelegt sind. GitHub kann Jobs oder das Konto einschraenken. Siehe [GitHub Actions Terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions).

## Technische Tests

- `python -m unittest -v test_self_runner`: lokale, netzwerkfreie Tests (10 Tests). Sie pruefen auch, dass Daten- und Telegram-Ausfaelle die Kette nicht stoppen.
- Die Selbstheilung und die Waechter-Entscheidungen wurden lokal mit simulierten GitHub-Antworten getestet: Kette laeuft, Uebergabe, Kette tot, Fehler, Stopp durch dich, Fehlerschleife.
- Zwei harmlose GitHub-Actions-Runs (`probe` und `probe_done`) haben die automatische Workflow-Weitergabe mit dem integrierten `GITHUB_TOKEN` bestaetigt.
- Die `state/`-Checkpointfunktion wurde auf einem separaten Feature-Branch mit einem echten Push getestet; die temporaeren Dateien wurden danach geloescht.

Backup vor dem Umbau: [backup/pre-self-running-bot-2026-11-20](https://github.com/DevV1ista/TradingBot/tree/backup/pre-self-running-bot-2026-11-20).
