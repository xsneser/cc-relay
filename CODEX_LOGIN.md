# Codex Web Login

The Codex upstream in **Connection Configuration** has a **Codex Login** button.
It starts CLIProxyAPI, opens its OpenAI authorization URL, and polls the result.
If a browser blocks the new tab, the same authorization URL remains available
as a link beside the login status. Complete authorization in the browser;
the UI never accepts an account password or an OAuth token.

## Runtime

- CLIProxyAPI executable: `codex-proxy/cli-proxy-api.exe`.
- Pinned version tested: `v7.2.155`, Windows AMD64.
- Release archive SHA256:
  `b24d8f443c4cdc6229a6d1a769476328a3290512dd1f5f26a03c181c3e8e15b5`.
- Independent configuration and credentials: `codex-proxy/ui-auth-runtime/`.
- Upstream service: `127.0.0.1:8317`.
- Temporary callback server: `127.0.0.1:1455`, maximum five minutes.

The executable is a separately installed upstream dependency, not committed
to this repository. Download it from the official CLIProxyAPI GitHub release
and verify its checksum before installation.

Existing external Codex proxy configurations are not overwritten. A conflicting
callback port is reported, not terminated. Official Codex credential files are
never imported or modified. Logging in does not change the selected relay route.
The independent credential directory and management secrets are gitignored.

The relay talks to CLIProxyAPI's authenticated management API on loopback.
The callback bridge uses that API rather than the upstream WebUI forwarder,
which otherwise binds the callback to all network interfaces in this version.
OAuth state is validated at both the bridge and the upstream service.

## Verification

Run `python -m unittest discover -s tests` for regression tests. To test the
real local service with disposable configuration, run:

```powershell
python diagnostics/codex_login_smoke.py --ui-port 8611 --proxy-port 18317
```

The fixture uses a temporary directory and no production account data. Stop
it with a POST to `http://127.0.0.1:8611/_test/stop`; it also expires after
15 minutes. Actual account authorization and a subsequent model request are
separate manual acceptance steps. Browser UI/backend smoke tests alone do not
prove that the account's subscription, network, or model access will work.

Backend changes require restarting cc-relay. Merely refreshing `ui.html` does
not upgrade a running Python process or an older packaged executable.
