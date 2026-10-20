# Volume at RI multiples and band edges

Run 2026-10-20 02:50. Score = log(volume density within ±w of the level / density 2w-5w either side); w = 1.25 pts ES, 4.5 pts NQ. Curve columns: mean score by scale.

*MASSIVE_API_KEY not set: tests 1 (published H26 RI) and 2 (bands over 2 years) skipped.*

## Test 1b - this terminal's RI (margin / point value / 8), last ~60 days

Yahoo continuous front month with volume. Same score and placebo.

| test | sessions | level hits at 1.00 | score at 1.00 | edge vs placebo [90% CI] | percentile among placebo scales | best scale |
|---|---|---|---|---|---|---|
| ES RI 62.29 · full session profile · 16:00 close | 50 | 212 | -0.107 | -0.010 [-0.059, +0.036] | 37 | 1.20 (-0.033) |
| ES curve (full, 16:00) | 0.50:-0.116 0.60:-0.064 0.70:-0.079 0.80:-0.142 0.90:-0.142 1.00:-0.107 1.10:-0.070 1.20:-0.033 1.30:-0.079 1.40:-0.089 1.50:-0.197 | | | | | |
| ES RI 62.29 · full session profile · 17:00 last bar | 50 | 210 | -0.127 | -0.041 [-0.100, +0.014] | 6 | 1.34 (-0.052) |
| ES RI 62.29 · RTH profile · 16:00 close | 48 | 138 | -0.020 | +0.001 [-0.054, +0.055] | 64 | 1.37 (+0.064) |
| ES RI 62.29 · RTH profile · 17:00 last bar | 47 | 137 | -0.050 | -0.029 [-0.106, +0.050] | 10 | 1.36 (+0.040) |
| NQ RI 266.76 · full session profile · 16:00 close | 50 | 297 | -0.062 | -0.005 [-0.041, +0.031] | 36 | 0.81 (-0.036) |
| NQ curve (full, 16:00) | 0.50:-0.053 0.60:-0.058 0.70:-0.064 0.80:-0.040 0.90:-0.063 1.00:-0.062 1.10:-0.078 1.20:-0.051 1.30:-0.061 1.40:-0.061 1.50:-0.041 | | | | | |
| NQ RI 266.76 · full session profile · 17:00 last bar | 50 | 295 | -0.056 | -0.001 [-0.035, +0.033] | 58 | 1.24 (-0.017) |
| NQ RI 266.76 · RTH profile · 16:00 close | 49 | 181 | -0.013 | +0.023 [-0.018, +0.063] | 94 | 1.03 (+0.013) |
| NQ RI 266.76 · RTH profile · 17:00 last bar | 49 | 180 | -0.057 | -0.022 [-0.069, +0.024] | 14 | 1.28 (+0.013) |

## Test 2b - realized-vol band edges, continuous front month (Yahoo, last ~60 days)

Band forecast at each prior close from the cash index (EWMA x fat-tail quantile), drawn about the prior session's last bar. Same score and placebo as test 1.

| test | sessions | level hits at 1.00 | score at 1.00 | edge vs placebo [90% CI] | percentile among placebo scales | best scale |
|---|---|---|---|---|---|---|
| ES 90% band edge | 5 | 5 | +0.252 | +0.345 [+0.147, +0.532] | 95 | 1.43 (+0.354) |
| ES 95% band edge | 3 | 3 | -0.114 | -0.059 [-0.100, -0.017] | 25 | 1.15 (+0.354) |
| ES 99% band edge | 0 | 0 | +nan | +nan [+nan, +nan] | 0 | 0.75 (+0.354) |
| NQ 90% band edge | 6 | 6 | -0.240 | -0.136 [-0.329, +0.044] | 16 | 1.45 (+0.493) |
| NQ 95% band edge | 3 | 3 | +0.034 | +0.138 [-0.120, +0.396] | 76 | 1.41 (+0.484) |
| NQ 99% band edge | 0 | 0 | +nan | +nan [+nan, +nan] | 0 | 0.95 (+0.461) |

