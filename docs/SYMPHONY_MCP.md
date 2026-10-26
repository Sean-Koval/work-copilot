# Connect an OAuth-protected Symphony MCP server

The MVP includes a **real configurable remote MCP client**, using the official Python MCP SDK 1.30, Streamable HTTP, OAuth discovery, authorization code + PKCE, refresh-token handling, and a loopback browser callback. It does not use Symphony bot APIs. No connection to your workplace was made during development. A local fictional OAuth/MCP server exercises the protocol in tests; your company's endpoint, registration policy, scopes, tool names and response schemas remain unverified.

## Before connecting

Obtain approval to store the selected work data on this laptop. Ask your MCP administrator for:

- Streamable HTTP MCP endpoint (legacy SSE-only servers are not yet supported).
- OAuth scopes and whether dynamic public-client registration is allowed. If not, request a public `client_id` registered for `http://127.0.0.1:8766/callback`; confidential client-secret flows are not implemented.
- Read-only tool names, argument schemas and explicit permitted room/project scopes, pagination, stable IDs, revision timestamps, deletion markers, checkpoint/overlap semantics, and response shape.
- Permission and retention behavior. This prototype does not simulate complete upstream ACL enforcement or immediately purge revoked cached evidence.

Never paste access/refresh tokens, client secrets, or work messages into this repository. Config contains no credential values. Tools must both be locally allowlisted and advertise `readOnlyHint: true`; if the server does not expose this annotation, ask its administrator to verify and add it. An annotation is a server assertion, not a guarantee against a malicious server. No send/room-create tool is configured or called by this application.

## Private configuration

Save outside the checkout, for example `~/.config/work-copilot/symphony.json`:

```json
{
  "name": "work-symphony",
  "endpoint": "https://YOUR-APPROVED-MCP-HOST.example/mcp",
  "storage": "keyring",
  "scope": "ADMINISTRATOR-PROVIDED-SCOPES",
  "callback_port": 8766,
  "allowed_read_tools": [],
  "max_pages": 20
}
```

Install OS-backed credentials support with `python -m pip install -e '.[oauth-keyring]'` in the environment you use to run Work Copilot. The selected keyring must be a secure OS backend; configure it before connecting. No automatic fallback occurs. Plaintext, chained and unavailable keyring backends are rejected; select a supported secure OS backend explicitly. Credentials are isolated by data directory, connector profile/name and endpoint. On POSIX, an **explicit** `"storage": "protected-file"` alternative stores unencrypted OAuth tokens and client registration under the private app data directory, with directory mode 0700 and file mode 0600. This is not disk encryption. Protected-file mode is rejected on Windows. Use an OS keyring there.

`client_id` optionally supplies a pre-registered public-client ID. `client_metadata_url` optionally supplies an administrator-approved HTTPS client metadata URL. Only enable `"trust_env": true` if your approved corporate HTTP proxy/CA environment requires it (install `httpx[socks]` if using a SOCKS proxy). Otherwise network clients ignore proxy environment variables by default.

```sh
work-copilot connect --config ~/.config/work-copilot/symphony.json --authorize
```

Run on the laptop with an interactive browser. This explicit command may register the OAuth public client and opens your authorization server, then listens on **127.0.0.1 only** for up to three minutes. SDK checks state and PKCE; callback validates path, Host, single code/state, and issuer when supplied. Callback URLs/codes and token errors are not logged by the app. Inspect the returned tool schemas. Discovery calls no source-reading tool and leaves coverage in `catching_up`; cached evidence remains untrusted after an auth gap until collection succeeds.

Without `--authorize`, missing credentials fail closed without opening a browser or registering a client. Existing refresh credentials may refresh during collection. Expiry is persisted as an absolute time so restart does not extend token lifetime. Revoked credentials show `auth_required`; reconnect explicitly.

## Select one read tool and mapping

Only after verifying the actual schema, add `tool`, its identical entry in `allowed_read_tools`, and `arguments` that select approved scope. **The following names are placeholders, not Symphony API claims:**

```json
{
  "tool": "ADMINISTRATOR_VERIFIED_READ_TOOL",
  "allowed_read_tools": ["ADMINISTRATOR_VERIFIED_READ_TOOL"],
  "arguments": {"ADMINISTRATOR_VERIFIED_SCOPE_ARGUMENT": "SELECTED_SCOPE"},
  "cursor_argument": "ADMINISTRATOR_VERIFIED_CURSOR_ARGUMENT",
  "checkpoint_argument": "ADMINISTRATOR_VERIFIED_CHECKPOINT_ARGUMENT"
}
```

The default result envelope is `{ "items": [normalized_sources], "next_cursor": null, "checkpoint": "opaque" }`. Sources use the normalized JSON contract in [CONNECTORS.md](CONNECTORS.md). For servers returning a different shape, `mapping` supports dot-separated dictionary paths:

```json
{
  "mapping": {
    "items_path": "data.messages",
    "fields": {"id": "stableId", "revision": "revision", "body": "text", "title": "title", "observed_at": "timestamp", "room_id": "roomId"},
    "constants": {"provider": "symphony", "projects": []},
    "next_cursor_path": "data.next",
    "checkpoint_path": "data.checkpoint"
  }
}
```

This mapping is deliberately simple: no code evaluation, no model, no automatic schema guessing. You must ensure the source ID includes tenant/account namespace and remains stable, and revision is meaningful. Nested arrays, ID composition and domain-specific ownership/intent extraction may require an administrator-reviewed adapter or normalized JSON import. A room mention is not user ownership. Many rooms and many projects can be linked through the normalized source contract.

```sh
work-copilot collect --config ~/.config/work-copilot/symphony.json
```

A portable per-profile lock prevents overlapping browser/refresh/collection operations across CLI and background processes. Busy attempts leave connector health unchanged. Collection follows configured pagination, rejects repeated cursors, bounds pages/items, and atomically commits normalized items and checkpoint only after every page succeeds. Failure leaves the prior checkpoint for safe replay/catchup; no blind partial advancement. The source tool must honor the supplied checkpoint/overlap contract. Configure bounded history in tool arguments. Missing history/deletion support cannot be inferred by the client and must be disclosed by your adapter. The background service does not discover or authorize new sources on its own.

## Boundaries

- No external sending, agent self-approval, or write tool execution.
- No guarantee that your server's OAuth registration, scopes or Symphony tool schema matches these options until tested with your administrator.
- No complete remote ACL/retention system: local cached evidence needs your organization's review and deletion policy.
- HTTPS required except loopback fixture servers. Local disk/backup encryption remains an OS responsibility.
- Sanitized connection errors avoid exposing credentials; check endpoint/tool/scope/mapping and administrator server logs when collection fails.

Implementation reference: [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0). Tests run without accounts or paid model calls.
