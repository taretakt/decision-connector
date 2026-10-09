---
name: Feature request
about: A new capability for the substrate or a new domain adapter
title: "[feat] "
labels: [enhancement]
assignees: []
---

**What should it do**

One paragraph. Name the layer: substrate (`decision_connector.py`), a domain adapter, or the
pipeline CLI (`funnel.py`).

**Why it matters**

What operationally changes if this ships — determinism, cost, auditability, a new domain.

**Constraints to respect**

- No runtime dependencies (offline stand-ins stay dependency-free)
- Byte-determinism: same input → identical output
- Contract: if this touches adapters, it must keep the 7-member shape

**Sketch**

Pseudocode or a doc link is enough — no implementation required.