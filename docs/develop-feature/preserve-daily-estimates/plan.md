# Preserve daily usage estimates

The user wants a useful approximate daily usage chart and has directed that
minor source corrections must not erase accumulated usage. Implement the
collector correction described in the preceding investigation. The follow-up
authorizes the two ponytail-review simplifications, a local commit and deployment
of the updated Mac collector with a backup and live verification.

- Keep daily totals on quota decreases and conflicting observations. Skip the
  uncertain interval. Persist a high-water usage baseline so a downward
  correction and rebound cannot count the same allowance twice.
- Allow reset timestamp jitter up to five minutes from the fixed accounting
  anchor. Preserve estimates for overlapping calendar intervals, marking
  changed edge intervals partial. Do not move the anchor on each jitter sample.
- Recover older ambiguous state without clearing matching calendar totals.
  Larger reset changes still require a stable pair; real scheduled transitions
  and account isolation retain their existing behavior.
- Preserve totals if a proposed increment would exceed the protocol's 100-point
  bound. Skip that increment rather than blanking the chart.
- Upgrade private state to v4 with an exact pre-upgrade backup. Save one bounded
  previous-state backup before replacing an established calendar ledger.
  Public v2 responses and firmware remain compatible.
- Test corrections/rebounds, restart and interrupted publication, jitter and
  midnight edges, legacy recovery and migration/backup failures. Run the desktop
  gate and native firmware/display compatibility checks, then inspect the diff.

The chart remains an estimate of weekly allowance consumed per day. It does not
measure raw tokens, reconstruct erased totals, or backfill unobserved gaps.
