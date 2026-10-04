# 3.2 documentation and website readiness

Baseline: `820a6ee` on `next-3.2-testing`; compare the complete branch against
`origin/main`, and the independently maintained static site on `origin/gh-pages`.
This prepares the release; it does not merge main or announce a tagged release.

## Plan

- [x] Inventory changes since main, distinguish current behavior from experiments
  removed later in the branch, and verify claims against implementation.
- [x] Update the user release summary and technical changelog: notifications,
  Trust and feedback, composer/session reliability, removed surfaces, skill
  recovery, maintenance performance, grading provenance and log retention.
- [x] Reconcile upgrade, API, configuration and operational guides, including
  schema v44, migration/recovery limits, hold-out denominators and job timestamps.
- [x] Update the gh-pages product site while preserving its design, domain and
  historical screenshots. Remove obsolete claims and provide clearly labeled
  3.2 preview links while main remains on 3.1.
- [x] Validate local documentation links/anchors, site links/assets, JavaScript,
  and desktop/mobile rendering and interactions. Record remaining limitations.
- [ ] Commit and push documentation to next-3.2-testing and site changes to
  gh-pages; verify GitHub Pages publication. Leave main unchanged.

## Review findings

- The public changelog has no 3.2 overview; the technical Unreleased section
  mixes removed intermediate experiments with the final branch behavior.
- The upgrade guide reports v43 rather than v44 and does not introduce the new
  exact skill-application journal or its legacy recovery restriction.
- Runtime reference pages need the final maintenance, grading and detached-job
  lifecycle details already established in operational audit records.
- The website still promotes adaptive policies/trials, Candor, Telos, nightly
  canary heartbeats, set_heartbeat and evaluate, and links an obsolete doc path.
- Site publication precedes main promotion, so preview versus stable installation
  and documentation must be explicit rather than implying 3.2 is released.

## Scope constraints

Preserve unrelated atra_findings files, deployment configuration, CNAME and site
assets. Historical release notes and dated audit records remain historical; do
not rewrite old events as if they described the current product. No application
behavior changes or Pernix service redeployment are needed for this task.

## Completed review and validation

Reviewed the complete main-to-testing history, including September's session,
worker, stream, scheduling and UI changes and October's removals and two audits.
Existing current guides for notifications, canaries, skills, spaces, MCP and
configuration were cross-checked; historical design plans remain historical.
Updated the two changelogs, README/index, installation/quickstart, contributing,
upgrade, API, skills, autonomy, canary/Trust and Reflect/Snooze guides. Added
`docs/operations.md` for maintenance diagnosis, retention and recovery.

- All **258** local documentation links and anchors resolve, including developer
  documents. Fixed the stale confirmation FAQ anchor.
- Static site checks at **1440×1000**, **768×1024** and **390×844** passed: 31
  anchor/link destinations, local assets and preview documentation targets; no
  page JavaScript errors or document horizontal overflow.
- Copy-install selects the preview branch; mobile menu navigation, API language
  tabs and screenshot tabs work. Desktop and phone hero/release layouts were
  visually inspected. Existing 3.1 screenshots stay labeled as historical.
- Diff whitespace checks passed in both branches. Application code is unchanged;
  this task uses documentation/site checks rather than repeating runtime tests.
  Runtime validation remains 4,250 passing tests / 79.31% coverage at `5faa480`.
- GitHub's public Actions history identifies the site publication workflow as
  `pages build and deployment` on gh-pages. The public release/latest endpoint
  returns 404, so no new tag/release is claimed here.

## At main promotion (separate release step)

1. Set the intended application version and release/tag metadata, then merge the
   reviewed branch to main. This documentation task does not perform that step.
2. Change the README/index/setup preview notices to release language, and remove
   explicit testing-branch checkout instructions when main contains the release.
3. Update gh-pages preview labels and install commands to main; switch its doc
   URLs from next-3.2-testing to main before retiring the testing branch.
4. Verify main's installation, migration and published doc links for the release.
