# TraceLink

A full-stack NoSQL project for investigating suspicious financial transfers. React powers the interactive analyst UI, FastAPI provides the backend, and Neo4j stores accounts, transactions, alerts, cases and notes as a property graph.

## Requirements

- Node.js 20+
- Python 3.11+
- Docker with a running Docker engine (or a local Neo4j 5 installation)

## Start locally

```bash
cd tracelink
docker compose up -d
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn backend.app:app --reload --port 8000
```

In a second terminal:

```bash
cd tracelink/frontend
npm install
npm run dev
```

Open **http://localhost:5173**. Click **Load demo data**, then **Run detection scan**. The seed is deterministic and only runs on an empty database. Database data persists in the Docker volume.

- API docs: http://localhost:8000/docs
- Neo4j Browser: http://localhost:7474
- Neo4j credentials: `neo4j` / `tracelink123`

To reset the local demonstration completely, run `docker compose down -v`, then `docker compose up -d` and load the seed again. This deletes the local Neo4j volume.

## Database model

```text
(:Account)-[:SENT]->(:Transaction)-[:RECEIVED]->(:Account)
(:Alert)-[:FLAGS]->(:Account)
(:Case)-[:INVESTIGATES]->(:Account)
(:Case)-[:HAS_NOTE]->(:Note)
```

Transactions are nodes so every transfer keeps its own ID, amount, timestamp, currency and known laundering label. The app's **Database model** page shows this structure and provides copyable Cypher queries for a live Neo4j Browser demonstration.

## Detection rules

1. Circular transfer: three different accounts form a directed transfer cycle.
2. Many-to-one: an account receives transfers from at least seven distinct senders.
3. Rapid onward transfer: an account sends at least 70% of a received amount onward to another account within two hours.

These rules produce **investigation alerts**, not proof of fraud. `laundering` is the source dataset label and remains separate from the app's alerts. Re-running the scan replaces prior rule alerts, while cases and notes persist.

## IBM AML dataset import

The app accepts CSV files using the [IBM AML transaction schema](https://github.com/IBM/AML-Data): `Timestamp`, `From Bank`, `Account`, `To Bank`, `Account.1`, `Amount Paid`, with optional currency, method and `Is Laundering`. Use **Transactions → Import IBM CSV**. Imports are limited to 30 MB and 50,000 rows per request to keep the demo responsive. Split larger source files into chunks and include the header row in each. Source account IDs are namespaced by bank to avoid collisions. For a clean evaluation, import into an empty database instead of mixing it with the seed.

The curated seed is the default demonstration because a random first slice of IBM data may omit complete laundering patterns. If using IBM data, choose a connected sample that retains all transactions involved in the patterns you want to demonstrate.

## Demonstration sequence

1. Load demo data and show the dashboard metrics.
2. Run detection and explain the three rules.
3. Open account `AC-0004` in Network explorer. Follow the red transfer cycle.
4. Open **Database model**, launch Neo4j Browser, and run the supplied graph and cycle queries.
5. Open an investigation on `AC-0004`, add a note, and change its status.
6. Refresh the app to prove Neo4j persistence.
7. Create a transaction and show it in the transaction list and graph.

## Checks

```bash
cd tracelink/frontend && npm run build
cd tracelink && TRACELINK_TEST_RESET=yes python3 -m pytest backend/tests
```

The integration test resets all nodes in the configured Neo4j database. Run it only against the local disposable demo instance. Without `TRACELINK_TEST_RESET=yes`, it skips safely.

## Cloud deployment

The repository includes `vercel.json` for Vercel Services: the Vite frontend and FastAPI backend deploy on one domain. A hosted **Neo4j AuraDB** instance is required. Create an AuraDB Free instance, then set these Vercel project environment variables before deploying:

- `NEO4J_URI` — AuraDB connection URI, usually `neo4j+s://...`
- `NEO4J_USER` — AuraDB username
- `NEO4J_PASSWORD` — AuraDB password
- `TRACELINK_ACCESS_KEY` — a long random key for data-changing requests

Do not commit these values. On the first data-changing action in the deployed UI, enter the analyst access key in TraceLink's access dialog. The browser keeps it in session storage for that tab. Read-only views are public; use synthetic demo data only. The database model page is self-contained and reads live node, relationship and constraint counts from Neo4j, so the demonstration does not depend on Aura Console availability.

Vercel Services configuration follows [Vercel's current polyglot service layout](https://vercel.com/kb/guide/vercel-services). After connecting the GitHub repository in Vercel and setting the variables, deploy from `main`, open `/api/health`, load demo data, and run a scan. AuraDB Free can auto-pause after inactivity, so the first query may take longer.

The production application is available at [tracelink-liard.vercel.app](https://tracelink-liard.vercel.app). The production database is an AuraDB Free instance; keep its generated credentials and the analyst access key outside the repository.
