DOCKET PRIVATE EVAL KIT
=======================
Test Docket on YOUR OWN documents, entirely on your machine. Nothing is uploaded.
You only send back one small JSON file of aggregate numbers (percentages and a
median time). It contains no document text, no questions, no answers, no file names.

What it does
  1. Copies/indexes your folder into a SCRATCH directory (your files are only read,
     never changed) and builds a small entity graph (company/party names).
  2. Asks your questions with the graph-assisted search ON and OFF.
  3. Grades answers with the local qwen3:14b model and prints only totals.
Your real Docket data (~/.local/share/docket) is never touched; the scripts refuse it.

Requirements (Ubuntu)
  - Ollama running, with models:  ollama pull qwen3:14b   and   ollama pull qwen3-embedding:0.6b
  - Python 3.11+ and git.
  - A GPU with ~12 GB+ free VRAM is recommended; CPU-only works but is slow.

1. One-time setup
  git clone <DOCKET_REPO_URL> ~/docket-kit
  cd ~/docket-kit
  git checkout private-eval-kit
  cd backend
  python3 -m venv ~/docket-kit-venv
  ~/docket-kit-venv/bin/pip install -e .
  ollama pull qwen3:14b
  ollama pull qwen3-embedding:0.6b

2. Write your questions (10-20)
  cp ~/docket-kit/evals/private-kit/questions_template.yaml ~/my_questions.yaml
  nano ~/my_questions.yaml
  Per question: question, expected_answer (short, key facts), expected_file (file name or
  a distinctive part of it), answerable (yes/no). Include 2-4 "no" questions whose answer is
  not in your documents. Name the company/party in the question where natural.
  Keep this file private.

3. Index your documents (use a NEW scratch folder; can take a while for big folders)
  cd ~/docket-kit/backend
  ~/docket-kit-venv/bin/python evals/private-kit/ingest_folder.py /path/to/your/documents ~/docket-private-eval/data

4. Run the evaluation (prints aggregate numbers only)
  ~/docket-kit-venv/bin/python evals/private-kit/run_private_eval.py ~/docket-private-eval/data ~/my_questions.yaml --summary-json ~/docket-summary.json

5. Send back ~/docket-summary.json (and, if you like, the table printed on screen).
  Check it first: cat ~/docket-summary.json  (numbers only).

What the numbers mean
  accuracy %               questions answered correctly (for "no" questions: correctly abstained)
  wrongful abstention %    answerable questions where Docket wrongly said it could not answer
  expected-file recall %   how often the expected file was among the evidence retrieved
  entity-match %           share of questions whose named company/party was found in the graph
  median latency s         median seconds per answer

Cleanup
  rm -rf ~/docket-private-eval      (deletes the scratch index; your documents are untouched)

Notes
  - Do not run other heavy GPU jobs at the same time.
  - Step 3 uses one PDF parser (about 3 GB RAM). Add --parse-workers 2 to ingest faster if you have 16 GB+ RAM.
  - Re-running step 3 needs a fresh scratch folder (or delete the old one).
  - Optionally: OLLAMA_KEEP_ALIVE=30m in the Ollama environment speeds up runs.
