# Recovery-code policy

Status: in force for the 2FA release, pending the browser checks listed in the release notes.

## Where a recovery code is accepted

| Action | Accepts a recovery code | Why |
|---|---|---|
| Sign-in second step | Yes, on its own page (`/auth/2fa/recovery/`) | The lost-device path. The six-digit field never takes one. |
| Turn off two-factor | Yes, in place of an app code, with the password | A user who has lost their phone can still remove the factor they can't use. |
| Make new recovery codes | Yes, in place of an app code, with the password. Requires an app | Same reason: the user can still prove possession of the saved codes. |
| Remove a passkey | No. Needs the password and an app or emailed code | Removing a passkey is not a recovery action. |

## Rules that apply to every redemption

- Each code works once. A successful redemption is a single conditional update, so two concurrent requests cannot both spend the same code.
- A wrong or consumed code counts against the same throttle as a wrong app code.
- A code is stored only as an HMAC digest keyed with `SECRET_KEY`. Plaintext exists only in the display window described below.
- Codes are never written to logs, audit records, or error messages.

## Display window

- A new set is held encrypted in the user's session for 10 minutes.
- Reloading the codes page shows the same set. Nothing regenerates on reload.
- The set is removed when the user confirms they saved it, or when the window expires.
- If the stored set cannot be decrypted or is malformed, nothing is shown and nothing is regenerated automatically.

## Regeneration

- Requires an enrolled authenticator app, the password, and a current app code or unused recovery code, all checked on the server.
- Email codes never authorise regeneration. Accounts without an app cannot be given recovery codes.
- Wrong passwords are limited to 5 per account in 15 minutes. Once the limit is reached the password is not checked, even when correct.
- The old set stops working in the same transaction that creates the new one.
- A row lock on the user serialises concurrent regenerations, so only one valid set can remain.

## Notifications and audit

- After a regeneration or a turn-off has committed, the account owner is emailed. The email names the change and the time, and never includes a code or secret.
- A delivery failure is logged and audited. It does not reverse the change, and the user is not told that the email was sent.
- Audit records exist for code generation, regeneration, consumption, failed attempts, and two-factor disablement.

## Pages

- The recovery page shows nothing about the account until the code is accepted, including how many codes remain.
- Passwords for sensitive changes are limited per account, so a hijacked session cannot guess them without bound.

## Known limits

- Someone with a password and the user's recovery codes can turn two-factor off. The notification email is how the owner learns of it.
- Recovery codes are invalid after `SECRET_KEY` rotation (see the backup and key-rotation runbook).
