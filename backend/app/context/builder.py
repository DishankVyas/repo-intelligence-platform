"""
Structured Context Builder: Neo4j query results -> a plain-text context string,
handed to the Ollama orchestration layer as `context` in
BaseLLMProvider.generate_summary(). Implements 2 of the 3 ContextVariant arms.

RAW is intentionally NOT implemented here -- it doesn't touch the graph at all
(SCHEMA.md), and needs the repo's raw source text, which analyze_repository()
doesn't retain past its cleanup step. That's Week 3 ablation work, and needs an
explicit decision about where raw source gets cached across the pipeline.
"""

from __future__ import annotations

from typing import Optional

from neo4j import Driver, ManagedTransaction

from app.schemas.llm_result import ContextVariant


class ContextBuilderError(Exception):
    """Raised when a context can't be built for the requested variant/repo."""


def build_context(
    driver: Driver, repo_name: str, variant: ContextVariant, model: Optional[str] = None
) -> str:
    """`model` selects whose tokenizer the budget is counted in (see token_budget.py);
    None keeps the cl100k proxy used by the live API."""
    if variant == ContextVariant.RAW:
        raise ContextBuilderError(
            "RAW context requires the repo's raw source text, which isn't retained "
            "past analyze_repository()'s cleanup step -- not implemented until "
            "Week 3's representation ablation work."
        )

    with driver.session() as session:
        if variant == ContextVariant.DEPENDENCY_GRAPH:
            rows = session.execute_read(_query_dependency_graph, repo_name)
            return _format_dependency_graph(rows, repo_name, model)

        data = session.execute_read(_query_knowledge_graph, repo_name)
        return _format_knowledge_graph(data, repo_name, model)


def _query_dependency_graph(tx: ManagedTransaction, repo_name: str) -> list[dict]:
    result = tx.run(
        """
        MATCH (r:Repository {name: $repo_name})-[:HAS_MODULE]->(m:Module)
        OPTIONAL MATCH (m)-[:IMPORTS]->(target:Module)
        OPTIONAL MATCH (r)-[:DEPENDS_ON]->(dep:ExternalDependency)
        RETURN m.path AS module_path,
               collect(DISTINCT target.path) AS imports,
               collect(DISTINCT dep.name) AS external_dependencies
        """,
        repo_name=repo_name,
    )
    return [dict(record) for record in result]


def _format_dependency_graph(rows: list[dict], repo_name: str, model: Optional[str] = None) -> str:
    from app.context.token_budget import compute_context_token_budget, get_encoder
    import logging
    logger = logging.getLogger(__name__)

    if not rows:
        return "No modules found for this repository."

    enc = get_encoder(model)
    budget = compute_context_token_budget(model=model)
    
    all_external: set[str] = set()
    modules_lines = []
    for row in rows:
        imports = [i for i in row["imports"] if i]
        modules_lines.append(f"- {row['module_path']} imports: {', '.join(imports) or '(none)'}")
        all_external.update(d for d in row["external_dependencies"] if d)

    deps_line = f"External dependencies: {', '.join(sorted(all_external)) or '(none)'}"
    
    full_text = "Module dependency graph:\n" + "\n".join(modules_lines) + "\n\n" + deps_line
    total_raw_tokens = len(enc.encode(full_text))
    if total_raw_tokens > budget + 50:
        warning_msg = f"WARNING: Repo {repo_name} dependency_graph context exceeds token budget! ({total_raw_tokens} > {budget} tokens). It will be truncated."
        logger.warning(warning_msg)
        print(warning_msg)

    output_lines = ["Module dependency graph:"]
    current_tokens = len(enc.encode(output_lines[0] + "\n"))
    deps_tokens = len(enc.encode("\n\n" + deps_line))

    for i, line in enumerate(modules_lines):
        line_tokens = len(enc.encode(line + "\n"))
        if current_tokens + line_tokens > budget:
            output_lines.append(f"... {len(modules_lines) - i} more modules omitted (token budget)")
            break
        output_lines.append(line)
        current_tokens += line_tokens

    if current_tokens + deps_tokens <= budget:
        output_lines.append("")
        output_lines.append(deps_line)
    else:
        msg = f"WARNING: Dropped external dependencies line for {repo_name} (dependency_graph) due to token budget."
        logger.warning(msg)
        print(msg)

    return "\n".join(output_lines)


def _query_knowledge_graph(tx: ManagedTransaction, repo_name: str) -> dict:
    # Kept in sync with schema.cypher's "knowledge_graph variant" reference query.
    result = tx.run(
        """
        MATCH (r:Repository {name: $repo_name})
        OPTIONAL MATCH (r)-[:HAS_MODULE]->(m:Module)
        OPTIONAL MATCH (m)-[:IMPORTS]->(target:Module)
        OPTIONAL MATCH (m)-[:DEFINES]->(c:Class)
        WITH r, m,
             collect(DISTINCT target.path) AS module_imports,
             collect(DISTINCT c.name) AS module_classes
        WITH r, collect({
                 path: m.path, imports: module_imports, classes: module_classes
             }) AS module_rows
        OPTIONAL MATCH (r)-[:HAS_ENDPOINT]->(e:Endpoint)
        OPTIONAL MATCH (e)-[:HANDLED_BY]->(handler:Function)
        OPTIONAL MATCH (r)-[:DEPENDS_ON]->(dep:ExternalDependency)
        OPTIONAL MATCH (r)-[:HAS_CONFIG]->(cfg:ConfigFile)
        OPTIONAL MATCH (r)-[:HAS_DATABASE_ENTITY]->(db:DatabaseEntity)
        RETURN r.detected_framework AS framework,
               r.framework_version AS framework_version,
               module_rows,
               collect(DISTINCT {method: e.method, path: e.path, handler: handler.name}) AS endpoints,
               collect(DISTINCT dep.name) AS external_dependencies,
               collect(DISTINCT cfg.path) AS config_files,
               collect(DISTINCT db.name) AS database_entities
        """,
        repo_name=repo_name,
    )
    record = result.single()
    return dict(record) if record else {}


def _format_knowledge_graph(data: dict, repo_name: str, model: Optional[str] = None) -> str:
    from app.context.token_budget import compute_context_token_budget, get_encoder
    import logging
    logger = logging.getLogger(__name__)

    if not data:
        return "No graph data found for this repository."

    enc = get_encoder(model)
    budget = compute_context_token_budget(model=model)

    module_rows = [m for m in data.get("module_rows", []) if m and m.get("path")]
    module_rows.sort(key=lambda m: m["path"])
    classes = sorted({c for m in module_rows for c in (m.get("classes") or []) if c})
    endpoints = [e for e in data.get("endpoints", []) if e.get("path")]
    deps = sorted(d for d in data.get("external_dependencies", []) if d)
    configs = sorted(c for c in data.get("config_files", []) if c)
    db_entities = sorted(d for d in data.get("database_entities", []) if d)

    # Fast fail check on full untruncated string
    lines_untrunc = []
    lines_untrunc.append(f"Framework: {data.get('framework') or 'unknown'} ({data.get('framework_version') or 'version unknown'})\n")
    lines_untrunc.append(f"Modules ({len(module_rows)}):")
    if module_rows:
        for m in module_rows:
            imports = sorted(i for i in (m.get("imports") or []) if i)
            defines = sorted(c for c in (m.get("classes") or []) if c)
            lines_untrunc.append(f"- {m['path']}")
            lines_untrunc.append(f"    imports: {', '.join(imports) or '(none)'}")
            if defines:
                lines_untrunc.append(f"    defines: {', '.join(defines)}")
    else:
        lines_untrunc.append("(none)")
    lines_untrunc.append("")

    lines_untrunc.append(f"Classes ({len(classes)}):")
    lines_untrunc.extend([f"- {c}" for c in classes] or ["(none)"])
    lines_untrunc.append("")

    lines_untrunc.append(f"API endpoints ({len(endpoints)}):")
    if endpoints:
        for e in endpoints:
            handler = e.get("handler") or "(inline handler, no stable function reference)"
            lines_untrunc.append(f"- {e['method']} {e['path']} -> {handler}")
    else:
        lines_untrunc.append("(none)")
    lines_untrunc.append("")

    lines_untrunc.append(f"External dependencies: {', '.join(deps) or '(none)'}")
    lines_untrunc.append(f"Config files: {', '.join(configs) or '(none)'}")
    lines_untrunc.append(f"Database entities: {', '.join(db_entities) or '(none)'}")

    full_text = "\n".join(lines_untrunc)
    total_raw_tokens = len(enc.encode(full_text))
    if total_raw_tokens > budget + 50:
        warning_msg = f"WARNING: Repo {repo_name} knowledge_graph context exceeds token budget! ({total_raw_tokens} > {budget} tokens). It will be truncated."
        logger.warning(warning_msg)
        print(warning_msg)

    # Budgeted builder
    output_lines = []
    current_tokens = 0

    def add_line(text: str) -> bool:
        nonlocal current_tokens
        t_count = len(enc.encode(text + "\n"))
        if current_tokens + t_count > budget:
            return False
        output_lines.append(text)
        current_tokens += t_count
        return True

    if not add_line(f"Framework: {data.get('framework') or 'unknown'} ({data.get('framework_version') or 'version unknown'})\n"):
        return "\n".join(output_lines)

    # Modules
    if not add_line(f"Modules ({len(module_rows)}):"): return "\n".join(output_lines)
    if module_rows:
        for i, m in enumerate(module_rows):
            imports = sorted(im for im in (m.get("imports") or []) if im)
            defines = sorted(c for c in (m.get("classes") or []) if c)
            mod_lines = [f"- {m['path']}", f"    imports: {', '.join(imports) or '(none)'}"]
            if defines:
                mod_lines.append(f"    defines: {', '.join(defines)}")
            group_text = "\n".join(mod_lines)
            
            t_count = len(enc.encode(group_text + "\n"))
            if current_tokens + t_count > budget:
                output_lines.append(f"... {len(module_rows) - i} more modules omitted (token budget)")
                return "\n".join(output_lines)
            output_lines.extend(mod_lines)
            current_tokens += t_count
    else:
        if not add_line("(none)"): return "\n".join(output_lines)
    if not add_line(""): return "\n".join(output_lines)

    # Classes
    if not add_line(f"Classes ({len(classes)}):"): return "\n".join(output_lines)
    if classes:
        for i, c in enumerate(classes):
            if not add_line(f"- {c}"):
                output_lines.append(f"... {len(classes) - i} more classes omitted (token budget)")
                return "\n".join(output_lines)
    else:
        if not add_line("(none)"): return "\n".join(output_lines)
    if not add_line(""): return "\n".join(output_lines)

    # Endpoints
    if not add_line(f"API endpoints ({len(endpoints)}):"): return "\n".join(output_lines)
    if endpoints:
        for i, e in enumerate(endpoints):
            handler = e.get("handler") or "(inline handler, no stable function reference)"
            if not add_line(f"- {e['method']} {e['path']} -> {handler}"):
                output_lines.append(f"... {len(endpoints) - i} more endpoints omitted (token budget)")
                return "\n".join(output_lines)
    else:
        if not add_line("(none)"): return "\n".join(output_lines)
    if not add_line(""): return "\n".join(output_lines)

    # Footer
    if not add_line(f"External dependencies: {', '.join(deps) or '(none)'}"):
        output_lines.append("... dependencies omitted (token budget)")
        return "\n".join(output_lines)
    if not add_line(f"Config files: {', '.join(configs) or '(none)'}"):
        output_lines.append("... configs omitted (token budget)")
        return "\n".join(output_lines)
    if not add_line(f"Database entities: {', '.join(db_entities) or '(none)'}"):
        output_lines.append("... database entities omitted (token budget)")
        return "\n".join(output_lines)

    return "\n".join(output_lines)
