# Theme tokens and contrast verification

Calculated from the implemented sRGB tokens using WCAG relative luminance. These are mathematical token checks, not a claim of native accessibility conformance.

| Token | Obsidian & Silver | Porcelain & Ink |
| --- | --- | --- |
| canvas | `#14181E` | `#FAFBFC` |
| nav | `#0E1218` | `#F0F3F5` |
| surface | `#1B2129` | `#FFFFFF` |
| surface_secondary | `#202833` | `#EDF1F4` |
| selection | `#293B4D` | `#DCE9EB` |
| hover | `#252F3B` | `#E7EDF1` |
| pressed | `#304257` | `#D6E2E7` |
| text | `#E8EDF4` | `#18232E` |
| secondary | `#A8B4C3` | `#516272` |
| disabled | `#8998AA` | `#586A78` |
| border | `#303A47` | `#D7DFE6` |
| control | `#7D8FA5` | `#718493` |
| accent | `#ADC6E1` | `#245F64` |
| on_accent | `#142232` | `#FFFFFF` |
| accent_hover | `#C3D6EA` | `#1C5359` |
| accent_pressed | `#91B1D3` | `#18464C` |
| focus | `#A7C9F0` | `#225F8E` |
| success | `#A4CBB9` | `#2F6653` |
| warning | `#E0C18D` | `#79551C` |
| danger | `#F0AAAA` | `#9D3E45` |
| info | `#ADC6E1` | `#365E85` |
| evidence | `#263C50` | `#DEECEE` |

| Foreground / background | Dark | Light | Target |
| --- | --- | --- | --- |
| text / canvas | 15.14:1 | 15.37:1 | 4.5:1 |
| secondary / canvas | 8.47:1 | 6.07:1 | 4.5:1 |
| secondary / surface | 7.70:1 | 6.29:1 | 4.5:1 |
| disabled / nav | 6.38:1 | 5.03:1 | 4.5:1 |
| text / selection | 9.77:1 | 12.81:1 | 4.5:1 |
| text / evidence | 9.68:1 | 13.15:1 | 4.5:1 |
| on_accent / accent | 9.15:1 | 7.25:1 | 4.5:1 |
| on_accent / accent_hover | 10.83:1 | 8.64:1 | 4.5:1 |
| on_accent / accent_pressed | 7.23:1 | 10.40:1 | 4.5:1 |
| focus / surface | 9.46:1 | 6.78:1 | 3:1 |
| control / surface | 4.89:1 | 3.87:1 | 3:1 |
| accent / surface_secondary | 8.45:1 | 6.38:1 | 3:1 |

The Rust test also checks reading text on hover/pressed surfaces and every success/warning/error/info text token against canvas, sidebar, primary and secondary surfaces. Essential input and radio boundaries use `control`; quiet action labels do not need an outline. Progress fills use `accent`, with a text ratio as an equivalent description. Nonessential dividers intentionally use a subtle border. Planned destinations are noninteractive labels painted with the readable disabled token. Native disabled primary actions are dimmed and are not represented as meeting ordinary active-control contrast. Focus, display scaling, antialiasing, screen-reader behavior and IME still require target-platform evaluation.
