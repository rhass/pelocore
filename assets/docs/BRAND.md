# Pelocore Brand Guide

## Brand idea
**Pelocore** is a personal workout-data bridge: it carries activity data from Peloton through FIT and into COROS, then keeps the two sides reconciled over time.

The visual identity is intentionally independent of both vendors' trademarks. The mark uses two opposing motion paths around a central node to communicate **flow, translation, and synchronization**.

## Tagline
**Bridge your workouts. Own your data.**

Optional short descriptor:
**Peloton → FIT → COROS activity bridge**

## Personality
Practical, energetic, technical without being nerdy, trustworthy, independent, and a little irreverent about closed fitness ecosystems.

## Color palette
| Token | Hex | Use |
|---|---|---|
| Deep Navy | `#0B132B` | primary text, dark backgrounds, app icon |
| Coral Orange | `#FF6B3D` | forward motion / source-side accent |
| Mint Teal | `#2DD4BF` | reverse motion / destination-side accent |
| Off White | `#FAFAF7` | light background |
| Slate Gray | `#94A3B8` | secondary UI text |

CSS variables are included in `palette.css`.

## Typography
Recommended:
- Headings / wordmark-adjacent UI: **Inter**, 700–800
- Body: **Inter**, 400–500
- CLI / technical labels: **JetBrains Mono** or system monospace

The supplied SVG wordmark uses a system-safe font stack so it remains usable without shipping proprietary font files.

## Mark rules
- Keep clear space around the mark equal to at least the central-node diameter.
- Use the full-color mark on light backgrounds.
- Use the reverse mark on dark backgrounds.
- Use the monochrome mark where color reproduction is unavailable.
- Do not recolor the orange and teal paths independently in ways that change their directional relationship.
- Do not add shadows, outlines, gradients, or vendor logos to the mark.

## Minimum sizing
- Digital icon: 24 px minimum; 32 px preferred for favicons.
- Full horizontal logo: 140 px wide minimum in web UI.
- Print: 20 mm wide minimum for the horizontal logo.

## Assets
- `logo/logo-horizontal.svg` — primary logo
- `logo/logo-horizontal-reverse.svg` — dark-background logo
- `logo/logo-horizontal-mono.svg` — monochrome logo
- `logo/mark.svg` — icon/brand mark
- `logo/mark-reverse.svg` — reverse mark
- `logo/mark-mono.svg` — monochrome mark
- `icons/favicon.svg` — favicon source
- `icons/app-icon.svg` — app / home-screen icon
- `social/github-social.svg` — GitHub social preview
- `docs/concept-board.png` — visual concept reference

## Naming / descriptor
Use **pelocore** in lowercase in UI and documentation. Treat “Pelocore” as the sentence-start / title-case presentation when grammar calls for it.

## Trademark note
Pelocore is an independent open-source project. The identity intentionally avoids reproducing Peloton or COROS marks. Product names may be referenced in explanatory text, but should not be incorporated into Pelocore artwork.
