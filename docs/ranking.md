# Ranking configuration

The optional `ranking` mapping is validated when configuration loads, before
fetching. Omitting it uses the packaged topic weights and major-event policy.
Existing private profiles therefore need no migration. Source and packaged
example profiles show the same commented override, so exported starter profiles
also retain this guidance.

```yaml
ranking:
  version: 1
  topics:
    - {terms: [earthquake, tsunami], weight: 3.0}
    - {terms: [election], weight: 3.0}
  major_events:
    enabled: true
    max_events: 2
    min_publishers: 2
```

Providing `topics` replaces the packaged topic list; omit it to keep that list.
Topic terms must be non-empty strings and weights must be finite numbers from
zero to three. At most 20 topics are allowed, with one to 30 terms per topic
and at most 80 characters per term. Terms are trimmed and lowercased.
Topic weights express personal interests; they do not establish major-event
importance. The major-event lane uses its own significance and corroboration
criteria. Its configured event limit cannot exceed two, and corroboration cannot
require fewer than two independent publishers. Set `enabled: false` to disable
that lane. Invalid values and unknown ranking keys raise `ConfigError`.
