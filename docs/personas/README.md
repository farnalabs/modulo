# Personas

Six user personas covering the Modulo ICP spectrum — solo developer to regulated enterprise — plus the community contributor who drives library growth.

## Summary

| Persona | Org size | Decision role | Adoption pattern | Edition |
|---|---|---|---|---|
| [Duncan](duncan-solo-developer.md) | 1 | Self (builder) | Grow complexity over time | Community → Enterprise (if he teams up) |
| [Alice](alice-devx-sme.md) | 30–150 | Recommends (budget: VP Eng) | Model existing SDLC → replace steps one at a time | Community |
| [Priya](priya-platform-engineer.md) | 150–1,000+ | Evaluates (budget: CTO) | Centralised platform rollout team-by-team | Enterprise |
| [Marcus](marcus-ciso.md) | 500–5,000+ | Signatory | Security governance gate; approves spend | Enterprise |
| [Elena](elena-engineering-director.md) | 50–300 | Decision-maker | Dashboard-driven oversight and ROI | Enterprise (for team governance) |
| [Jordan](jordan-community-contributor.md) | 1 (solo OSS) | None | Build, share, and fork library primitives | Community |

## How to use

Persona-indexed Gherkin feature files live in `backend/tests/features/personas/`. Each `.feature` file asserts that the product needs for that persona are achievable. Run:

```bash
pytest -k "persona-duncan"   # everything Duncan needs
pytest -k "persona-alice"    # everything Alice needs
```

Each persona references existing low-level scenarios via `@persona-*` tags, plus higher-level "happy path" scenarios that cross multiple features.
