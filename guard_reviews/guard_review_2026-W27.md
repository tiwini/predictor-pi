# Guard review 2026-W27

Generado 2026-07-05T12:15+00:00 UTC.

Regla fable (asimétrica): apretar N_sole≥20; relajar N_sole≥40 + ROI_sole>0 + sobrevive trim-2. Sólo `sole` decide relajar.

## KPHX

| guard | sole | shared | flag | reason |
|---|---|---|---|---|
| cold_bias | n=0 | n=1 w=1 pl=$+14.69 ROI=+146.9% | keep | N_sole=0 < 40 (relajar exige más N que apretar) |
| ext_diff | n=0 | n=1 w=0 pl=$-10.00 ROI=-100.0% | keep | N_sole=0 < 40 (relajar exige más N que apretar) |
| models_spread | n=0 | n=2 w=1 pl=$+4.69 ROI=+23.5% | keep | N_sole=0 < 40 (relajar exige más N que apretar) |
| overnight | n=0 | n=2 w=1 pl=$-7.42 ROI=-37.1% | keep | N_sole=0 < 40 (relajar exige más N que apretar) |
| station_dir_min | n=0 | n=2 w=2 pl=$+17.27 ROI=+86.3% | keep | N_sole=0 < 40 (relajar exige más N que apretar) |
| streak | n=0 | n=3 w=2 pl=$+7.27 ROI=+24.2% | keep | N_sole=0 < 40 (relajar exige más N que apretar) |

