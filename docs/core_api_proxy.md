# Core API over HTTPS

The Workspace Element requires a build of `core_api_https` that exports
`core_api_trust_pem`. The gateway resolves `core.<local-domain>` through
internal DNS and uses the same name for HTTP Host, TLS SNI, and certificate
hostname verification. The HTTPS scheme and port 443 are in the same Nginx
configuration as the hostname. Workspace API and events services send their
IAM requests through the gateway on loopback.

## Configure the Elements

1. Make `core_api_https` available in the Element repository and install it.
   Configure its certificate and key for the default internal Core hostname.
   Verify that this hostname resolves to Core inside the installation and that
   the HTTPS listener serves `/api/core/v1/` with certificate verification on.
2. For a self-signed certificate, Core supplies its public PEM automatically.
   If a CA issued the certificate, set `core_api_trust_pem` in Core Values
   Store to the public trust bundle for the issuing chain.
3. Apply the Workspace manifest. It imports `core_api_trust_pem` from Core and
   installs it on the gateway. The gateway waits for the bundle on first start.
   Verify Core and IAM through the gateway before sending traffic to the node.
   When the trusted bundle changes, Nginx validates the configuration before
   reloading it.

For a standalone developer process, `iam_endpoint` is configured in the local
`workspace.conf` independently of the Element.

## Regression checks

```sh
.tox/develop/bin/python -m pytest -q workspace/tests/unit/test_runtime_deployment_contract.py
```

On an isolated Realm, set `WORKSPACE_CORE_API_URL` to the internal HTTPS Core
origin and `WORKSPACE_GATEWAY_URL` to the Workspace gateway origin. For a
private CA or self-signed Core certificate, point `REQUESTS_CA_BUNDLE` to a PEM
bundle trusted by the test client for both origins. The test client and the
Workspace gateway use their own respective trust configuration.

```sh
.tox/develop/bin/python -m pytest -q workspace/tests/deployment/test_core_api_proxy.py
```

The checks cover the Core API, IAM token refresh, and IAM key discovery. They
use unauthenticated requests and require neither a user account nor tokens.
