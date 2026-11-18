# Evaluations

Everything used to measure Docket's answer quality lives here, outside the Python package, so it can change without touching application code.

| Folder | Contents |
| --- | --- |
| [`suites/`](suites/README.md) | Public and synthetic gold question sets, the corpora and generators behind them, benchmark scripts (`scale/`, `m0/`, `experiments/`) and the recorded results. See its README for which files are reproducible fixtures and which are generated outputs. |
| [`private-kit/`](private-kit/README.txt) | The kit for evaluating on your own documents. It contains only code and a question template: your documents, questions, labels and results stay outside Git. |

## Running

The `docket eval` commands find the suites automatically from a source checkout. From anywhere else set `DOCKET_EVAL_SUITES` to the `evals/suites` folder (an installed package does not ship them) or pass `--gold-dir` explicitly. Examples are in [`suites/README.md`](suites/README.md).

## What never goes in Git

Private gold sets, labels, run outputs and scratch libraries for real documents; downloaded public corpora (CUAD, FinQA, MuSiQue; see `suites/external/NOTICE`); model artifacts; databases and vector indexes.
