# Coverage regression and private release evidence

`tools/check_coverage_history.py` evaluates deterministic selection using two
explicit clean code roots, full commit IDs, private full profiles and pinned
captures. It prohibits network access. Non-ranking desk, freshness, slot and
outlet settings must match the recording. The explicit profile ranking replaces
the captured ranking; the report records this policy change.

Required arguments are `--pin-path`, `--baseline-root`, `--baseline-commit`,
`--baseline-profile`, `--candidate-root`, `--candidate-commit`,
`--candidate-profile`, `--expectations`, `--strict-equivalence-report` and
`--report`. Reports must be new files outside repositories. Use `--dispositions`
only after reviewing the first comparison; preserve that initial report.

The pin lists each capsule's path, date, original mode, full source commit and
SHA-256 hashes for the manifest and both boundary inputs. Forced captures remain
historical audits and never count as scheduled acceptance. Captures are checked
in place without rewriting their mode or fetching missing text.

Expectations are private JSON with `version: 1`, `input_pin_sha256`,
`candidate_profile_sha256` and `assertions`. Each assertion has `case_id`, `kind`
(`recognized`, `protected` or `not_grouped`) and at least two exact `urls`.
Recognition and protection assertions are both required. This permits incident
specific regression evidence without adding incident specific runtime policy.

Every changed desk's ordered URL selection has a `change_sha256`. A disposition
contains `version: 1`, the exact `binding` from the comparison report and
`reviews`. Each review names `case_id`, `desk`, `change_sha256`, `decision`
(`intended`), an evidence based `reason` and `reviewed_by`. No tool creates owner
acceptance or automatically approves a replacement. Unreviewed, rejected or
obsolete dispositions leave the report on HOLD. The binding includes commits,
profile hashes, input pin hash, expectations hash and strict report hash.

A PASS means reviewed offline selection policy only. The report does not
generate a new model result, apply the final coverage gate to new analysis or
prove rendered brief quality. Archived content availability is checked using
the reviewed cleaner and both grounding floors. Missing content is UNAVAILABLE;
even when all bodies exist, historical end-to-end generation is NOT_EXECUTED.
The original strict comparison remains separately hashed historical evidence;
deliberate coverage changes are never labelled byte equivalent.

Publication manifest version 1 retains seven consecutive scheduled runs and the
strict replay gate. Version 2 uses an explicit attributed owner policy of two
consecutive scheduled days after the frozen candidate timestamp. Its policy
binds the candidate and full profile hash. Both runs must declare complete
quality, confirmed transport and owner acceptance; future dates and wrong
profiles fail. Version 2 replaces the strict replay PASS gate with validated
coverage regression evidence and preserves the old strict report. Actual
commands, input hashes and unchanged captures are checked again during preflight.

These external logs, approvals, transport declarations and offline report
provenance remain ATTESTED. They never become independently verified PASS merely
because JSON says success, and publication remains blocked while any gate is
ATTESTED or pending. The preflight never changes repository visibility.

Live minimal verification requires a full exact source commit and a separate
complete registered-event quality status, in addition to complete analysis and
grounded rendering. Its quality scope does not claim complete major-event recall.
The world prompt/schema L0 contract is unchanged. The repair schema's reviewed
maximum of three cards is a separate coverage contract change.
