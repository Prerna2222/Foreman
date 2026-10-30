# Foreman

**A crew of AI coding agents that fix 10 planted bugs in a copy of TinyDB, coordinating through one FalkorDB graph.**

Entry for the WeMakeDevs x FalkorDB **Graph Hacks** hackathon, **Track 02: Agent Memory and Coordination**.

- The graph is not a side store. It is how the crew decides who may work on what: it grants claims, blocks bugs that share code, takes a dead worker's bug back, passes the handoff note on, and remembers lessons for later.
- Open `ui/index.html` through the server (see "Run it") to watch a recorded run play, ask the graph why it made a decision, and see the numbers.

## 1. The crew

| Name | Role | What it does |
|---|---|---|
| Rex | Foreman | Plain code, no model. Creates the 10 tickets, asks the graph which ones share code, sets the order, reopens expired claims |
| Mina, Jo, Kai | Workers | Code picks and claims a bug; the model recalls lessons, edits code in its own git worktree, runs tests, then submits (or leaves a handoff note) |
| Vera | Reviewer | The model decides, but the tools refuse an approval if the ticket's tests fail, other tests broke, files outside the footprint changed, or test files were edited |
| Lore | Historian | Turns each approved fix into a lesson in the graph |

Each role has its own small tool list. A model can only call tools for its own role, and the Python code (not the model) enforces the rules.

## 2. Models (AI inside the project)

| Use | Model (NVIDIA hosted endpoint) |
|---|---|
| Workers, Reviewer and Historian, first choice | `nvidia/nemotron-3-super-120b-a12b` |
| Backup if the main model fails after retries | `openai/gpt-oss-20b` |

Both pass the tool-call check in `foreman-code/llm_check.py`. Every call, retry, fallback and token is counted in the run report. Full AI disclosure: `foreman-code/AI-USAGE.md`.

## 3. How the graph is used (the data model)

One graph per repo, named `foreman_<repo>` (for example `foreman_tinydb`). Nothing in one graph can be seen from another. Three layers:

| Layer | Nodes | Written by |
|---|---|---|
| Code | `File`, `Function`, `Test`; links `DEFINED_IN` (function to file), `IMPORTS`, `CALLS`, `COVERS` (which test runs which function) | `foreman-schema/load_code.py`, once |
| Work | `Agent`, `Task`, `Session`, `Message`, `Handoff`, `Result`, `Decision`; links `FAILS_ON`, `TOUCHES` (weighted), `CONFLICTS_WITH`, `ABOUT`, `MADE_BY`, `RECALLED` | the agents, through `graph_api.py` |
| Memory | `Memory` (kind `episodic`, `semantic` or `procedural`; status active or retired; uses and successes); links `ABOUT` (a function), `LEARNED_FROM` (a ticket) | the Historian |

Full schema with every property: `foreman-schema/graph-schema-v1.md`.

How the five Track 02 focus areas map to the graph:

| Focus area | Where it lives |
|---|---|
| Episodic, semantic, procedural memory | `Memory` nodes tagged by kind, linked to the code they are about; success counts rank them; outdated ones are retired, not deleted |
| Persistent conversation history | `Session` and `Message` nodes, numbered inside a session; a restarted worker reads its own history |
| Shared state and handoffs | Ticket status lives only in the graph; `Handoff` notes are stored on the ticket and the next worker reads them |
| High-concurrency reads and writes | Every claim, message and memory update is one Cypher statement |
| Separate memory per tenant | One graph per repo |

## 4. The Cypher the product depends on

These are in `foreman-schema/schema.py` (and used by `graph_api.py`). Each is one statement, so each is atomic.

**Safe claim.** One statement checks the ticket is open, that no conflicting ticket is claimed, takes it, and writes the decision. Two workers cannot both get it.

```cypher
MATCH (a:Agent {id:$who})
MATCH (t:Task {run_id:$run, id:$id})
WHERE t.status = 'open'
OPTIONAL MATCH (t)-[:CONFLICTS_WITH]-(o:Task {run_id:$run, status:'claimed'})
WITH a, t, o
WHERE o IS NULL
SET t.status = 'claimed', t.claimed_by = a.id, t.claim_expires_at = $expires
WITH a, t
MERGE (c:Counter {name:'decision'}) ON CREATE SET c.v = 1 ON MATCH SET c.v = c.v + 1
CREATE (d:Decision {id: 'dec-' + toString(c.v), kind:'claim', run_id:$run, ts:$now, summary: a.id + ' claimed ' + t.id})
CREATE (d)-[:ABOUT]->(t), (d)-[:MADE_BY]->(a)
RETURN d.id
```

**Footprint.** For each ticket, every function its failing tests run gets a weight: how suspicious it is (it is run by this ticket's failing tests and few passing ones) times how specific it is (a function every test runs says little).

```cypher
MATCH (t:Task {run_id:$run, id:$id})-[:FAILS_ON]->(ft:Test)
WITH t, count(ft) AS tf
MATCH (t)-[:FAILS_ON]->(x:Test)-[:COVERS]->(f:Function)
WITH t, tf, f, count(DISTINCT x) AS ef
OPTIONAL MATCH (p:Test {status:'pass'})-[:COVERS]->(f)
WITH t, tf, f, ef, count(DISTINCT p) AS ep
MERGE (t)-[r:TOUCHES]->(f)
SET r.weight = (ef / sqrt(tf * (ef + ep + 0.0))) * log(toFloat($n_tests) / f.test_count)
```

**Conflicts.** Two tickets whose footprints overlap by 0.10 or more get a `CONFLICTS_WITH` link, which the claim query above checks.

```cypher
MATCH (a:Task {run_id:$run, id:$id})-[ra:TOUCHES]->(f:Function)<-[rb:TOUCHES]-(b:Task {run_id:$run})
WHERE b.id <> a.id
WITH a, b, f, CASE WHEN ra.weight < rb.weight THEN ra.weight ELSE rb.weight END AS m
WITH a, b, sum(m) AS inter, collect([f.id, m]) AS shared
RETURN b.id, b.status,
       CASE WHEN a.footprint_weight + b.footprint_weight - inter > 0
            THEN inter / (a.footprint_weight + b.footprint_weight - inter) ELSE 0.0 END AS score, shared
ORDER BY score DESC
```

**Claim expiry.** A claim that stops getting heartbeats goes back to open, and a decision records why. The handoff note stays on the ticket for the next worker.

```cypher
MATCH (t:Task {run_id:$run, status:'claimed'})
WHERE t.claim_expires_at < $now
WITH t, t.claimed_by AS prev
SET t.status = 'open', t.claimed_by = null, t.claim_expires_at = null
-- then a Decision node 'claim by <prev> on <ticket> expired, ticket reopened' is created
RETURN t.id
```

**Memory recall.** Lessons about the same code the ticket touches, ranked by how targeted (share of the lesson's functions the ticket touches) times how reliable (successes and uses). Recalled lessons get a `RECALLED` link, so you can see which lessons a ticket used.

```cypher
MATCH (t:Task {run_id:$run, id:$id})-[r:TOUCHES]->(f:Function)<-[:ABOUT]-(m:Memory {status:'active'})
WITH m, count(DISTINCT f) AS shared, sum(r.weight) AS weight
MATCH (m)-[:ABOUT]->(a:Function)
WITH m, shared, weight, count(a) AS total
RETURN m.id, m.kind, m.summary, toFloat(shared) / total AS relevance, (m.successes + 1.0) / (m.uses + 2.0) AS score
ORDER BY relevance * score DESC, weight DESC, m.id
LIMIT $k
```

**Explain.** "Why did it do that?" follows a `Decision` to the agent, the ticket, the code it touches, and the tickets it conflicts with.

```cypher
MATCH (d:Decision {id:$dec})
OPTIONAL MATCH (d)-[:MADE_BY]->(a:Agent)
OPTIONAL MATCH (d)-[:ABOUT]->(t:Task)
OPTIONAL MATCH (t)-[:TOUCHES]->(f:Function)
OPTIONAL MATCH (t)-[c:CONFLICTS_WITH]-(o:Task)
RETURN d.kind, d.summary, a.id, t.id, t.title,
       collect(DISTINCT f.id) AS code, collect(DISTINCT [o.id, o.status, c.score]) AS conflicts
```

## 5. What the screen shows

Five pages, driven by the same graph reads whether the run is live or a saved replay:

- **Stage**: the six agents around a table in an office. Each role moves and holds a prop that fits its job; tickets and lessons fly between them as they are claimed, reviewed and saved.
- **Graph**: tickets, functions and lessons. The camera zooms to the nodes that matter at each moment; clashing tickets are linked by dashed arcs.
- **Board**: a ticket board (Waiting, Working, In review, Done). A lock means the graph is holding a ticket back because it shares code with one in progress.
- **Ask**: pick a question ("why did Kai wait for T-06?") and the graph answers from the decisions it recorded, with the evidence path.
- **Numbers**: this run's counts, the three-setup comparison and the claim load test.

## 6. Evaluation: three setups, same ten bugs

Same model, same tickets, same tools, same crash (worker `w1` is stopped after 6 steps). Only the coordination changes. Each setup was run **twice**. After each run, all approved fixes are merged into one fresh copy of the buggy code and the whole test suite is run, so the score is what really works. Details: `foreman-code/EVAL.md`.

Averages over the 2 runs of each setup:

| Setup | Runs | Bugs fixed in merged code (of 10) | Double claims | Clashing bugs worked together | Duplicate work | Wasted worker sessions | Sent back by reviewer | Crashed bug recovered | Runs stalled | Time (min) | Model calls | Tokens (million) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| No coordination | 2 | 9 | 0 | 1 | 0 | 1.5 | 0 | 0 of 2 | 2 of 2 | 32.7 | 240.5 | 1.8 |
| Graph only | 2 | 9 | 0 | 0 | 1.5 | 8 | 0 | 2 of 2 | 0 of 2 | 35.4 | 266.5 | 1.5 |
| Graph + memory | 2 | 9.5 | 0 | 0 | 1 | 4.5 | 1 | 2 of 2 | 0 of 2 | 42.3 | 320.5 | 2.0 |

Every run, one by one:

| Run | Fixed in merged code | Approved | Regressions | Double claims | Duplicate work | Stalled | Time (min) |
|---|---|---|---|---|---|---|---|
| none-1 | 9 | 9 | 0 | 0 | 0 | yes | 20.7 |
| none-2 | 9 | 9 | 0 | 0 | 0 | yes | 44.7 |
| graph-1 | 10 | 10 | 0 | 0 | 0 | no | 20.6 |
| graph-2 | 8 | 8 | 0 | 0 | 3 | no | 50.2 |
| full-1 | 10 | 10 | 0 | 0 | 2 | no | 34.4 |
| full-2 | 9 | 9 | 0 | 0 | 0 | no | 50.2 |

How to read it, honestly:

- **What the graph clearly did: crash recovery and no stalls.** The crashed worker's bug was recovered in all 4 graph runs and in 0 of 2 runs without coordination. Both runs without coordination stalled (they ended with a bug left undone); none of the 4 graph runs did.
- **The number of bugs fixed is about the same.** 9, 9 and 9.5 on average. The graph setup alone ranged from 8 to 10 between its two runs, so this gap is within run-to-run luck. We do not claim the graph fixes more bugs.
- **Memory did not show a benefit in these runs.** Graph + memory took the longest on average and used the most model calls. What memory does is visible on the Graph page: lessons are written and recalled for tickets that touch the same code.
- **The graph setups spent more worker sessions that ended without an approved fix** (8 and 4.5 against 1.5). We have not broken that number down further.
- **Small sample.** Two runs per setup, on a free-tier model service whose speed changes through the day (the same setup took 20 to 50 minutes). Small differences can be luck.
- "No coordination" is a simple baseline, not the worst possible one. No double claims happened in any run; the claim race in section 7 shows why they can.
- The bugs are planted by us and each ticket has its own tests, so most patches do not overlap.

**The demo run on the screen (run-5):** 10 of 10 approved in 1772 s (about 29.5 minutes) with 3 workers, 0 double claims, 13 claims granted, 3 same-file overlaps handled, 3 handoffs recovered. The earlier run-4, before we fixed agents giving up too early and the order of work, got 6 of 10 in 5406 s.

## 7. Claim race and load tests

Scripts need no model key.

| Test | What it shows |
|---|---|
| `foreman-code/race_test.py` | 50 workers claim one ticket at once, 20 rounds, atomic claim versus a naive "look, then take" claim. Result in `race_results.md` |
| `foreman-schema/load_test.py` | 100 workers claiming, 50 workers finishing 300 tickets, mixed reads and writes, the conflict rule with the real tickets, 5 repos at once. Result in `load_test_results.md` |
| `foreman-db-setup/check_persistence.sh` | Stops and restarts the database container and checks the graph and the run are still there |

Result from our run against FalkorDB in Docker (`race_results.md`):

| Claim | Rounds with exactly one winner | Most workers told "yours" in one round | p50 (ms) | p95 (ms) |
|---|---|---|---|---|
| Atomic claim (the product) | 20 of 20 | 1 | 21.2 | 24.3 |
| Naive "look, then take" | 0 of 20 | 50 | 64.1 | 68.1 |

With the naive claim, all 50 workers were told they own the ticket in every round. With the atomic claim, exactly one was, every time. Timings are from one laptop and will differ on other machines.

## 8. Run it

You need Docker, Python 3.11 or newer, and git. Put the four folders side by side: `foreman-code`, `foreman-schema`, `foreman-db-setup`, `tinydb` (the seeded copy with the 10 bugs).

**Just watch the recorded run (no database, no key).**

```
cd foreman-code
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run_all.sh --screen
```

Then open http://localhost:8000. The replay file is `ui/replay/run-5.json`.

**Run the whole crew yourself.**

```
cd foreman-db-setup && docker compose up -d && cd ../foreman-code
cp .env.example .env            # then put your own NVIDIA key in .env (never commit it)
./run_all.sh                    # database check, key check, code load, crew run, replay export, screen
QUICK=1 ./run_all.sh            # one worker, one ticket, about 3 minutes: checks everything is connected
```

**Tests without a key** (a pretend model makes the tool calls): the `foreman-code/test_*.py` files. Some of them need the answer-key kit (`--kit ../a2-eval-kit`: the reference fixes for the 10 planted bugs). We keep that kit out of this repository on purpose, so the agents can never see the answers. The crew itself (`run_all.sh`) does not need it.

**The comparison**: see `foreman-code/EVAL.md`.

### 9. Analyze another Python project (code graph)

Foreman supports configurable Python source discovery and
pytest tracing through a `foreman.yaml` file.

Example configuration:

```yaml
name: sample_project
repo_path: .
source_dirs:
  - miniapp
test_dirs:
  - tests
test_command:
  - "{python}"
  - "-m"
  - "pytest"
timeout_seconds: 60
```

Paths are relative to the configuration file's directory
(`repo_path`) or the target repository (`source_dirs`
and `test_dirs`).

From the Foreman repository root, start FalkorDB:

```bash
docker compose -f foreman-db-setup/docker-compose.yml up -d
```

Podman Compose can be used instead of Docker Compose.

Generate the test trace:

```bash
uv run python foreman-code/trace_tests.py --config examples/sample_project/foreman.yaml --out results/sample-trace.json
```

Load the code graph:

```bash
uv run python foreman-schema/load_code.py --config examples/sample_project/foreman.yaml --trace results/sample-trace.json
```

The sample graph is named `foreman_sample_project`.

Note: Loading a project deletes and rebuilds its named graph.
Tracing runs the configured pytest tests, so only analyze
trusted repositories.

Run the offline regression tests:

```bash
uv run python -m pytest tests/test_configurable_project.py -v
```

The optional FalkorDB integration test requires a running
database and the environment variable
`FOREMAN_RUN_GRAPH_TESTS=1`.

Current scope: configurable code indexing, test tracing,
and graph generation. The AI-agent orchestration remains
specific to the original TinyDB demonstration.



## 10. Repository map

| Folder | What is in it |
|---|---|
| `foreman-code/` | Agents, tools, model client, crew runner, evaluation runner, show builder, server, screen (`ui/`) |
| `foreman-schema/` | Graph schema, `graph_api.py` (every function the agents call), code loader, contract tests, load test |
| `foreman-db-setup/` | Docker Compose for FalkorDB, setup check, persistence check |
| `tinydb/` | The seeded copy of TinyDB with 10 planted bugs |

## 11. Credits and licences

- TinyDB (MIT) by Markus Siemens is the codebase the agents fix.
- The six animated characters are rendered by the **bible-strong-avatar-lab** engine (AGPL-3.0, © Stéphane Montlouis-Calixte, https://github.com/smontlouis/bible-strong-avatar-lab). Because it is AGPL-3.0, this repository is public and the credit is shown on the page. The office scene is drawn for this page in SVG.
- AI assistance in building the project is disclosed in `foreman-code/AI-USAGE.md`.
