## What this changes

<!-- One paragraph. What changed and why. -->

## Type

- [ ] Bug fix
- [ ] New capability
- [ ] Research tooling
- [ ] Documentation
- [ ] Refactor / internal

## Verification

<!-- Paste the actual output, not a claim that it passed. -->

- [ ] `cd api && ../.venv/bin/python -m pytest tests -q`
- [ ] `cd web && npm test`
- [ ] `cd web && npm run typecheck`

```
<!-- test output -->
```

## Safety

- [ ] This change adds **no** order-execution path: no request signing, no API-key
      header, no authenticated exchange endpoint, no order submission.
- [ ] `tests/test_paper_only.py` and `tests/test_safety.py` still pass.
- [ ] No secret, credential, private hostname, IP address or personal
      information is introduced anywhere, including in comments, fixtures and
      test data.

## If this touches measurement

- [ ] Features stay causal; no look-ahead introduced.
- [ ] Any performance claim is out-of-sample and walk-forward validated.
- [ ] Losing/negative results are reported, not filtered out.
- [ ] No threshold was moved in order to improve a result.
- [ ] Unmeasured values are not rendered or stored as zero.

## Notes for reviewers

<!-- Anything non-obvious: trade-offs, things you deliberately left out. -->
