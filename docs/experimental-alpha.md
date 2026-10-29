# Public source release decision

On 2026-10-29, the owner approved publishing the audited current source and
improving it in public, accepting the known auxiliary-summary accuracy defect.
This decision supersedes the source-publication eligibility requirements in the
historical checklist below, including its rule blocking a candidate for an
unsupported accepted full artifact. It does not change runtime review barriers,
security controls or the strict production checker.

The published runtime source is based on audited commit
`72793fb25df58b82c86b478254ddf80fdbdf338d`, followed only by release-documentation
updates. The full-profile probe completed and saved output, but independent
review rejected a Signal for omitting a material vehicle-category restriction.
This was an accepted inaccurate artifact, not a safely withheld result. Story
cards passed review; auxiliary summaries remain outside automatic grounding
review. Extending review to those sections and testing qualifier preservation
remains open work.

Only the audited source history and these documentation updates are authorized
for the new public repository. Private configuration, credentials, source
captures, model evidence, operational state and the original private repository
remain private. No production deployment, scheduler change or delivery is part
of source publication. Historical checks below are retained for context rather
than presented as completed production qualification.

---

# Historical experimental source-alpha policy

World Intel is experimental. This policy describes eligibility for owner review;
it does not assert that the current candidate has passed any gate. Publication
requires final approval of the exact source commit, proposed destination,
experimental scope, known limitations and unfinished production gates. Approval
of a design or execution plan is not publication approval.

The permitted distribution is an audited source commit and its source ZIP.
Package registry uploads, wheels/binaries, version bumps, production installs,
scheduler activation and delivery changes are outside this scope. Keep the
existing private repository and its uncleared historical refs private. Preparing
a new destination requires authorization of its name and preparation; any later
authorized push must use only the audited branch and an explicit refspec, never
a mirror of the original repository.

## Try the synthetic demo

Follow the [README setup](../README.md#development-setup) for Python 3.11 or 3.12
on Windows or Linux, including hash-locked dependencies and package installation.
Dependency installation uses the network. The installed demo itself is offline
and needs no credentials or model subscription:

```text
world-intel --help
world-intel demo --out out/demo
```

The demo uses invented stories and figures; it is not live content validation.
Commands refuse to overwrite an existing demo or initialized profile. Live use
additionally needs configured network services, the Codex executable and
authentication/model access. `doctor` checks local prerequisites but does not
verify authentication. `run --dry` still uses the network and model; live
`run --offline` is unsupported. Use private profiles and data directories outside
all repositories. Replace the full profile's synthetic SEC contact before real
SEC requests. Standalone users should select `file` or `stdout` delivery.

For alpha live use, add both existing settings to the private profile's existing
`analysis` section:

```yaml
analysis:
  source_review_enabled: true
  source_review_repair_enabled: true
```

Review remains opt-in. Disabled review preserves legacy behavior without this
source-review barrier, including possible model KB-delta application in normal
non-dry runs. Repair requires review. These settings do not certify factuality.

## Manual eligibility checklist

Record evidence privately for each item. An unchecked item remains pending;
this document creates no machine `publication_ready` value or automatic PASS.

- [ ] Independently review implementation and documentation, synthetic regression
  fixtures, and preserved failure barriers.
- [ ] Run fresh final-commit offline tests, architecture/L0 checks, installed
  commands and an offline demo outside the checkout, a tracked-content scan,
  and all four Windows/Linux CI jobs. Historical results are context only.
- [ ] Establish clean distribution byte equivalence and inspect every reachable
  object/ref intended for publication, commit metadata, source ZIP paths and
  contents. Exclude credentials, owner profiles, personal runtime launchers,
  full articles, raw model evidence and production logs.
- [ ] Predeclare minimal/full profiles, settings, evaluation rubric and budgets.
  Preserve all outcomes. Do not retry unchanged code until it passes.
- [ ] Obtain one fresh minimal live verification of that exact source with final
  content independently accepted against captured sources.
- [ ] Run one bounded isolated full-profile probe and record one of the allowed
  dispositions below, all writes and complete private evidence.
- [ ] Obtain explicit final owner approval naming the exact commit, proposed
  destination, experimental scope, known limitations and unfinished production
  gates before publication.

The allowed full-probe dispositions are:

| Disposition | Required evidence and meaning |
| --- | --- |
| Accepted output | Final output independently checked against captured sources. Automated acceptance alone is insufficient. |
| `HOLD / SAFE FAILURE OBSERVED` | Complete evidence shows no new/replaced briefing, MEDIA handoff, delivery or model KB-delta application. Identify any status, raw fetch cache and model-capture writes. Disclose the HOLD as a limitation; it is not accepted full output or full-profile qualification. |

A generated full artifact containing a positively unsupported factual claim is
a blocking finding for the candidate. Record and adjudicate disagreements about
clearly signposted ordinary analysis; do not silently call them falsehoods or
suppress them. Missing/incomplete evidence cannot establish a safe failure.
Normal HOLD may return exit code zero; use status and artifact/state evidence.

Keep live raw evidence outside every Git repository, issue attachment, public
archive and CI artifact. New alpha regression additions and the bundled demo
must be wholly fictional. Inherited tests retain short historical headline
examples in `tests/test_fetch.py`, public historical names in
`tests/test_ownership.py`, and numeric/public-filing metadata examples. See the
[fixture provenance inventory](../tests/fixtures/README.md): historical provenance
is incomplete, and no rights clearance or authenticity verification is asserted.
Full private captures/articles and exact private replays remain forbidden in
public distributions. See [security boundaries](../SECURITY.md). The project
remains MIT-licensed; that license does not license third-party reporting or
provider output. Preserve provenance and applicable terms separately.

## Known limits and production gates

Coverage checks establish registered-event coverage and source eligibility,
not factual correctness or complete major-event recall. Finance guidance asks
the model to avoid invented uses of borrowing proceeds, unsupported causality
or rankings, and lost attribution. A conditional analysis label cannot support
an invented factual premise. Prompt improvements and model-assisted review
reduce risk; synthetic tests establish bindings and orchestration, not general
semantic competence or guaranteed factuality.

Review covers full world, configured national and finance story cards' headlines,
explanations and bodies against captured cited sources. Radar, threads, signal,
scoreboard data, other finance narrative, model KB proposals and existing KB
content remain outside that review scope. With review enabled, model KB
proposals are retained as private evidence and withheld even after accepted
cards; deterministic updates and archiving can continue after acceptance.

The review budget is one initial review, at most one targeted correction and
one final all-card review within the existing analysis deadline (at most 120
seconds per call). Review alone adds at most one model call; repair permits at
most three added calls in total. Missing sources, invalid responses, unsupported
or uncertain cards, and exhausted budget can HOLD the run. Correction preserves
card membership/order and citations. A bounded preflight may remove an extra
citation only under the existing captured-source checks; it never rewrites card
prose or supplies missing evidence. There is no further correction/retry on
failure. See [review details](../README.md#source-review-and-private-evidence).

The frozen world prompt/schema L0 remains unchanged; this policy does not claim
to resolve every analytical-certainty concern. Review feed desk spelling and
the remaining configuration limitations described in the README.

Manual alpha approval may precede two scheduled days. Production qualification,
two consecutive frozen scheduled days, independently evidenced delivery and
owner content acceptance remain pending until actually performed. The strict
publication checker remains unchanged: version 1 retains seven scheduled runs
and strict replay; version 2 retains its two-day policy and coverage-regression
requirements. External declarations/provenance remain ATTESTED, not independently
verified PASS, and ATTESTED/pending gates still block that checker. This manual
checklist does not authenticate evidence, finish a trial or authorize cutover.
See [coverage and publication evidence](coverage-validation.md).
