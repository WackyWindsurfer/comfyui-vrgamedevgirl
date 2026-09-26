# Proposed community-use and commercial-software licensing

**Proposal only. The repository's [active LICENSE](../../LICENSE) remains in force.**
The [proposed license](PROPOSED-LICENSE.txt) is a working draft for legal review,
not legal advice or a conclusion that the whole repository can be relicensed.

The intended distinction is **free use to create and monetize content; paid
permission to commercialize the covered software itself**. A blanket prohibition
on commercial use would prohibit the monetized videos the owner wants to allow.
The proposal is named **VRGameDevGirl Community and Commercial License**.

The current draft extends the content-creation permission to individuals and
studios/businesses producing media. Confirm that scope before activation; it is
intended to avoid requiring a filmmaker to pay just because they form a company.
The proposal also permits genuinely optional community donations and educational
media, while treating paid software support/customization as commercial software
services. These boundary choices should be reviewed along with the core policy.

## Examples under the proposed terms

| Activity | Proposed treatment |
| --- | --- |
| Make personal videos or learn the code | Free |
| Monetize YouTube videos, films, or music videos | Free under this code license; model/input rights still apply |
| Sell completed videos or produce a paid creative commission | Free under this code license |
| A studio uses an internal installation to make ads or client videos | Free under the proposed content exception |
| Employees use a private production UI | Free when limited to permitted internal content production |
| A client reviews a video and asks the creator for changes | Free creative production use |
| Customers submit generation jobs through a UI, API, bot, or queue in a commercial offering | Paid software license required |
| Sell automated rendering/generation capacity while an operator submits jobs | Paid software license required |
| Fork the project, improve it, and share the fork free under the same terms | Free |
| Publish a free community fork with genuinely optional donations and no donor perks | Free |
| Charge for fork downloads, premium features, early access, or software subscriptions | Paid software license required |
| Bundle covered code into a commercial app, plugin, appliance, or service | Paid software license required |
| Offer an ad-supported generation platform using covered code | Paid software license required, even if end users pay nothing |
| Sell installation, customization, or support of the software to others | Paid software license required under this proposal |
| Publish a monetized tutorial about using the Builder | Free; no paid software access may be bundled |
| A company buys a commercial license | Only rights specified in its contract; no automatic copyright ownership |
| A company wants to purchase the copyright or exclusive ownership | Separately negotiated, signed rights-transfer agreement |
| Use a prior AGPL release under that release's terms | Earlier rights continue; this proposal cannot impose a new fee |

Examples summarize the proposal; the reviewed, activated license would control.
Code that is independently implemented or otherwise usable without copyright
permission is not captured merely because it has similar ideas or features.

## Why neither supplied template should be used as written

**MIT template:** the suggested text combines noncommercial restrictions with an
unrestricted MIT grant, including permission to sell the software. These provisions
conflict with the intended paid-license requirement. Adding a second identical MIT
grant does not solve that conflict. Do not call a commercial-restricted custom
license MIT or include an alternative unrestricted MIT grant for covered code.
See the [official MIT license](https://opensource.org/license/mit).

**PolyForm template:** the supplied six-section rewrite is not the official
PolyForm Noncommercial 1.0.0 text. The official license has patent, violation/cure,
and organizational-use provisions the rewrite omits. Its
[reuse terms](https://github.com/polyformproject/polyform-licenses/blob/1.0.0/README.md)
require removing PolyForm branding from modified license texts. Plain
[PolyForm Noncommercial](https://polyformproject.org/licenses/noncommercial/1.0.0)
does not provide the general express commercial-content-production permission
wanted here. A carefully drafted supplemental permission alongside an unchanged
standard license is a possible alternative for counsel to evaluate, but neither
of the supplied templates establishes the desired boundary reliably as written.

The custom proposal makes that boundary explicit and does not claim to be MIT,
PolyForm, or OSI open source. Do not copy another project's author, product name,
email address, or URL into this project's terms.

## Before activation

1. Confirm copyright ownership and express relicensing authority for all covered
   code. Audit contributions and borrowed code, preserving notices. Repository
   ownership alone is not copyright ownership. Obtain contributor permissions or
   exclude/rework code where necessary; do not claim existing contributions were
   assigned automatically.
2. Obtain legal review of ComfyUI GPL compatibility and any incorporated copyleft
   code/dependencies. An exclusion sentence alone cannot resolve combined-work or
   derivative-work obligations. A restrictive license may be unavailable for code
   that must be distributed under GPL; assess actual integration and provenance.
3. Supply the commercial email or sales page and confirm the boundary cases above.
   Set commercial pricing, duration, deployments, support, and payment terms in a
   separate agreement. Choose fixed fees or royalties there; this proposal neither
   invents prices nor promises automatic collection of money from infringers.
4. Identify the exact first covered release and files. Preserve historical license
   notices and explain that earlier AGPL or other permissions remain effective.
5. After review, replace the active LICENSE, update the README license section,
   relevant file notices and package/registry metadata, and publish a transition
   notice. `pyproject.toml` currently points to LICENSE. Check registry acceptance
   of a custom license. Describe the policy as source available, not OSI open source.
6. Establish contributor terms for future contributions if the owner needs rights
   to offer paid licenses covering them. A contribution is not an automatic
   copyright assignment. Have counsel prepare an appropriate contributor agreement.

## Suggested README text after activation

> You may use the Builder to create, sell, and monetize videos and other creative
> media without paying us a software license fee, including paid client production,
> subject to model licenses and other third-party rights. Free community forks and
> modifications are permitted under the license. Selling the covered software,
> commercial forks or integrations, paid access, or commercial hosted generation
> services requires a separate paid written license from Jean Thompson
> (VRGameDevGirl). Contact: [approved licensing contact]. A license does not transfer
> copyright ownership. Models and third-party code retain their own terms, and
> previously granted licenses remain effective. See LICENSE for complete terms.

## Sources for legal review

- [GNU licensing FAQ](https://www.gnu.org/licenses/gpl-faq.en.html): copyright
  ownership, multiple licensing, commercial use, and plugin/combined-work issues.
- [AGPLv3](https://www.gnu.org/licenses/agpl-3.0.en.html): existing license terms.
- [Open Source Definition](https://opensource.org/osd): open-source licenses cannot
  exclude commercial fields of use.

A licensing condition is not a payment-processing system or technical lock. It
sets permitted uses and a basis for enforcement to the extent valid under law;
it cannot guarantee discovery of every misuse or payment by every violator.
