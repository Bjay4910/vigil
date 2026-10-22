# Detector backtest

5 seeds x 3 series x 1440 points, 3 injected spikes per series. Generated with `python3 -m detection.backtest`.

| Rule | Threshold | Caught | False alarms | Precision | Recall |
|---|---|---|---|---|---|
| zscore | 3.0 | 45 | 85 | 35% | 100% |
| zscore | 3.5 | 45 | 17 | 73% | 100% |
| zscore | 4.0 | 45 | 4 | 92% | 100% |
| zscore | 5.0 | 45 | 2 | 96% | 100% |
| ewma | 3.0 | 45 | 81 | 36% | 100% |
| ewma | 3.5 | 45 | 13 | 78% | 100% |
| ewma | 4.0 | 45 | 3 | 94% | 100% |
| ewma | 5.0 | 45 | 2 | 96% | 100% |
