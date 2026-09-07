# Single sign-on

Stocktake can hand sign-in to an identity provider you already run — Authentik,
Authelia, Keycloak, Pocket ID. It acts as an OIDC **relying party**.

It is off by default and inert when off: no request is made to anybody until
somebody presses the button.

**Local passwords keep working.** That is deliberate — an identity provider
that is down should be an inconvenience, not a lock-out from your own records.

## Setting it up

Register Stocktake as a confidential client in your provider, then fill in
Admin → Settings, or `config.yaml`:

```yaml
auth:
  oidc:
    enabled: true
    issuer: https://auth.example.com/application/o/stocktake/
    client_id: stocktake
    client_auth: basic
    client_secret: from-your-provider
    redirect_uri: https://stocktake.example.com/login/oidc/callback
    button_label: Sign in with Authentik
    provisioning: 'off'
```

`redirect_uri` must match what your provider has registered, **exactly** — a
trailing slash counts. It is declared here rather than taken from the request,
because a value read from a header is one the caller chose.

The client secret is the one setting not editable on the settings page: a field
that renders its own value puts a live credential on screen.

## Public clients

If your provider issued a **public** client — one with no secret — say so:

```yaml
auth:
  oidc:
    client_auth: none
```

Then `client_secret` is not needed and is ignored if it is still there, so you
can move an existing confidential client across in two steps rather than one.
Nothing else changes: Stocktake always sends PKCE, so the proof that the code
is being redeemed by whoever asked for it is the same either way.

Authentik, Keycloak and Pocket ID all offer public clients. Some deployments
prefer them because there is then no shared secret at rest anywhere.

**It has to be said, not guessed.** Stocktake will not decide you meant a
public client because it cannot find a secret — a secret lost from the
environment would then downgrade sign-in to an unauthenticated token request
without anybody being told. `client_auth: basic` with no secret refuses to
offer sign-in and says which setting is missing.

## Who may sign in

`provisioning` is the whole policy:

| | Who gets in |
| --- | --- |
| `off` | Only accounts already linked to the provider |
| `invite` | Those, plus anyone holding a live [invitation](invitations.md) |
| `open` | Anyone your provider authenticates |

`open` is right for a household directory. It is worth saying out loud that it
means everyone in that directory can create an account here.

## Linking an account you already have

From your profile, under **How you sign in** → Single sign-on → Link. You will
be sent to your provider and brought back.

Accounts are matched on the provider's `sub` claim, **never on the email
address**. An address is something a directory can often be told, so matching
on it would be a way to sign in as somebody else. That is why linking an
existing account is something you do while signed in, rather than something
that happens by itself.

## Getting back in if the provider goes away

An account created by a provider has no password. It does have **recovery
codes**, issued when the account is made — the banner asks you to save a set on
first sign-in, and those codes set a new password at
[`/login/recover`](recovery-codes.md).

You can also set a password at any time from your profile, and an administrator
can reset one.

Unlinking your last provider is refused while you have no password, because an
account with no way in is not something a settings page should be able to
create quietly.
