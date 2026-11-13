# Signing in: passwords, single sign-on, and getting back in

OpenNVR has three ways in and two ways back:

| | Where | Who sets it up |
|---|---|---|
| Username + password + TOTP | the login page | each account (TOTP enrolled on first login) |
| **Single sign-on** (OpenID Connect) | *Continue with …* on the login page | a superuser, Access Control → Authentication |
| Administrator reset | Access Control → Users → Edit → *New password* | a superuser, with their TOTP code |
| **E-mailed reset** (*Forgot password?*) | the login page | a superuser configures the mail server once |

Every password change or reset ends the account's other sessions: tokens
carry the account's password generation and stop working when it moves.

## Single sign-on (OpenID Connect)

Any provider with a discovery document works: **Dex**, Authentik,
Keycloak, Zitadel, Microsoft Entra ID, Google Workspace, Okta. The flow
is the authorization code grant with PKCE; the provider's `id_token` is
verified against its published keys (issuer, audience, signature, nonce,
expiry). Tokens never travel in a URL: the callback hands the browser a
one-time code that it swaps for the session over POST.

Access Control → Authentication → *Identity provider*:

- **Issuer** — the provider's base URL (`https://dex.example.com`). *Test
  provider* fetches its discovery document and keys and shows what it
  supports before anyone depends on it.
- **Client ID / secret** — from the client you register with the
  provider. The secret is stored encrypted and never shown again.
- **Redirect URI** — shown on the page, register it with the provider:
  `https://<your site>/api/v1/auth/oidc/callback`.
- **Create accounts on first sign-in** — new people get the **default
  role**; otherwise only accounts that already exist (linked by e-mail)
  can sign in.
- **Role claim / role map** — a claim from the provider (`groups` by
  default) mapped to OpenNVR roles, one `value = role` per line;
  **superuser values** make a superuser. The mapping is applied on every
  sign-in, so a change at the provider takes effect at the next login.
- **Allow password sign-in** — off makes the site single sign-on only.
  Superusers can always sign in with a password, so a broken provider
  never locks the operators out.

A single sign-on account skips the TOTP wall: the provider owns MFA.

### Linking existing accounts

The first single sign-on from a person whose e-mail matches an existing
OpenNVR account links the two, and from then on that account opens
without its password or TOTP. So linking is narrow:

- The provider must say it **verified** the address (`email_verified:
  true` in the id_token or userinfo). Providers that let users edit their
  own e-mail and send no such claim, such as Entra ID, never link
  existing accounts; with *Create accounts on first sign-in* on, new
  people still get accounts.
- **Superusers are never linked**, verified address or not. They sign in
  with their password and TOTP; give an operator single sign-on through
  a separate account, or a superuser value in the role map.
- A linked account stays a password account: its password, TOTP and the
  e-mailed reset keep working.

### Dex, self-hosted, next to OpenNVR

```yaml
# docker-compose.dex.yml — one static user; swap the connector for LDAP, GitHub, Google…
services:
  dex:
    image: ghcr.io/dexidp/dex:v2.41.1
    command: ["dex", "serve", "/etc/dex/config.yaml"]
    ports: ["5556:5556"]
    volumes: ["./dex-config.yaml:/etc/dex/config.yaml:ro", "dex_data:/var/dex"]
volumes:
  dex_data: {}
```

```yaml
# dex-config.yaml
issuer: https://dex.example.com
storage: { type: sqlite3, config: { file: /var/dex/dex.db } }
web: { http: 0.0.0.0:5556 }
oauth2: { skipApprovalScreen: true }
staticClients:
  - id: opennvr
    name: OpenNVR
    secret: change-me
    redirectURIs: ["https://nvr.example.com/api/v1/auth/oidc/callback"]
enablePasswordDB: true
staticPasswords:
  - email: ops@example.com
    hash: "$2a$10$..."          # bcrypt
    username: ops
    userID: 08a8684b-db88-4b73-90a9-3cd1661f5466
```

In OpenNVR: issuer `https://dex.example.com`, client `opennvr`, the
secret, scopes `openid email profile groups`, role claim `groups`.
NetBird ships Dex the same way; the configuration above is the minimum.

### How others do it

Milestone and Genetec take identities from Active Directory / Entra;
UniFi Protect uses the Ubiquiti account; Frigate and Scrypted have no
provider of their own and rely on an authenticating proxy (oauth2-proxy,
Authelia). OpenNVR speaks OIDC itself, so no proxy is needed and the
roles still live here.

## E-mailed password reset

Access Control → Authentication → *Mail server*: SMTP host, port,
STARTTLS/TLS, credentials, the From address. *Send test* proves it. With
a server configured, *Forgot password?* on the login page takes an
address and sends a single-use link that is valid for 30 minutes; the
page answers the same way whether or not the address exists. Without a
server the link says to ask an administrator, which is the truth.

The link opens `/reset-password`; a new password there follows the site
password policy, ends the account's other sessions, and clears a
lockout. Single sign-on accounts have no password to reset.

## Administrator reset

Access Control → Users → Edit → *New password*, with the administrator's
own TOTP code. Audited as `password_reset`. The user's other sessions
end at once.
