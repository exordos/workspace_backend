# Die Politik eines externen Providers erhalten

[← Hauptindex der Dokumentation](../../../../index.md) ·
[Index der Abfolge-Diagramme](../../README.md) ·
[Abschnitt Außenintegration/runtime](../README.md)

`GET /api/workspace/v1/messenger/external_provider_policies/{kind}`

Die gesundheitspolize des reals für einen providertyp lesen ..

![Folgebild](diagrams/get_external_provider_policy.svg)

[Ausgangsgestalt , die bearbeitet werden kann PlantUML](diagrams/get_external_provider_policy.puml)

## Anfrage

Es gibt keine weiteren Anforderungsparameter außer den oben genannten Variablen..

Titel zusätzlich zum Bearer-Token:

- Die Antwort enthält eine strenge `ETag` für `revision`.

Der Körper ist weg. JSON.

## Eine erfolgreiche Antwort

HTTP `200`:

```json
{
  "uuid": "bbf5398b-7d85-5770-aaf6-827605ca1200",
  "provider": "zulip",
  "enabled": true,
  "emergency_suspended": false,
  "limits": {
    "max_accounts": 100,
    "max_selected_chats_per_account": 1000,
    "max_file_bytes": 5368709120
  },
  "custom_ca_bundle": {
    "uuid": "40a917df-3c67-43a7-b5a3-d0ea38e24666",
    "generation": 4,
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "certificate_count": 1
  },
  "revision": 4,
  "created_at": "2026-07-01T09:00:00Z",
  "updated_at": "2026-07-17T12:12:00Z"
}
```

Die Antworten der Reviereinrichtungen enthalten strenge `ETag: "<revision>"`.

## Fehler

| HTTP | Verhalten in der Öffentlichkeit |
| --- | --- |
| `403` | `workspace.external_provider_policy.read` ist nicht zugelassen oder die Ressource befindet sich außerhalb des autorisierten Bereichs. |
| `400` | Für nicht zulässige Pfadwerte, Anfrageparameter oder Körper wird ein Standard-Validierungsfehler verwendet RESTAlchemy. |

Beispiel für Validierungsfehler:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

## Grenze RestAlchemy

Ziel-Ressource-/Kontrollanzeige (Angebotsdokumentation, nicht Produktionscode)):

```python
class ExternalProviderPolicy(models.ModelWithUUID, models.ModelWithTimestamp, orm.SQLStorableMixin):
    __tablename__ = "m_external_provider_policies_v1"

    provider = properties.property(types.Enum(PROVIDER_KINDS), required=True)
    enabled = properties.property(types.Boolean(), required=True)
    emergency_suspended = properties.property(types.Boolean(), read_only=True)
    limits = properties.property(types.Dict(), required=True)
    custom_ca_bundle = properties.property(types.AllowNone(types.Dict()), read_only=True)
    revision = properties.property(types.Integer(min_value=1), read_only=True)

    @classmethod
    def get_id_property(cls) -> dict[str, typing.Any]:
        return {"provider": cls.properties.properties["provider"]}


class ExternalProviderPolicyController(controllers.BaseResourceController):
    __resource__ = resources.ResourceByRAModel(ExternalProviderPolicy)
    # ResourceByRAModel restores by provider kind, not by the hidden storage UUID.
```

Die öffentliche Ressource wird an den `kind` Anbieter adressiert; die UUID Metadaten bleiben skalare UUID Eigenschaften. Wenn die benutzerdefinierten CA-Metadaten physisch normalisiert sind, verweist der `null` `custom_ca_bundle_uuid` verzeichnete Zugriff auf den geschützten CA-Paket mit `ON DELETE SET NULL`. Die öffentliche Anzeige RestAlchemy verwendet nicht `relationships.relationship` für JSON in der Form UUID, weil die Beziehung als URI serialisiert wird. An der Grenze der physischen Schema ist die kanonische nicht-polymorphe Verbindung `*_uuid` ein indexierter externer Schlüssel mit einer eindeutig ausgewählten Verweisungsaktion. Die Sanitizer verbergen den Besitzer, die Rohdaten, die Roh-Provider-ID, das geschlossene Zertifikat, die interne Adresse und die Protokollfelder.

## Synchrone Transaktion

1. Authentifizieren der Anfrage und definieren des Projekt-/Benutzereignisses IAM.
2. Überprüfen Sie den Weg, die Anfrage-Parameter und die erforderliche Berechtigung.
3. Führen Sie ein Indexlesen durch , wobei der Bereich aus der kanonischen Zeile oder der vormaterialierten Leseboden gespeichert wird.
4. Nur gesundheitsschützende öffentliche Felder serialisieren.

Eine Lesetransaction schreibt keinen Domain-Outbox, eine typische Projektionsvorgabe, einen gewünschten Statusbefehl oder ein bereites öffentliches Ereignis auf. Während der Anfrage führt sie keine `COUNT`, `GROUP BY`, korrelierte Unteranfrage, Fan-out-Bindung, Provider-Aufruf oder Cache-Feststellung aus.

## Hintergrundbearbeitung, Ereignisse und Übereinstimmung

Typisierte Projektionsvorgaben: keine.

Für diese Operation wird kein öffentliches Ereignis Workspace erstellt, so dass der einzelne Dispatcher WebSocket nichts zu liefern hat.

Absprache, die dem Kunden sichtbar ist: keine zusätzliche Verzögerung; die Antwort ist ein autoritatives festgehaltenes Bild.

## Idempotenz und Parallelismus

Für jede Providerart gibt es eine einzige Richtlinienzeile. Revision/ETag verhindert den Verlust von Updates; jede verändernde Operation führt zu einer eigenen unveränderlichen Aufgabe, und eine Wiederholung einer Aufgabe ist potentiell.

Die Wiederholungen verwenden stabile Geschäftsschlüssel und den aktuellen Ausgangszustand. Jedes immutable outbox event erstellt eine separate task mit einem einzigartigen `outbox_event_uuid`; die Wiederholung dieser task muss idempotent sein, coalescing fehlt. Die monopolistische Verarbeitung des Themas Messenger von neuen Eintragungen zu den alten wird nur dann angewendet, wenn die betroffene kanonische Platzierung tatsächlich auf `(project_id, topic_uuid)` bezieht; Provider-Administration/Leseoperationen erstellen kein künstliches Thema und gehören nicht zu dieser Schlange.

## Die Quellen

- [`workspace_api.md`](../../../../workspace_api.md) — Autoritätreiche öffentliche Routen, allgemeine JSON, Pagination, Ereignisse und Vertrag WebSocket.
- [`zulip_bridge_v1_product_and_api.md`](../../../../zulip_bridge_v1_product_and_api.md) — gesundheitsschützender Lebenszyklus von externen Ressourcen, Berechtigungen und Provider-Semantik.

[← Hauptindex der Dokumentation](../../../../index.md) ·
[Index der Abfolge-Diagramme](../../README.md) ·
[Abschnitt Außenintegration/runtime](../README.md)
