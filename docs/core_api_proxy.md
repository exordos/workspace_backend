# Core API proxy transport

Workspace proxies `/api/core/` to the Core node using the protocol and port of
Core's API virtual host, imported as `core_api_vhost`. The import points to
`$core.network.lb.$core_lb.vhosts.$core_lb_core_http`, which Core also exports as
`core_lb_http_vhost`. Its actual `protocol` and `port` determine the upstream
scheme and listener together.

Stock Core installations expose this API over HTTP. Their HTTPS listener can
serve a rejecting fallback or an unrelated virtual host, so an open TLS port
does not establish that the Core API is available there. A Core API virtual host
declared as HTTPS continues to use its declared TLS listener, including a
non-default port.

## Production and local installations

Production deployments require HTTPS on both public ingress and the
Workspace-to-Core hop. The Core API virtual host referenced by `core_api_vhost`
must declare the production HTTPS listener. An HTTP-only Core API is not a
supported production configuration.

HTTP is supported for an isolated installation on a developer's own computer,
where the local network between Workspace and Core is trusted. This development
case does not permit plaintext credentials across a shared or untrusted network.
Deployment operators must select the appropriate Core API virtual host; this
compatibility change reads its declared transport and does not classify a
deployment as production or development.

Public HTTPS termination is independent of this internal hop. Workspace keeps
the original Host header and `X-Forwarded-Proto: https`; the proxy does not
follow redirects or probe for a fallback transport. The forwarded header records
the public request scheme; it does not encrypt the upstream connection.

## Regression checks

The deployment contract tests render HTTP, HTTPS and non-default HTTPS ports:

```sh
.tox/develop/bin/python -m pytest -q workspace/tests/unit/test_runtime_deployment_contract.py
```

On an isolated Realm with Workspace installed, set `WORKSPACE_CORE_API_URL` to
the origin of the declared Core listener and `WORKSPACE_GATEWAY_URL` to the
Workspace gateway origin. For a private certificate authority, set
`REQUESTS_CA_BUNDLE` to its verified CA bundle. Run:

```sh
.tox/develop/bin/python -m pytest -q workspace/tests/deployment/test_core_api_proxy.py
```

Run this acceptance check on an HTTP-only Core with its rejecting HTTPS fallback
and on a Core API declared as HTTPS. Both must return the Core API JSON without
a redirect, and preserve the IAM response to a deliberately invalid refresh
token. The latter needs no account credentials and must produce an IAM client
error instead of a gateway error. These transport checks do not establish a
successful authenticated login or chat-loading acceptance.
