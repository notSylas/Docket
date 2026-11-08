"""Read-only mail connectors.

A connector pulls messages from a remote mailbox to this machine as plain
``.eml`` files inside a Docket-managed folder (``<data_dir>/connectors/<id>/
mail``). That folder is registered as an ordinary local source, so the
existing mail reader, evidence store, freshness/tombstone path and ingestion
pipeline index it unchanged. The remote side is never modified: the
``Connector`` protocol has no send/delete/move/flag method, and the Gmail IMAP
provider additionally runs behind a whitelist wrapper that refuses anything but
EXAMINE, SEARCH and FETCH BODY.PEEK.

Network use is confined to ``ConnectorService.sync`` (and ``docket connectors
sync`` / ``docket sync``) and to the connector's declared host
(``Connector.allowed_hosts``). Query, ingest and chat never construct a
connector.

Adding a provider (not implemented here)
----------------------------------------
Sync, storage and privacy logic only see ``Connector``: ``validate_config``,
``authenticate`` (returns an opaque credential reference, never a secret),
``list_changes(cursor)`` (a ``ChangeBatch`` of ``Change`` objects plus the new
cursor) and ``disconnect``.

* OAuth Gmail: ``authenticate`` runs a loopback/device OAuth flow with the
  read-only ``gmail.readonly`` scope and keeps the refresh token in the OS
  keyring, returning ``keyring:docket/<account>`` as the reference.
  ``list_changes`` stores the mailbox ``historyId`` as its cursor, calls
  ``users.history.list`` (messageAdded/messageDeleted, labelRemoved for
  folders out of scope) and ``users.messages.get(format=raw)`` for bodies;
  an expired historyId (HTTP 404) means a full rescan, like a UIDVALIDITY
  change. Allowed hosts: ``gmail.googleapis.com``, ``oauth2.googleapis.com``.
* Microsoft (Graph): ``authenticate`` uses MSAL (device code or auth code
  with ``Mail.Read`` only), token cache in the keyring. ``list_changes`` uses
  per-folder ``/delta`` queries, the ``@odata.deltaLink`` as cursor, ``$value``
  on a message for its MIME bytes, and ``@removed`` entries as deletions.
  Allowed hosts: ``graph.microsoft.com``, ``login.microsoftonline.com``.

Each yields ``Change(remote_id, folder, raw=<rfc822 bytes>)`` or
``Change(..., deleted=True)``; the service writes or removes the ``.eml``.
"""
