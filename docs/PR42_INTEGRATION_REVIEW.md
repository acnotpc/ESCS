# PR #42 integration review — 16 October 2026

Verdict: keep draft. Mergeability is not operational readiness.
Reviewed head d538875249865480edb3a0b0c6f89e2468db49ea. PR #43 is merged at
8ceab84c116e6e6b9e832821bb48f5fb43f8bc15; its deployment is unverified.

## Implemented in this revision

- Require unique FMS staff IDs and explicit identity_verified=true in private map
  rows. Reject identity/name disagreement, ambiguous canonical candidate names,
  nonfinite coordinates and evidence more than 120 seconds ahead of the plan.
  Matching names alone no longer establishes identity. Aliases must be reconciled
  by an operator before constructing the runtime row with the current FMS name.
- Explicit operator teams do not depend on unused optional proximity evidence.
- Add pure meeting_preview.py: verified meeting point, route origin/destination,
  road mode, source, observation time and departure validity interval required.
  Road duration must include reviewed traffic and contingency. Calculation uses
  UTC arithmetic and renders Europe/London dates and offsets, including midnight.
- Meeting = D1 required time minus 15 minutes early arrival minus verified road
  minutes minus 15 minutes vehicle checks. Check the whole outbound interval
  against eligible_window; retain pending reviews and never clear dispatch.
- Authenticated POST /team-sync/daily/meeting-preview invokes the calculation without saving evidence, changing plans or publishing. Dockerfile includes the module.
- proximity_required=true blocks the legacy fallback and validates map freshness at plan validation/publication. Dated previous-day colour evidence supports mixed fresh/continuing crew warnings; absent evidence requires review.
- Validation: 77 focused tests passed after rebase onto main, including the #43 planning preview, authentication and no-write regression tests.

## Required before finalising

1. Rebase onto any newer main before merge and repeat focused daily teams, colour,
   crew-readiness, planning-preview and endpoint regression tests. This revision includes #43. Do not treat
   the GitHub mergeable field as evidence of semantic compatibility.
2. Obtain the private runtime snapshot. Verify map_reference exactly matches each
   saved plan; observation age is now checked at plan validation/publication time. Reconcile every available regular staff ID and canonical name to
   a verified pin. Check duplicates, missing pins and current base changes.
   No live map snapshot or runtime environment was accessed in this review.
3. Set proximity_required=true in each saved plan requesting proximity. Legacy
   callers retain seniority grouping; requested proximity rejects absent/incomplete
   evidence rather than silently falling back.
4. Associate each proposed team/job with an operator-verified meeting_point_id,
   labelled address, vehicle location and D1 destination ID. Supply a reviewed
   road route valid for the calculated departure. Never infer an office from
   a crew centroid. Check each staff member's travel to the point (ideally <=30
   minutes); an alternative point remains a suggestion for Control Room/crew.
5. Exercise the new authenticated meeting-preview endpoint with real verified
   evidence and expose its results in the consuming team-list UI/process. Missing
   route/point evidence returns a sanitized 422; never substitute invented minutes.
   The endpoint and Docker COPY are implemented. Dispatch readiness stays false.
6. Feed dated previous/current/next-day rota evidence into shared
   crew_readiness.planning_checks. Preserve FMS regular groups and explicit teams;
   prefer keeping verified pink-to-blue 06:00 fresh starters together. Flag mixed
   crews for review when preserving group boundaries conflicts with this preference.
   Candidate now supports previous_day_marker with dated, fresh Plan evidence.
   Meeting preview flags mixed crews but does not regroup or change assignments.
   Blue-to-pink staff have a next-day 06:00 finish target, not an availability
   guarantee. Preserve continuing crew/actual duty start and uncertain release.
7. Before job allocation, check actual release, commitments, full duty/rest,
   qualified driver/current licence, vehicle readiness, D1-D2 duration, A&E waits,
   return journey, final meeting-point return and next booking. Meeting preview
   checks only the outbound interval. No demographics or driver status inferred.
8. Add end-to-end preview tests for missing map, ID aliases/duplicates, stale
   map at publication, group boundaries, operator override, mixed fresh starters,
   blue/pink and light-blue commitments, unknown actual return, duty >12h and
   11h rest, release after 06:00, missing/mismatched/stale route, traffic validity,
   DST transitions, return jobs and operator-post preservation. Existing tests
   establish portions of these behaviours, not their integrated correctness.

## Runtime configuration and verification

BITRIX_DAILY_TEAMS_BASE_MAP is private JSON:
{"map_reference":"exact saved plan reference","observed_at":"timezone-aware ISO timestamp",
 "staff":[{"staff_id":"verified FMS ID","name":"current FMS display name",
 "identity_verified":true,"latitude":0,"longitude":0}]}
Coordinates above illustrate schema only, not operational evidence.
Do not commit real coordinates, home addresses or roster snapshots.

Saved plans require current complete FMS roster/source/observation, map reference
and map_checked_at, unique IDs, verified group/profile and dated next-day evidence.
Blue indicates working; pink, sick, holiday and training are excluded. Light blue
requires its commitment to be checked; unknown never establishes availability.
A proximity team is still only a provisional grouping.

Preview with BITRIX_DAILY_TEAMS_WRITE_ENABLED=false. Inspect ENABLED, restricted
DEST, AUTHOR_ID and ROSTER_SNAPSHOT against the actual deployment. Verify deployed
commit and authenticated status/preview before any separately authorised write
activation. Existing operator-managed team posts must remain protected.
Merging this PR neither establishes deployment nor enables publication.

## Meeting-preview request

Use the existing connector authentication with JSON keys plan, staff_ids, point,
route, d1_id and required_at. point requires id, label and verified=true; route
requires origin_id, destination_id, mode=road, verified=true, source, observed_at,
valid_from, valid_until and positive finite duration_minutes. Route observation
limit is 24 hours; the validity window must cover calculated departure.
Plan previous-day evidence requires previous_day_date, previous_day_observed_at
and previous_day_source_reference when any previous_day_marker is supplied.
Times must include timezone. All examples/tests use synthetic data.
