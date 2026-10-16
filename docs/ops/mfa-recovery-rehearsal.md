# MFA recovery rehearsal (release gate for enforcement)

Enforcement of mandatory MFA stays off until every step below passes in the
target environment, and the evidence is recorded in the release ticket.

## Preconditions
- Enforcement flag is off (`MFA_ENFORCEMENT_ENABLED` not `1`, `MFA_ENFORCED_ROLES` empty).
- A test business user has a confirmed TOTP device and recovery codes.
- A second platform-staff operator is available who is not that test user.
- A support ticket reference exists for the rehearsal.

## Steps

1. **Authorization.** Run the reset with `--approved-by <other staff email>` and `--ticket <ref>`.
   Expected: fails closed if the approver is missing, is the target, or is not platform staff.
2. **Dry run.** Run without `--confirm`. Expected: prints the device and recovery-code counts and changes nothing.
3. **Reset.** Run with `--confirm`. Expected:
   - TOTP devices and recovery codes removed;
   - the user's sessions ended (the user's existing browser is logged out);
   - an `auth.mfa_operator_reset` audit entry with the ticket and approver.
4. **Notification.** The user receives the reset email referencing the ticket. Record delivery status from the mail provider.
5. **Next login.** The user signs in with their password and is sent to set-up (once enforcement is on for their role), enrols a new TOTP device, and completes a code prompt. Confirm no other route reaches a protected page without the factor.
6. **Evidence.** Record: environment, date and time, ticket, approver, target account, command output, audit entry ID, notification delivery result, and any deviation.

## Pass criteria
All six steps pass with no manual database edits. Any deviation blocks enforcement until it is fixed and the rehearsal is repeated.
