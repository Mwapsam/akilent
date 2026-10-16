# Publishing Release Roadmap

**Current Date**: 2026-10-15  
**Current Status**: Steps 4.7A–4.7C Implementation Complete  
**Next Milestone**: Workstream A Staging Validation  

---

## Completion Status

### ✅ Implementation Complete (Steps 4.7A–4.7C)

| Step | Component | Status | Tests | Evidence |
|---|---|---|---|---|
| **4.7A** | Enqueue-failure recovery | ✅ DONE | 1 | View reset to FAILED on .delay() failure |
| **4.7A** | Stale-claim reconciler | ✅ DONE | 1 | Task escalates PUBLISHING → RECONCILING |
| **4.7B** | Confirm published action | ✅ DONE | 4 | Operator can resolve with evidence |
| **4.7B** | Retry action | ✅ DONE | 2 | Variant resets to FAILED |
| **4.7B** | Leave unresolved action | ✅ DONE | 2 | Audit records investigation step |
| **4.7B** | Audit logging | ✅ DONE | 1 | ReconciliationLog captures all actions |
| **4.7C** | Poll timestamp tracking | ✅ DONE | 1 | `last_poll_attempt` updated per poll |
| **4.7C** | Exhaustion detection | ✅ DONE | 1 | Task finds variants with stale `last_poll_attempt` |
| **4.7C** | Safe escalation | ✅ DONE | 1 | Metadata preserved, atomic transition |
| | **Total: 128 tests passing** | **✅** | | **111 base + 17 new** |

---

## Execution Order

### Phase 1: Staging Validation (2–3 days) — **CURRENT PRIORITY**

**Objective**: Verify implementations work together under real Celery, Redis, database, and provider behavior.

**Checklist**: 19 items across 5 phases (see `STAGING-VALIDATION-CHECKLIST.md`)

**Preconditions**:
- Staging environment isolated from production
- Redis running and accessible
- Celery workers active
- Instagram test account connected

**Gate Criteria** (all required):
1. ✅ No duplicate publications (reconciliation before retry)
2. ✅ Recovery survives process failure (worker restart, task state preserved)
3. ✅ Partial success stays partial (per-channel independence)

**Evidence Required**:
- Observed results with variant IDs
- Database state snapshots (before/after)
- Logs from failed/recovered scenarios
- Captured errors and timing

**Deliverable**: Staging validation report with evidence, blockers identified.

---

### Phase 2: Production Migration (Step 4.7D) — Starts after staging passes

**Objective**: Audit production, plan safe migration, ensure no data loss.

**Scope**:
1. Query production for posts without variants
2. Audit publication IDs and provider metadata
3. Decide: backfill needed? Can it be idempotent?
4. Document deployment sequence
5. Plan rollback procedure

**Gates**:
- All posts accounted for (no orphaned records)
- Migration is idempotent (safe to rerun)
- Rollback plan documented
- Stakeholders signed off

**Deliverable**: Production migration plan and runbook.

---

### Phase 3: Observability (Step 4.7E) — Final P0 blocker

**Objective**: Add metrics, logs, dashboards, and alerts for operational confidence.

**Components**:
1. **Metrics**: Stuck variants, exhaustion rate, reconciliation success rate
2. **Logs**: Structured logs for each state transition
3. **Dashboards**: Real-time publishing health overview
4. **Alerts**: Stuck claims, exhaustion, repeated failures, unresolved cases

**Gates**:
- Alerts fire for expected failure scenarios
- Operators can understand system state from dashboard
- Runbooks exist for each alert

**Deliverable**: Operational monitoring setup, runbooks.

---

### Phase 4: Production Release Decision

**Proceed Only When**:
1. ✅ Staging validation passes (all 19 items with evidence)
2. ✅ Production migration plan reviewed and approved
3. ✅ Observability in place with verified alerts
4. ✅ Team trained on recovery procedures
5. ✅ Rollback plan confirmed

**Release Approval**:
- Engineering: Technical readiness confirmed
- Product: Feature meets user requirements
- Ops: Monitoring and runbooks in place
- Leadership: Business decision to ship

---

## Three Critical Staging Tests

These are non-negotiable gates:

### 1. No Duplicate Publications

**Scenario**: Timeout during final publish → provider may have accepted request

**Test**:
- Simulate provider timeout during container publish
- Variant enters RECONCILING (ambiguous outcome)
- Operator investigates provider account
- Confirm variant is PUBLISHED with evidence
- **Assert**: Post appears exactly once in provider, no duplicates

**Evidence**:
- Variant ID: [id]
- Provider response: [evidence of published post]
- Timestamp: [when confirmed]

### 2. Recovery Survives Process Failure

**Scenario**: Worker crashes mid-polling

**Test**:
- Video in PROCESSING with container ID persisted in metadata
- Kill Celery worker
- Restart worker
- Verify polling resumes
- Container ID not lost
- `last_poll_attempt` timestamp updated post-restart

**Evidence**:
- Variant ID: [id]
- Container ID before crash: [id]
- Container ID after restart: [id] (same)
- `last_poll_attempt` before crash: [timestamp]
- `last_poll_attempt` after restart: [timestamp] (recent)

### 3. Partial Success Stays Partial

**Scenario**: Multi-channel post with one provider failing

**Test**:
- Create post with Facebook + Instagram
- Simulate Facebook API error during publish
- **Expected**: IG PUBLISHED, FB FAILED
- Retry only FB
- **Assert**: IG remains PUBLISHED (unchanged)

**Evidence**:
- Post ID: [id]
- IG variant: PUBLISHED ✅
- FB variant before retry: FAILED ✅
- FB variant after retry: PUBLISHED ✅
- IG variant after retry: PUBLISHED (unchanged) ✅

---

## What Counts as "Complete"

✅ **Complete**: Observed behavior recorded with variant IDs, database state, logs  
❌ **Not complete**: "UI looks right" or "unit test passes"

**Required evidence for each item**:
1. **Variant ID** (specific record tested)
2. **Database state** (SELECT query results)
3. **Behavior** (what happened, when, duration)
4. **Logs** (relevant entries from app logs)
5. **Screenshots** (UI state if visual)

---

## Timeline Estimate

| Phase | Duration | Blocking | Owner |
|---|---|---|---|
| **Phase 1: Staging** | 2–3 days | YES | QA/Eng team |
| **Phase 2: Migration** | 1–2 days | YES | Eng (post-staging) |
| **Phase 3: Observability** | 1–2 days | YES | DevOps/Eng |
| **Phase 4: Release decision** | 1 day | YES | Leadership |
| **Total to production** | **6–8 days** | | |

---

## Risk Mitigation

### Staging Isolation
- Use separate Redis instance (not production)
- Separate database (not production)
- Test Instagram account (not business account)
- Reset data before and after testing

### Broker Outage Test Safety
- Only in staging environment
- Have Redis restart plan ready
- Document DB state before outage
- Verify recovery via dbshell (not just UI)

### Worker Restart Safety
- Restart while polling in progress (not after completion)
- Monitor logs for resume
- Verify no missed polling cycles
- Confirm container ID preserved

---

## Known Limitations (Acceptable for Pilot)

1. **Manual reconciliation**: Operators check provider API themselves
   - *Acceptable*: Ambiguous outcomes rare (~0.1% of publishes)
   - *Future*: Auto-polling of provider status

2. **48-hour exhaustion threshold**: Fixed, not adaptive
   - *Acceptable*: Conservative timeout prevents false escalation
   - *Future*: Provider-specific timeouts per container type

3. **No automatic retry after exhaustion**: Requires operator intervention
   - *Acceptable*: Exhaustion indicates investigation needed
   - *Future*: Auto-retry with exponential backoff after investigation

---

## Decision Checkpoints

### After Staging (Go/No-Go)
```
[ ] All 19 checklist items complete
[ ] No duplicate publications observed
[ ] Process failures recovered correctly
[ ] Partial success verified
[ ] Evidence captured with variant IDs and DB state
[ ] Decision: PROCEED to Step 4.7D
```

### After Migration Planning (Go/No-Go)
```
[ ] Production data audit complete
[ ] Backfill migration (if needed) designed and tested
[ ] Rollback procedure documented
[ ] Idempotency verified
[ ] Decision: APPROVE migration plan
```

### After Observability (Go/No-Go)
```
[ ] Metrics collecting and dashboards live
[ ] Alerts tested and firing correctly
[ ] Runbooks written and team trained
[ ] Monitor silence configured for deployment
[ ] Decision: READY for production release
```

### Production Release (Final Go/No-Go)
```
[ ] Engineering sign-off: Technical readiness
[ ] Product sign-off: Feature meets requirements
[ ] Ops sign-off: Monitoring and runbooks in place
[ ] Leadership decision: Business approval
[ ] Execute: Production deployment
```

---

## What This Roadmap Guarantees

If all phases complete successfully:

✅ Publishing variants through correct channels (Facebook, Instagram, others)  
✅ Partial success handled (one channel fails, others succeed)  
✅ Ambiguous outcomes escalated safely (no blind retries)  
✅ Operator recovery workflows available (confirm/retry/postpone)  
✅ Process failures recoverable (worker crashes, broker outages)  
✅ All state transitions audited (who did what, when, why)  
✅ Per-channel independence enforced (no cross-variant side effects)  

**This does NOT yet guarantee**:
- Performance optimization (polling still synchronous)
- Advanced retry strategies (fixed backoff only)
- Automatic reconciliation (manual investigation required)
- Multi-tenant isolation (assumes single workspace)

---

## Current Status Summary

**Implementation**: 100% (Steps 4.7A–4.7C)  
**Testing**: 128 tests passing, no regressions  
**Staging**: 0% (NEXT PRIORITY)  
**Migration**: 0% (after staging)  
**Observability**: 0% (after migration)  
**Production**: Not ready until all gates pass  

**Next Action**: Execute Workstream A staging validation using the 19-point checklist.
