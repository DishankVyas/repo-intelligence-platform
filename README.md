# RepoIntel: Graph-Grounded Repository Intelligence Platform

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Node: 18+](https://img.shields.io/badge/node-18+-green.svg)](https://nodejs.org/)
[![Tests: 158 passed](https://img.shields.io/badge/tests-158%20passed-success.svg)](backend/tests/)

An end-to-end framework and evaluation benchmark for repository-level architectural comprehension, comparing how structured code representations (raw source code, dependency graphs, and knowledge graphs) affect the factual accuracy, coverage, and hallucination rates of open-weight Large Language Models.

Developed as a research artifact accompanying the paper:
> **"Grading the Grader: A Parser-Grounded Audit of LLM-as-Judge Coverage Metrics for Repository-Level Code Summarization"**

---

## Overview

When summarizing multi-module software repositories, LLMs typically ingest concatenated raw source code or simple file listings, which frequently exceed effective context windows and induce architectural hallucinations. 

**RepoIntel** explores an alternative approach:
1. **Static AST Extraction:** Automatically parses backend codebases using Tree-sitter, extracting architectural components (framework configurations, routes/endpoints, classes, controllers, service functions, and external packages).
2. **Knowledge Graph Construction:** Maps repository entities and relational edges into a Neo4j property graph.
3. **Structured Context Linearization:** Renders grounded structural prompts under strict token budgets.
4. **Three-Way Ablation Benchmark:** Evaluates LLM comprehension across three representations:
   - `raw`: Linearized raw source files (concatenated according to structural importance).
   - `dependency_graph`: Multi-level dependency DAG showing module relationships and import hierarchies.
   - `knowledge_graph`: Full relational schema containing API routes, database models, class hierarchies, and external integrations.
5. **Deterministic Verification & Metric Auditing:** Replaces brittle LLM-as-a-judge coverage metrics with a parser-grounded deterministic oracle, cross-model secondary judges, and blinded human evaluation protocols.

```
                  +----------------------------------------------+
                  |           Target Code Repository             |
                  +----------------------------------------------+
                                         |
                                         v
                     +----------------------------------------+
                     |   AST Parser (Tree-sitter TS/JS)       |
                     |   - Express.js & NestJS Analyzers      |
                     +----------------------------------------+
                                         |
                       +-----------------+-----------------+
                       |                                   |
                       v                                   v
        +-----------------------------+     +-----------------------------+
        |  Dependency Graph (DAG)     |     |  Property Graph (Neo4j)     |
        |  Module & import topologies |     |  Endpoints, DB, classes     |
        +-----------------------------+     +-----------------------------+
                       |                                   |
                       +-----------------+-----------------+
                                         |
                                         v
                     +----------------------------------------+
                     | Context Builder & Token Budget Engine  |
                     |  - raw / dependency / knowledge graph  |
                     |  - Strict budget truncation (8192 ctx) |
                     +----------------------------------------+
                                         |
                                         v
                     +----------------------------------------+
                     | Open-Weight LLM Generation (Ollama)    |
                     |  - Qwen2.5-Coder, CodeLlama, Granite   |
                     +----------------------------------------+
                                         |
                                         v
                     +----------------------------------------+
                     |          Evaluation Harness            |
                     |  - Deterministic Coverage Oracle       |
                     |  - Grounded Hallucination Detection    |
                     |  - G-Eval Quality & Overlap Metrics    |
                     |  - Blinded Human Review Sheet Pipeline |
                     +----------------------------------------+
```

---

## Repository Structure

```
repo-intel-platform/
├── backend/
│   ├── app/
│   │   ├── acquisition/       # Repository cloning, shallow checkout & validation
│   │   ├── parsers/           # Tree-sitter AST parsers for Express and NestJS
│   │   ├── graph/             # Neo4j schema construction, Cypher queries & builders
│   │   ├── context/           # Context builders (raw, dependency DAG, knowledge graph)
│   │   ├── providers/         # Ollama inference client & prompt templates
│   │   ├── evaluation/        # Oracle, hallucination scoring, G-Eval, BERTScore harness
│   │   ├── schemas/           # Pydantic data schemas for AST nodes and metrics
│   │   └── main.py            # FastAPI application endpoints
│   ├── scripts/               # Harness runners, rejudging, review sheet generation
│   ├── tests/                 # 158 automated unit and integration tests
│   ├── requirements.txt       # Python dependencies for full stack
│   └── requirements-cluster.txt # Lightweight requirements for GPU nodes
├── frontend/
│   ├── src/
│   │   ├── components/        # Interactive Cytoscape graph & Mermaid diagram renderers
│   │   ├── pages/             # Dashboard, Analyze, Architecture, 3-Way Ablation
│   │   └── api/               # Typed client connecting to FastAPI backend
│   ├── package.json           # React 18, TypeScript, Tailwind CSS, Vite
│   └── vite.config.ts         # Vite build configuration
├── docs/
│   ├── REPORT.md              # Research paper draft and complete experimental results
│   ├── GPU_BATCH.md           # Guide for distributed multi-MIG GPU execution
│   └── evaluation_metrics.pdf # Metric formalizations and visual data distributions
├── hpc/                       # Production PBS batch scripts for Altair cluster execution
├── 18_repo_urls.txt           # Benchmark dataset: 18 curated open-source repositories
├── context_pack.json          # Pre-built, frozen context pack for offline replication
└── docker-compose.yml         # Container definitions (Neo4j, Ollama, Backend, Frontend)
```

---

## Key Features

* **Dual Framework Parsers:** Native support for both un-opinionated Express.js and modular NestJS backends, extracting controllers, decorators, routes, schemas, and service dependencies.
* **Deterministic Coverage Oracle:** An evaluation oracle that checks summary claims directly against ground-truth AST parser symbols, eliminating LLM-as-a-judge non-determinism and format sensitivity.
* **Fine-Grained Hallucination Measurement:** Deconstructs generated summaries into discrete claims, cross-referencing each against static analysis facts to report a grounded hallucination score in `[0, 1]`.
* **Zero-Network Offline Replication:** Includes `context_pack.json` (frozen context strings and parsed ASTs for all 18 benchmark repositories), allowing exact replication of generation and judging runs without network access or live Git clones.
* **Interactive Visual Dashboard:** A React frontend featuring interactive dependency topologies (Cytoscape), architectural diagrams (Mermaid), and a side-by-side three-way representation comparison tool.

---

## Dataset

The benchmark evaluates **18 production-grade open-source repositories** (9 Express.js, 9 NestJS) selected for structural diversity, varying from minimal microservices to massive boilerplate frameworks:

| Framework | Repositories |
| :--- | :--- |
| **Express.js** | `node-express-boilerplate`, `express-rest-boilerplate`, `node-express-mongodb-jwt-rest-api-skeleton`, `express-mongoose-es6-rest-api`, `express-sequelize-api-boilerplate`, `api-design-node-v3`, `rest-api-nodejs-mongodb`, `node-express-sequelize-postgresql`, `node_passport_login` |
| **NestJS** | `clean-architecture-nestJS`, `nestjs-boilerplate`, `awesome-nest-boilerplate`, `nestjs-realworld-example-app`, `domain-driven-hexagon`, `nestjs-recipe`, `ack-nestjs-boilerplate`, `nestjs-prisma-starter`, `nestjs-starter-rest-api` |

The repository URLs and metadata are cataloged in [`18_repo_urls.txt`](18_repo_urls.txt).

---

## Quickstart & Local Setup

### Prerequisites
* Docker & Docker Compose (v2.20+)
* Python 3.10+
* Node.js 18+ (optional, for running frontend outside Docker)
* Ollama installed locally or accessible via network

### Option 1: Docker Compose (Full Stack)

1. Clone the repository:
   ```bash
   git clone https://github.com/DishankVyas/repo-intel-platform.git
   cd repo-intel-platform
   ```

2. Create the backend environment configuration:
   ```bash
   cp backend/.env.example backend/.env
   # Edit backend/.env to set your NEO4J_PASSWORD and OLLAMA_HOST
   ```

3. Launch Neo4j, FastAPI Backend, and React Frontend:
   ```bash
   docker compose up -d
   ```
   - **Frontend UI:** `http://localhost:5173`
   - **Backend API Docs:** `http://localhost:8000/docs`
   - **Neo4j Browser:** `http://localhost:7474`

### Option 2: Local Development Setup

#### Backend Setup
```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run the test suite
pytest -v
```

#### Frontend Setup
```bash
cd frontend
npm install
npm run build
npm run dev
```

---

## Reproducing the Evaluation Benchmark

To reproduce the study's experimental results without needing a live Neo4j instance or active internet connection, use the pre-built `context_pack.json`:

### 1. Run Generation & Primary Evaluation Battery
Execute the battery across any model served by Ollama (e.g., `qwen2.5-coder:14b`):
```bash
cd backend
python3 -m app.evaluation.harness \
  --context-pack ../context_pack.json \
  --model qwen2.5-coder:14b \
  --judge gemma2:9b \
  --runs 1 \
  --out-dir evaluation_results
```
This produces an SQLite database (`battery_qwen14b.db`) and CSV report (`battery_qwen14b.csv`) tracking latency, input tokens, hallucination scores, coverage scores, and text overlap.

### 2. Multi-Judge Cross-Validation
Re-score previously generated summaries using an independent secondary judge model:
```bash
python3 -m scripts.rejudge \
  --judge mistral:7b-instruct \
  --src-db evaluation_results/battery_qwen14b.db \
  --context-pack ../context_pack.json \
  --phase judge
```

### 3. Generate Blinded Human Validation Sheets
To validate the reliability of LLM judges against human annotators:
```bash
# 1. Extract full claim breakdown from model summaries
python3 -m scripts.rejudge_full_claims \
  --src-db evaluation_results/battery_qwen14b.db \
  --sample-csv evaluation_results/sample_manifest.csv \
  --context-pack ../context_pack.json \
  --out evaluation_results/human_validation_claims.csv

# 2. Assemble blinded review workbook
python3 -m scripts.build_review_sheet \
  --pack ../context_pack.json \
  --claims-csv evaluation_results/human_validation_claims.csv \
  --out evaluation_results/human_review.xlsx
```
The resulting Excel workbook blindingly presents claims alongside ground-truth AST facts with dropdown verification controls. Once graded, score inter-rater reliability via:
```bash
python3 -m scripts.score_human_validation \
  --sheet evaluation_results/human_review.xlsx
```

---

## High-Performance Cluster (HPC) Execution

For distributed evaluation on institutional HPC clusters using PBS / Altair Access with NVIDIA A100 or H100 GPUs:

* Refer to [`docs/GPU_BATCH.md`](docs/GPU_BATCH.md) for MIG partition setup and environment variables.
* Production batch scripts are provided in [`hpc/`](hpc/):
  - `hpc/run_battery.sh`: Single generator model execution on a dedicated MIG slice.
  - `hpc/run_rejudge.sh`: Network-free secondary judging pass using frozen AST caches.
  - `hpc/run_all.sh`: Automated multi-stage orchestration (battery, rejudge, G-Eval, human validation).

---

## Summary of Findings

Across 160 evaluations on the 18-repository benchmark using scaled open-weight models (`codellama:13b-instruct`, `qwen2.5-coder:14b`, `qwen2.5-coder:32b`), structured knowledge graph contexts consistently outperform raw source code across all factual accuracy metrics:

| Model | Context Variant | Hallucination Rate ↓ | Fact Coverage ↑ | BLEU-4 | ROUGE-L | METEOR |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **CodeLlama 13B** | `raw` | 78.9% | 17.8% | 0.0155 | 0.1690 | 0.1375 |
| | `dependency_graph` | 51.5% | 45.1% | 0.0129 | 0.1579 | 0.1486 |
| | `knowledge_graph` | **23.9%** | **77.5%** | 0.0127 | 0.1558 | 0.1523 |
| **Qwen 2.5 Coder 14B** | `raw` | 46.8% | 45.4% | 0.0147 | 0.1760 | 0.1553 |
| | `dependency_graph` | 38.7% | 68.2% | 0.0143 | 0.1609 | 0.1568 |
| | `knowledge_graph` | **18.9%** | **83.0%** | 0.0110 | 0.1555 | 0.1469 |
| **Qwen 2.5 Coder 32B** | `raw` | 54.9% | 44.2% | 0.0158 | 0.1838 | 0.1622 |
| | `dependency_graph` | 39.5% | 69.3% | 0.0121 | 0.1550 | 0.1278 |
| | `knowledge_graph` | **20.9%** | **89.4%** | 0.0142 | 0.1651 | 0.1459 |

* **Hallucination Reduction:** Transitioning from `raw` source code to `knowledge_graph` representations yields a **59.6% to 69.7% relative reduction in hallucination rates** across models.
* **Coverage Expansion:** Knowledge graphs deliver up to **4.35× higher structural fact coverage** compared to raw code while strictly adhering to token budgets.
* **LLM-as-a-Judge Audit:** As detailed in [`docs/REPORT.md`](docs/REPORT.md), prompt-based coverage judges that demand verbatim lexical matching exhibit a balanced accuracy of only **0.541** (chance level), underscoring the critical necessity of parser-grounded deterministic oracles.


---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
