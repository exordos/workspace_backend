# Messenger nicht gelesenes Projektions-Upgrade und Rollback

Dieses Runbook wendet die durch die Migration eingeführte Abfrageplankorrektur an
`0149-split-messenger-unread-read-state-branches-c84ae9.py`. Nur die Migration
ersetzt Ansichtsdefinitionen; er schreibt keine Nachrichten- oder Lesestatusdaten neu.

Die Ausführung der Produktion erfordert eine separate Änderungsgenehmigung.
Verfahren erstmals auf einer isolierten Umgebung wiederhergestellt von einem Vertreter
Datenbanksicherung.

## Prüfung der Zugriffsvalidierung

Der Helfer für den begrenzten Zugang zum Stream liest nur den Stream, seine Benutzerbindung und
Die Kanonische Externe Zugriffsprojektion wird für Validierungen verwendet, die
nicht gelesenen Zählern zu verzehren:

- Validierung des Nachrichtenziels;
- Erstellung von Themen und Validierung von Themenanmeldungen;
- Erste Aktualisierung des Stroms und Validierung der Strommeldung;
- Erstellung von Ordner-Elementen;
- Überprüfung der Anbieter-Themen-Meldung.

Nachrichtenliste, die bereits durch die kanonische Benutzernachrichtenprojektion erfasst wird
und ruft den Stream-Snapshot-Helfer nicht an.ACLund
Die Reaktionswege halten absichtlich die volle sichtbare Nachricht
Schnappschuss, weil ihre Antwort, Ereignis und Provider-Nutzlast Nachricht verbrauchen
Vollständige Stream-Snapshots bleiben auch in den Leseaktionen, Bindung Ereignis Fan-out,
Streamschließung und Reaktion nach der Mutation/event
öffentliche Felder erforderlich sind.

## Voraussetzungen

1. Bestätigen Sie, dass das bereitgestellte Backend Migration `0149` enthält und dass `0148`
   der einzige Antragsteller.
2. Speichern Sie `pg_get_viewdef()` Ausgabe für diese Ansichten auf eine vom Bediener gesteuerte
   sichere Lage:
   - `m_workspace_user_unread_messages_base_v1`
   - `m_workspace_user_topic_unread_counts_v1`
   - `m_unread_user_messages`
   - `m_workspace_user_streams`
   - `m_workspace_user_topics_view`
   - `m_folders_view`
3. Nur aggregierte Zählungen des Lesezustands erfassen:

   ```sql
   SELECT COALESCE(mode, 'legacy') AS mode, COUNT(*)
   FROM m_workspace_read_state_projects_v1
   GROUP BY COALESCE(mode, 'legacy')
   ORDER BY mode;
   ```

4. Überprüfen Sie die ...PostgreSQLVerbindungsbudget.Messenger APIArbeitnehmer mit
   Pro-Prozess-Pool von höchstens zwei erfordern höchstens vier Messenger API
   Zählen Sie alle anderen Servicepools, aktive Wartungssitzungen,
   und reservierten Verbindungen vor der Ermöglichung des zweiten Arbeiters.
5. Erfassen der aktuellen 499/504 Rate, PostgreSQL Warteereignisse, temporäre Datei
   Zähler und p50/p95 für die genaue Streambildung, Streambildung und
   Anbieter-Serien anwenden und Verpflichtungsaufenthalte.
6. Bestätigen Sie, dass der Rollback-Befehl und die gespeicherten Ansichtsdefinitionen verfügbar sind
   Vor dem Stillstand.

## Aufwertung

1. - Ich bin nicht hier .Messenger APIVerkehr und Lieferantenlieferung.PostgreSQL
   Die Daten werden in einem anderen Migrationslaufwerk eingesetzt.
2. Nur die neue Migration mit begrenzten DDL Wartezeiten anwenden:

   ```bash
   PGOPTIONS='-c lock_timeout=5s -c statement_timeout=60s' \
     .tox/develop/bin/ra-apply-migration \
       --config-file <runtime-config> \
       --path migrations \
       --migration 0149-split-messenger-unread-read-state-branches-c84ae9.py
   ```

   Ein Timeout ist ein gescheiterter Einsatz, kein Grund, die Grenzen zu entfernen oder zu warten.
   Sie untersuchen die Sperrtransaktion und beginnen von vorne.
   Vorbedingungenprüfungen.
3. Bestätigen Sie, dass die Migrationszeile angewendet wird und die sechs abhängigen Ansichten angezeigt werden können
   mit `LIMIT 0` ausgewählt.
4. Start Messenger API und Provider-Zustellung. Bestätigen Sie, dass PostgreSQL Sitzungen
   für Messenger API und Anbieter unterschiedliche `application_name`-Werte verwenden
   Kontrolle.
5. Ausführen von authentischen Lese-allein-Prüfungen für Server-Einstellungen, Streamlisten,
   Sie können auch die Daten des E-Mail-Nachrichten-Systems und die Daten des E-Mail-Nachrichten-Nachrichten-Systems verwenden.
   Die Kommission hat die Kommission aufgefordert,
6. Ausführen `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` für desinfizierten Vertreter
   Abfragen für den genauen Strom und die Stromsammlung:
   - Die Vergangenheit verwendet
     `m_workspace_unread_flags_user_message_idx`;
   - Die genaue Zugriffsvalidierung wird nicht besucht `m_workspace_messages`.
   - Die genaue Suche schreibt keine temporären Blöcke.
7. Vergleichen Sie die Nach-Upgrade 499/504 Rate, Warteereignisse, temporäre Datei Delta,
   Genauere Suche p50/p95, Streambesammlung p50/p95 und Provider-Batch gelten
   und die Dauer der Daten mit der Basislinie festlegen.

## Rücklauf

Rollback ändert nur die Migration `0149`; nicht die Backend-Veröffentlichung oder
die Komprimierung im Lesestand als Vorfallumgehungsmethode ermöglichen.

1. Messenger API Verkehr und Anbieter Lieferung wieder still.
2. Gezwungene DDL wartet und nur Migration `0149` heruntergräbt:

   ```bash
   PGOPTIONS='-c lock_timeout=5s -c statement_timeout=60s' \
     .tox/develop/bin/ra-rollback-migration \
       --config-file <runtime-config> \
       --path migrations \
       --migration 0149-split-messenger-unread-read-state-branches-c84ae9.py
   ```

3. Vergleichen Sie die wiederhergestellten Definitionen von Basis und Topic-Count mit den gespeicherten
   Das Downgrade stellt den Mixed-Mode-Betrieb wieder her.
   Definitionen und ändert keine Daten des Lesestates.
4. Dienstleistungen starten und dieselben nurlesbaren Gesundheitsprüfungen und Aggregate wiederholen
   Telemetrie-Vergleich.

## Lokale Leistungsüberprüfung

Die Transaktionsbenchmark wird nur anhand einer Datenbank ausgeführt, deren Name
enthält `test`. Er ersetzt Ansichten und fügt Fixtureszeilen in eine Transaktion ein
vor dem Ausgang zurückgerollt:

```bash
WORKSPACE_TEST_DB_URL='<disposable-database-url>' \
  .tox/develop/bin/python \
    workspace/tests/scale/benchmark_unread_projection.py \
    --messages 250000 --unread 100
```

Die JSON Ausgabe lässt Abfragepredikaten und Fixure-Identifikatoren weg.
Die Daten werden in einem anderen System als dem, das in der CASSI-Test-Run-Archiv verwendet wird, gesäubert.
