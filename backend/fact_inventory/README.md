# Independent fact inventory (CICT-2026 reviewer R1)

The reviewer's first weakness: the same parser builds the knowledge graph *and* the
coverage fact list, so coverage is circular. This directory holds fact lists written
**by a person reading the source**, never derived from parser output, for a subset of
the 18 repositories. `scripts/score_fact_inventory.py` then reports:

1. **Parser accuracy against the inventory**: per-category precision and recall. This
   quantifies how far the parser's fact list (the circular one) is from the truth.
2. **Summary coverage against the inventory**: every stored summary re-scored with the
   deterministic matcher (`app/evaluation/oracle.py`) against these facts instead of the
   parser's. If the knowledge graph still beats raw source here, the result does not
   depend on the parser grading its own output.

## Which repositories

Aim for 5–6, spread across both frameworks and sizes. These are small enough to
annotate exhaustively in about an hour each:

| Repository | Framework | Why |
|---|---|---|
| node-express-sequelize-postgresql | Express | smallest; raw fits untruncated |
| node_passport_login | Express | small; raw fits untruncated |
| express-mongoose-es6-rest-api | Express | mid-size; ES-module style |
| api-design-node-v3 | Express | ES modules; parser had a known gap here |
| nestjs-recipe | NestJS | small NestJS |
| clean-architecture-nestJS | NestJS | mid-size NestJS |

## Protocol — what keeps it independent

- **Do not open** `context_pack.json`, `evaluation_results/parse_cache/`, the knowledge
  graph, the dashboard, or `annotations/*.json` for a repo before its inventory is done.
  Those are all parser output.
- Check out the **exact commit** the study used (`commit_sha` in the template) so the
  inventory and the summaries describe the same code.
- Read the source (and `package.json`) directly. Record what is *true of the code*,
  not what you expect a parser to find.
- Record the time spent and anything ambiguous in `notes`.

## Fact categories — the same strings the coverage metric uses

| Category | Write it as | Rule |
|---|---|---|
| `Framework` | `express` / `nestjs` | one entry |
| `Language` | `javascript` / `typescript` | one entry |
| `Dependency` | `mongoose`, `@nestjs/common` | every package under `dependencies` in the root `package.json` (not `devDependencies`) |
| `Endpoint` | `GET /api/tutorials/:id` | every HTTP route, with its **full** path including router mount prefixes and `@Controller()` prefixes. Exclude `setGlobalPrefix()` and URI versioning (applied uniformly; matches `annotations/README.md`) |
| `Class/Service` | `TutorialService` | every class declared in application source (not tests, not `node_modules`) |
| `Database entity` | `User` | every model/entity/schema that maps to a table or collection |

## Format

Start each file from the generated template:

```bash
cd backend
python -m scripts.score_fact_inventory --template node_passport_login
```

```json
{
  "repo_name": "node_passport_login",
  "commit_sha": "…",
  "annotator": "your name",
  "verified": false,
  "minutes_spent": null,
  "notes": "",
  "facts": {
    "Framework": ["express"],
    "Language": ["javascript"],
    "Dependency": [],
    "Endpoint": [],
    "Class/Service": [],
    "Database entity": []
  }
}
```

Set `"verified": true` only when the file is complete. The scorer **skips** files that
are not marked verified, so a half-finished inventory can never leak into a result.

## Scoring

```bash
cd backend
python -m scripts.score_fact_inventory           # writes evaluation_results/FACT_INVENTORY_RESULTS.txt
```
