# Heat mat controller tuning

`heat.py` runs the mat as time-proportional PI control: every `WINDOW_S`
(15 min) it picks a duty from 0 to 1 and keeps the mat on for that share of the
window, in one pulse. The soil responds to the average power rather than to
full-on/full-off switching.

## The rig model

Fitted to readings from the grow stand while the first PI tuning was running
(soil and air temperature every 5 min, the controller's own duty reconstructed
by replaying its decisions; the replay matched the live duty):

- dead time about 12 min, then two lags of about 15 min each
- steady state: soil ≈ 16.5 + 0.38 × air + 3.69 × duty (°C)
- run in closed loop with the first tuning (Kp 0.6 per °C, Ti 1 h) the model
  reproduces the 2.75 °F swing seen on the real soil, which is why it was used
  for retuning; an earlier model fitted only to on/off data predicted 0.9 °F and
  had the lag wrong

## Current tuning

| Constant | Value |
| --- | --- |
| `KP_PER_C` | 0.25 duty per °C below target |
| `TI_S` | 7200 s |
| feed-forward | `(target − 16.5 − 0.38 × air) / 3.69`, the duty that holds the target at this air temperature |
| `WINDOW_S` / `MIN_PULSE_S` | 900 s / 60 s |

Simulated over a recorded day: about 0.4 °F peak to peak, mean on target; under
1 °F with the rig's gain off by 30 %, its lags 40 to 60 % off, or its delay
50 % off. Roughly 165 plug switches a day.

## Retuning

The power level is logged each window as `heat:duty`. With a day of `heat:duty`,
`temp:soil` and `temp:air`, refit the model above and retune in simulation; the
suite's "fitted rig" check (tests/test_suite.py, Heat mat) is the place to
encode a new model so a retune is tested against it.
