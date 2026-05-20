# Contributing to the the firm RDI Demo

Read this once before changing anything in `rdi/`, `rdi-processor/`,
`mock-rdi-api/`, `docs/`, slide source, or talk-track. It is short and
non-negotiable.

---

## Rule #1 — Redis official documentation is the single source of truth

This is a **customer-facing demo**. Anything we show, claim, or
configure here is implicitly a promise that the same thing works on the
production Redis Enterprise + RDI install the customer is evaluating
for purchase. If a feature, API endpoint, YAML key, transformation,
JMESPath function, or product claim is **not in the official Redis
docs**, it does not go into this repo.

### Authoritative URLs

| Surface | URL |
|---|---|
| RDI overview / features | <https://redis.io/docs/latest/integrate/redis-data-integration/> |
| RDI architecture | <https://redis.io/docs/latest/integrate/redis-data-integration/architecture/> |
| `config.yaml` schema | <https://redis.io/docs/latest/integrate/redis-data-integration/reference/config-yaml-reference/> |
| Job files / transforms | <https://redis.io/docs/latest/integrate/redis-data-integration/data-pipelines/transform-examples/> |
| Data transformation blocks | <https://redis.io/docs/latest/integrate/redis-data-integration/reference/data-transformation/> |
| JMESPath custom functions | <https://redis.io/docs/latest/integrate/redis-data-integration/reference/jmespath-custom-functions/> |
| Control-plane API | <https://redis.io/docs/latest/integrate/redis-data-integration/reference/api-reference/> |
| CLI (`redis-di`) | <https://redis.io/docs/latest/integrate/redis-data-integration/reference/cli/> |
| Redis Enterprise | <https://redis.io/docs/latest/operate/rs/> |
| RedisJSON | <https://redis.io/docs/latest/develop/data-types/json/> |
| RediSearch | <https://redis.io/docs/latest/develop/interact/search-and-query/> |
| Redis Insight | <https://redis.io/docs/latest/develop/tools/insight/> |

If you cannot find your feature on one of these pages, **stop**. Ask
the demo owner; do not invent.

---

## Rule #2 — Reference implementations may do less, never more

`rdi-processor/` and `mock-rdi-api/` are *reference implementations*
that exist because real RDI ships only as a VM installer or Helm chart
(not as a laptop-friendly Docker image). They are allowed to:

- ✅ Implement a strict subset of real RDI's features
- ✅ Be transparent about what they don't implement
  (see [`docs/04-redis-enterprise-verification.md`](docs/04-redis-enterprise-verification.md))

They are **not** allowed to:

- ❌ Expose YAML keys, API endpoints, transformation blocks, or
  functions that don't exist in the official Redis product
- ❌ Behave differently from the documented contract
  (e.g. return a different JSON shape than the real `/api/v1/...` route)
- ❌ Silently swallow features the YAML asks for —
  if `rdi/jobs/*.yaml` uses a construct, the reference processor must
  implement it correctly, or that construct must be removed

---

## Rule #3 — Every change updates the assurance pack

There are two evidence documents that we hand to the customer's
architecture / IT security team:

- [`docs/04-redis-enterprise-verification.md`](docs/04-redis-enterprise-verification.md) — proves the components are real Redis products
- [`docs/05-rdi-spec-conformance.md`](docs/05-rdi-spec-conformance.md) — proves every YAML key / API / claim is on the official RDI spec

If your change touches any of: `rdi/`, `rdi-processor/`,
`mock-rdi-api/`, the slide deck source, talk-track, or runbook, you
**must** update both documents so they stay in sync with reality.

---

## Checklist for any PR / change

- [ ] Looked up the change against the relevant URL in the Rule #1 table
- [ ] If adding to `rdi/config.yaml`: every key is on the current
      [config-yaml-reference](https://redis.io/docs/latest/integrate/redis-data-integration/reference/config-yaml-reference/);
      no deprecated keys reintroduced
- [ ] If adding to `rdi/jobs/*.yaml`: every `uses:`, `data_type`,
      `language`, and JMESPath function is on the official lists
- [ ] If editing `mock-rdi-api/app.py`: every route maps to a real
      `/api/v1/...` operation in the
      [API reference](https://redis.io/docs/latest/integrate/redis-data-integration/reference/api-reference/)
- [ ] If editing slides / talk-track / runbook: every product claim
      cites an official doc page
- [ ] Updated `docs/05-rdi-spec-conformance.md` to reflect the change
- [ ] Re-ran `./scripts/verify-redis-enterprise.sh` — all 25 checks pass
- [ ] If the reference processor needed new support, implemented it
      and verified the new YAML construct flows end-to-end

---

## How this rule is enforced

1. **Cursor rules** at `.cursor/rules/` — any AI agent (Cursor, Copilot
   plugins that read `.cursor/rules/`, etc.) loads these
   automatically. The always-on rule is
   `redis-official-source-of-truth.mdc`; file-scoped rules fire when
   you open `rdi/**/*.yaml` or `mock-rdi-api/**/*.py`.
2. **This `CONTRIBUTING.md`** — human reviewers / future contributors
   read this before merging.
3. **`docs/05-rdi-spec-conformance.md`** — concrete audit; the
   "what's allowed" reference table.
4. **`./scripts/verify-redis-enterprise.sh`** — quick automated check
   that the running stack still matches the assurance claims.

If a future change requires deviating from any of these rules, the
deviation must be called out explicitly in the PR description **and**
in the spec-conformance doc, so the customer-facing story stays
honest.
