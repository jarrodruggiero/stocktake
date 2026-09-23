---
name: tests-that-can-fail
description: How tests in this repo pass while proving nothing, and how to catch that before it ships. Use WHENEVER writing or reviewing a test, and before reporting that a change is verified. Companion to docs/contributing/testing.md, which says to break the code on purpose; this says what to look for.
---

# Tests that pass while proving nothing

[`docs/contributing/testing.md`](../../../docs/contributing/testing.md) sets the
rule: **break the code on purpose and watch the test go red.** This is the
companion — the shapes a useless test takes, each drawn from a real case in
this repository, so they can be spotted while writing rather than discovered by
a mutation hours later.

The framing that matters:

> **A mutation check is not a test of the code. It is a test of the test.**

A test that cannot fail is worse than no test. No test is an honest gap; a
green test is a claim, and the next person to touch that code will believe it.

## The one question

Before finishing any test, answer it concretely — not "does this pass" but:

> **If the code were wrong in the way this test is about, what exactly would
> this assertion see?**

If the answer is "the same thing", the test is decoration. Eight ways that
happens, all of them observed here.

---

## 1. The assertion matches something else on the page

The most common one, and it always looks fine.

```python
# WRONG — "Read only" is also in the top-bar portfolio switcher, on every page
assert "Read only" not in page

# RIGHT — scoped to the thing under test
assert "Read only" not in _target_options(page)
```

Happened three times in one session: a portfolio name found in the global
switcher; a notice's wording found inside the `(?)` tooltip that explains the
same thing; and a button's label found in the dialog heading below it.

**The tell:** asserting a *phrase* against a whole rendered page. Any string
worth asserting is probably worth asserting in one element. Write a small
helper that extracts that element, and say in its docstring what it excludes
and why.

## 2. The fixture never creates the state the bug would damage

```python
# WRONG — the stranger has an identity but no SESSION, so a logout keyed on
# the wrong subject has nothing to wrongly delete. Passes with the bug in.
db.add(ExternalIdentity(user_id=stranger.id, issuer=OTHER, subject=SAME))

# RIGHT — give them the thing the bug would destroy
auth.create_session(db, stranger, settings)
```

**The tell:** the test is about something being *preserved*, and the fixture
did not create it. Ask "what would the bug destroy, and is it present?"

## 3. The assertion is trivially true for another reason

```python
# WRONG — passes whether or not the AUD early return exists, because the
# query finds nothing anyway
assert fx_on_or_before(db, "AUD", date) is None

# RIGHT — the row is what makes it a test
fac.add_fx(db, "AUDAUD", "2026-09-21", "1.00")
assert fx_on_or_before(db, "AUD", date) is None
```

**The tell:** asserting `None`, `[]`, `0` or `False` on an empty database.
Arrange the data so the guard under test is the *only* thing standing between
the assertion and a different answer.

## 4. Only half a mechanism is tested

Back-channel logout keys on a `sid` captured at sign-in. The logout side had
nine tests; **nothing asserted the `sid` was ever stored.** Dropping it would
have failed nothing until a real provider sent a token naming a session the app
had never recorded.

**The tell:** a feature with a write side and a read side, a producer and a
consumer, an encode and a decode. Test both ends, and prefer one test that goes
through both over two that each stub the other.

## 5. The guard cannot fail because it is unreachable

Not a bad test — a bad *guard*, which a mutation surfaces as a survivor.

```python
# `oidc_sid` is written in exactly one place, always alongside `via_oidc`, so
# a local session holds NULL and can never match a sid. This clause is
# unreachable defence-in-depth.
UserSession.oidc_sid == notice.session_id,
UserSession.via_oidc.is_(True),      # <- no mutation can fail this
```

**Delete it and pin the invariant instead.** A guard no test can fail implies a
threat that does not exist, and the next reader will preserve it out of caution.

## 6. The sweep found nothing

Every guard that derives what it checks from the repository needs a **smoke
test that the sweep is non-empty.** Already a house rule, and it earned its
keep twice in one session: `test_workflows.py` found no workflows inside the
container (`.github/` was not copied in), and the doc-link guard would have
passed on an empty list.

```python
def test_there_are_workflows_to_check():
    """An empty sweep proves nothing — the guard's own smoke test."""
    assert len(WORKFLOWS) >= 2
```

Add the planted-violation test too: a guard never shown to fail is a guard with
no evidence behind it.

## 7. The thing under test is stubbed globally

`conftest._no_network` replaces `main._kick_feed` with a no-op for the whole
suite. So the call could be deleted from either instrument-creation route and
nothing would fail.

Two ways out, both fine, one trap:

```python
# Override the stub with a recorder
monkeypatch.setattr(main_module, "_kick_feed", lambda: seen.append(True))

# Or capture the REAL function at import, before conftest patches it
_REAL_KICK = main_module._kick_feed
```

**The trap:** asserting against the stub and not noticing. If both sides of a
guard fail identically, suspect you are testing a no-op.

## 8. The mutation did not actually apply

The check is only evidence if the edit landed on the code under test.

- `main.py` contains `raise HTTPException(404, "no such instrument")` **three
  times**. A `str.replace(old, new, 1)` hit the wrong one, the test passed, and
  it read as a missing test rather than a broken harness.
- A mutation producing invalid Python reports as a *collection error*, which is
  not the same as a caught mutation.

**Always assert the anchor is unique** (`assert source.count(old) == 1`), and
when it is not, target by line number. Read the result properly: `N failed` is
caught, `collection error` is a broken mutation, `N passed` is a survivor.

---

## Running a mutation check

```python
import pathlib, subprocess
p = pathlib.Path("app/thing.py"); orig = p.read_text()
assert orig.count(OLD) == 1          # the harness's own guard
p.write_text(orig.replace(OLD, NEW, 1))
r = subprocess.run(["uv", "run", "pytest", "tests/test_thing.py",
                    "-q", "--no-header", "-p", "no:warnings"],
                   capture_output=True, text=True)
p.write_text(orig)                   # ALWAYS restore
```

Mutate the **decision**, not the syntax: invert a comparison, drop a `where`
clause, remove a guard, return the wrong branch. `if False:` on a guard is the
cheapest honest mutation there is.

Do not edit source while a coverage run is in flight — coverage misattributes
lines and reports a failure that is not real.

## Two adjacent traps that cost a cycle each

**Never import a pytest fixture** from another test module. It shadows the test
parameter of the same name, ruff reports `F811`, and pytest resolves which wins
by luck. Declare it locally with a one-line comment, or promote it to
`conftest.py`. (Three occurrences in one session, the third after writing the
note about it.)

**An `ImportError` or `AttributeError` is not a RED.** Step 1 of test-first
means watching the test fail *on its assertion*. Stub the function to return
the wrong answer if that is what it takes — otherwise all that has been proven
is that the function does not exist yet.

## What to report

"Tests pass" is not evidence. Say which mutations were tried and what happened,
and **say when one survived** — a survivor that turned out to be a test bug is
the most useful thing in a report, because it is the part nobody else could
have known.

Do not quietly fix a survivor and report only the final green. The survivor is
the finding.
