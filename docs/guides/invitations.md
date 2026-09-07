# Invitations

An invitation is a link that lets somebody join one of your portfolios. They
can create an account with it, or use one they already have.

Portfolio owners create them from **Admin → This portfolio → Invite someone**.

## The link is a credential

Anyone who opens it gets the role it names, so treat it like a password. It is
shown **once**, when you create it — only a hash is stored, so it cannot be
displayed again. Create another if you lose it.

Each link:

- works **once**
- expires after **7 days**
- can be **withdrawn** at any time before it is used

The list on the Members page keeps used invitations, so "who did I let in, and
when" has an answer. Withdrawn ones disappear.

## What happens when someone opens it

**No account:** they choose a name, email and password, and land in the
portfolio at the role you picked. If [single sign-on](single-sign-on.md) is
configured they can use your identity provider instead of a password.

**An account already:** they sign in and the portfolio is added to what they
already have. Nothing they own changes — an invitation adds access, it never
replaces it.

If they are already a member, the invitation is still used up. It was offered
and answered, and a spent link that still worked would be a live credential
nobody remembers issuing.

## Roles

Choose the role when you create the link. An invitation **never changes an
existing membership**: sending a viewer link to someone who already owns the
portfolio will not demote them.
