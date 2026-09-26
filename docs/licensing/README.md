# Proposed personal-use / paid-commercial licensing

**Proposal only. The repository's [active LICENSE](../../LICENSE) remains in force.**
The [proposed license](PROPOSED-LICENSE.txt) is a working legal draft, not legal advice
or a conclusion that the whole repository can be relicensed.

The intended policy is free personal, noncommercial use of Jean Thompson's code,
with a separate paid written license required for commercial use, including paid
client work, business use, hosted services, and monetized content. Model licenses
and other third-party rights remain separate. Generated content does not become
owned by the software author.

Before activating the proposal:

- Confirm copyright ownership and express relicensing authority for all covered
  code. Audit outside contributions and borrowed code; preserve their notices and
  licenses. Repository ownership alone is not copyright ownership.
- Have qualified counsel review compatibility with ComfyUI's GPL and any copyleft
  dependencies or incorporated code. A third-party exclusion does not by itself
  resolve a combined-work or derivative-work licensing obligation. Restructure or
  limit scope if needed; do not simply replace the top-level license and assume
  compatibility.
- Supply the commercial contact email or sales page. Set prices, scope, term,
  renewals, and payment terms in a separate commercial agreement. The proposal
  intentionally invents neither a price nor an entitlement from donations.
- Confirm that all business use and monetized creator content should require a
  license, even before revenue is earned. The current proposal has no revenue
  threshold or free commercial evaluation exception.
- Identify the exact first covered release and code scope. Preserve the historical
  licenses and explain that already granted AGPL and other rights remain available
  for earlier versions; the new policy is not retroactive.
- Only after that review, replace the active LICENSE, update the README license
  section and any affected package/registry metadata, and publish a transition
  notice. The current `pyproject.toml` points to LICENSE. Describe the new policy as
  source available, not OSI open source, because commercial use is restricted.

Suggested README text after approval and activation:

> Original code covered by the VRGameDevGirl Personal Use and Commercial License is
> free for personal, noncommercial use. Business use, paid client work, monetized
> content, commercial products, and hosted services require a separate paid written
> license from Jean Thompson (VRGameDevGirl). Contact: [approved licensing contact].
> Models, dependencies, and third-party code retain their own licenses. This policy
> does not revoke licenses granted for earlier versions. See LICENSE for scope and
> full terms.

References for review:

- [GNU licensing FAQ](https://www.gnu.org/licenses/gpl-faq.en.html): copyright
  ownership, multiple licensing, commercial use, and plugin/combined-work issues.
- [AGPLv3](https://www.gnu.org/licenses/agpl-3.0.en.html): existing license terms.
- [Open Source Definition](https://opensource.org/osd): open-source licenses cannot
  exclude commercial fields of use.

## Review of the suggested PolyForm template

The supplied six-section template is not the official PolyForm Noncommercial
License 1.0.0. The official text includes separate patent, violation/cure, and
noncommercial-organization provisions that the supplied template omits. PolyForm's
[license-text reuse terms](https://github.com/polyformproject/polyform-licenses/blob/1.0.0/README.md)
require removing its name and website references from modified license texts.
Do not publish the supplied rewrite under the PolyForm name or copy its unrelated
copyright holder and project URL.

The [official license](https://polyformproject.org/licenses/noncommercial/1.0.0)
is an alternative if its broader permissions are desired: it permits noncommercial
purposes generally and expressly permits uses by specified organizations, including
charities, educational institutions, and government institutions regardless of their
funding source. That differs from an individuals-only personal-use grant.

If choosing PolyForm, use the official text unchanged and document the separately
available paid commercial license alongside it. Extra explanatory notices cannot
narrow rights the official license grants. If retaining the stricter personal-use
policy, keep a separately named custom license and have counsel review it. The
current proposal uses that separate name and requires both a written commercial
agreement and its required payment, rather than permission alone.
