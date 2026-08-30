[← Hauptindex der Dokumentation](../../../index.md) · [Index der Abfolge-Diagramme](../README.md) · [Inhalt und Benutzer Workspace](README.md)

# Abbildungen von Transaktionen mit Inhalt Workspace und Client-Zustand

Dieser Verzeichnis umfasst 35 öffentliche HTTP -Operationen zur Route-Detektion,
Server-Einstellungen, Ordner, Ordnerelemente, Entwürfe, Dateien, Dienste,
Für jede Operation gibt es eine Reihe von Push-Notifications-Geräten, Benutzer Workspace und `/me`.
Einzelbeschreibung des Vertrags, redaktionsfähiger Abfolge-Quell
PlantUML und lokal generiert SVG.

Der öffentliche Vertrag bleibt. [`workspace_api.md`](../../../workspace_api.md).
Die Projektion (projection) der Akten folgt der vereinbarten Ziellinie:
Die kanonische `FOLDER` ist in einer einzigen Kopie gespeichert, und der Zugriff,
Der persönliche Zustand und die bereitgestellten Aggregate der nicht gelesenen / Erwähnungen werden in
einzigartige `USER_FOLDER_BINDING`  Bindung des Benutzers an den Ordner.
`FOLDER_ITEM` Sie verbindet den Ordner mit einem unterstützten kanonischen Objekt, z. B.
`STREAM`, Die ungelesenen Felder der
Die Daten des Ordnerelementes kommen aus der einzigartigen `USER_STREAM_BINDING`.
UUID-Verweise bleiben skalare UUID-Eigenschaften, und physische Spalten —
Die einfachen Lesewege werden nur von den einfachen Schlüsseln verwendet.
Indexverbindungen und nicht `COUNT` während der Anfrage oder Umgehung von Nachrichten ausführen.

Systemordner sind `USER_FOLDER_BINDING` mit festgelegten Regel und Typ dargestellt:
Der Client kann diese Anbindung nicht löschen oder die Regel manuell ändern.
Fertiges automatisches `FOLDER_ITEM`  wiederherstellbares materialisiertes
Die Projektion wird von einem Worker unterstützt.
`USER_STREAM_BINDING` und kanonische `STREAM` mit `is_archived = false`:
`All chats` («Alle Chats) beinhaltet alle verfügbaren User-Flow-Streams;
`Personal` («nur Flüsse von `STREAM.private = true`;
`Channels` («Kanäle)  nur Ströme mit `STREAM.private = false`.,
Aktualisieren oder Löschen
Die Transaktions-Outbox schreibt die Strombindungen, aus denen die
Einzigartige immutable Task mit `outbox_event_uuid`;
`user-folder` Ich füge potenziell hinzu/entferne.
Automatische Elemente und erneuert die fertigen Aggregate
`unread_count`/`mention_count` in
`USER_FOLDER_BINDING`. Es werden keine neuen öffentlichen Aktionen eingeführt..

| Die Methode | Öffentlicher Weg | Spezifikation der Operation | PlantUML | SVG |
| --- | --- | --- | --- | --- |
| `GET` | `/api/workspace/v1/` |  [get_api_routes_index](operations/get_api_routes_index.md)  |  [Ausgang](operations/diagrams/get_api_routes_index.puml)  |  [Visualisierung](operations/diagrams/get_api_routes_index.svg)  |
| `GET` | `/api/workspace/v1/messenger/` |  [get_messenger_routes_index](operations/get_messenger_routes_index.md)  |  [Ausgang](operations/diagrams/get_messenger_routes_index.puml)  |  [Visualisierung](operations/diagrams/get_messenger_routes_index.svg)  |
| `GET` | `/api/workspace/v1/messenger/server_settings` |  [get_server_settings](operations/get_server_settings.md)  |  [Ausgang](operations/diagrams/get_server_settings.puml)  |  [Visualisierung](operations/diagrams/get_server_settings.svg)  |
| `GET` | `/api/workspace/v1/messenger/folders/` |  [get_folders_list](operations/get_folders_list.md)  |  [Ausgang](operations/diagrams/get_folders_list.puml)  |  [Visualisierung](operations/diagrams/get_folders_list.svg)  |
| `POST` | `/api/workspace/v1/messenger/folders/` |  [post_folders_create](operations/post_folders_create.md)  |  [Ausgang](operations/diagrams/post_folders_create.puml)  |  [Visualisierung](operations/diagrams/post_folders_create.svg)  |
| `GET` | `/api/workspace/v1/messenger/folders/{folder_uuid}` |  [get_folder](operations/get_folder.md)  |  [Ausgang](operations/diagrams/get_folder.puml)  |  [Visualisierung](operations/diagrams/get_folder.svg)  |
| `PUT` | `/api/workspace/v1/messenger/folders/{folder_uuid}` |  [put_folder_update](operations/put_folder_update.md)  |  [Ausgang](operations/diagrams/put_folder_update.puml)  |  [Visualisierung](operations/diagrams/put_folder_update.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/folders/{folder_uuid}` |  [delete_folder](operations/delete_folder.md)  |  [Ausgang](operations/diagrams/delete_folder.puml)  |  [Visualisierung](operations/diagrams/delete_folder.svg)  |
| `GET` | `/api/workspace/v1/messenger/folder_items/` |  [get_folder_items_list](operations/get_folder_items_list.md)  |  [Ausgang](operations/diagrams/get_folder_items_list.puml)  |  [Visualisierung](operations/diagrams/get_folder_items_list.svg)  |
| `POST` | `/api/workspace/v1/messenger/folder_items/` |  [post_folder_items_create](operations/post_folder_items_create.md)  |  [Ausgang](operations/diagrams/post_folder_items_create.puml)  |  [Visualisierung](operations/diagrams/post_folder_items_create.svg)  |
| `GET` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` |  [get_folder_item](operations/get_folder_item.md)  |  [Ausgang](operations/diagrams/get_folder_item.puml)  |  [Visualisierung](operations/diagrams/get_folder_item.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` |  [delete_folder_item](operations/delete_folder_item.md)  |  [Ausgang](operations/diagrams/delete_folder_item.puml)  |  [Visualisierung](operations/diagrams/delete_folder_item.svg)  |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/pin/invoke` |  [post_folder_item_pin](operations/post_folder_item_pin.md)  |  [Ausgang](operations/diagrams/post_folder_item_pin.puml)  |  [Visualisierung](operations/diagrams/post_folder_item_pin.svg)  |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/unpin/invoke` |  [post_folder_item_unpin](operations/post_folder_item_unpin.md)  |  [Ausgang](operations/diagrams/post_folder_item_unpin.puml)  |  [Visualisierung](operations/diagrams/post_folder_item_unpin.svg)  |
| `GET` | `/api/workspace/v1/messenger/drafts/` |  [get_drafts_list](operations/get_drafts_list.md)  |  [Ausgang](operations/diagrams/get_drafts_list.puml)  |  [Visualisierung](operations/diagrams/get_drafts_list.svg)  |
| `POST` | `/api/workspace/v1/messenger/drafts/` |  [post_drafts_create](operations/post_drafts_create.md)  |  [Ausgang](operations/diagrams/post_drafts_create.puml)  |  [Visualisierung](operations/diagrams/post_drafts_create.svg)  |
| `GET` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` |  [get_draft](operations/get_draft.md)  |  [Ausgang](operations/diagrams/get_draft.puml)  |  [Visualisierung](operations/diagrams/get_draft.svg)  |
| `PUT` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` |  [put_draft_update](operations/put_draft_update.md)  |  [Ausgang](operations/diagrams/put_draft_update.puml)  |  [Visualisierung](operations/diagrams/put_draft_update.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` |  [delete_draft](operations/delete_draft.md)  |  [Ausgang](operations/diagrams/delete_draft.puml)  |  [Visualisierung](operations/diagrams/delete_draft.svg)  |
| `GET` | `/api/workspace/v1/messenger/files/` |  [get_files_list](operations/get_files_list.md)  |  [Ausgang](operations/diagrams/get_files_list.puml)  |  [Visualisierung](operations/diagrams/get_files_list.svg)  |
| `POST` | `/api/workspace/v1/messenger/files/` |  [post_files_create](operations/post_files_create.md)  |  [Ausgang](operations/diagrams/post_files_create.puml)  |  [Visualisierung](operations/diagrams/post_files_create.svg)  |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}` |  [get_file](operations/get_file.md)  |  [Ausgang](operations/diagrams/get_file.puml)  |  [Visualisierung](operations/diagrams/get_file.svg)  |
| `PUT` | `/api/workspace/v1/messenger/files/{file_uuid}` |  [put_file_update](operations/put_file_update.md)  |  [Ausgang](operations/diagrams/put_file_update.puml)  |  [Visualisierung](operations/diagrams/put_file_update.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/files/{file_uuid}` |  [delete_file](operations/delete_file.md)  |  [Ausgang](operations/diagrams/delete_file.puml)  |  [Visualisierung](operations/diagrams/delete_file.svg)  |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}/actions/download` |  [get_file_download](operations/get_file_download.md)  |  [Ausgang](operations/diagrams/get_file_download.puml)  |  [Visualisierung](operations/diagrams/get_file_download.svg)  |
| `GET` | `/api/workspace/v1/services/` |  [get_services_list](operations/get_services_list.md)  |  [Ausgang](operations/diagrams/get_services_list.puml)  |  [Visualisierung](operations/diagrams/get_services_list.svg)  |
| `GET` | `/api/workspace/v1/services/{service_uuid}` |  [get_service](operations/get_service.md)  |  [Ausgang](operations/diagrams/get_service.puml)  |  [Visualisierung](operations/diagrams/get_service.svg)  |
| `PUT` | `/api/workspace/v1/push_devices/{registration_uuid}` |  [put_push_device](operations/put_push_device.md)  |  [Ausgang](operations/diagrams/put_push_device.puml)  |  [Visualisierung](operations/diagrams/put_push_device.svg)  |
| `DELETE` | `/api/workspace/v1/push_devices/{registration_uuid}` |  [delete_push_device](operations/delete_push_device.md)  |  [Ausgang](operations/diagrams/delete_push_device.puml)  |  [Visualisierung](operations/diagrams/delete_push_device.svg)  |
| `GET` | `/api/workspace/v1/users/` |  [get_users_list](operations/get_users_list.md)  |  [Ausgang](operations/diagrams/get_users_list.puml)  |  [Visualisierung](operations/diagrams/get_users_list.svg)  |
| `GET` | `/api/workspace/v1/users/{user_uuid}` |  [get_user](operations/get_user.md)  |  [Ausgang](operations/diagrams/get_user.puml)  |  [Visualisierung](operations/diagrams/get_user.svg)  |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/presence/invoke` |  [post_user_presence](operations/post_user_presence.md)  |  [Ausgang](operations/diagrams/post_user_presence.puml)  |  [Visualisierung](operations/diagrams/post_user_presence.svg)  |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_upload/invoke` |  [post_user_avatar_upload](operations/post_user_avatar_upload.md)  |  [Ausgang](operations/diagrams/post_user_avatar_upload.puml)  |  [Visualisierung](operations/diagrams/post_user_avatar_upload.svg)  |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_reset/invoke` |  [post_user_avatar_reset](operations/post_user_avatar_reset.md)  |  [Ausgang](operations/diagrams/post_user_avatar_reset.puml)  |  [Visualisierung](operations/diagrams/post_user_avatar_reset.svg)  |
| `GET` | `/api/workspace/v1/me/` |  [get_me](operations/get_me.md)  |  [Ausgang](operations/diagrams/get_me.puml)  |  [Visualisierung](operations/diagrams/get_me.svg)  |

Umfang: **35 HTTP-Operationen**.

[← Hauptindex der Dokumentation](../../../index.md) · [Index der Abfolge-Diagramme](../README.md) · [Inhalt und Benutzer Workspace](README.md)
