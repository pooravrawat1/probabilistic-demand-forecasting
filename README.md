# Probabilistic Demand Forecasting

Research project to predict the probability that an electricity client will have an unusually high demand peak the next day, using readings available at the end of the current day.

**Status:** Source audit complete; modeling and results are TBD.

## Reproduce the source audit

Requires Python 3.11 or newer. The audit uses only the standard library.

```sh
python3 -m venv .venv
PYTHONPATH=src .venv/bin/python -m demand_forecasting download
PYTHONPATH=src .venv/bin/python -m demand_forecasting audit
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

The download goes to ignored `data/raw/`; audit outputs go to ignored `data/audit/`. To use an existing unchanged archive or text file, run `PYTHONPATH=src .venv/bin/python -m demand_forecasting audit --source /path/to/file`.

## Documents

- [Requirements](docs/requirements.md), [design](docs/design.md), and [tasks](docs/tasks.md)
- [Data audit](docs/data_audit.md) and [data dictionary](docs/data_dictionary.md)

Source: [UCI Electricity Load Diagrams 2011–2014](https://archive.ics.uci.edu/dataset/321/electricityloaddiagrams20112014.) by Artur Trindade, [DOI: 10.24432/C58C86](https://doi.org/10.24432/C58C86), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The source file is not committed.
