"""
Express.js parser: walks a repo's .js/.ts files, uses tree-sitter to extract
functions, imports, and Express route definitions -- both the direct style
(app.get/post/... and router.get/post/...) and the chained style
(app.route('/x').get(...).post(...)) -- resolves router mount prefixes so
router-relative paths become real external paths, plus package.json for
external dependencies.

This is the first parser implementation and establishes the pattern that the
NestJS parser (Phase 6) will follow.
"""

from __future__ import annotations

import json
from pathlib import Path

import tree_sitter_javascript as tsjs
from tree_sitter import Language, Node, Parser, Query, QueryCursor

from app.parsers.base import BaseParser
from app.schemas.parser_schema import (
    ApiEndpoint,
    ClassNode,
    ConfigFile,
    ConfigFileType,
    Dependencies,
    DependencyType,
    ExternalDependency,
    FunctionNode,
    HttpMethod,
    InternalDependencyEdge,
    ModuleNode,
    ParsedRepository,
    RepoMetadata,
)

JS_LANGUAGE = Language(tsjs.language())

SKIP_DIRS = {"node_modules", ".git", "dist", "build", "coverage", ".next"}
# Test trees are not application architecture -- counting them as modules inflates the
# module set with files that don't describe the running service. Unlike SKIP_DIRS these
# are *recorded* in files_skipped rather than dropped silently, so the exclusion stays
# auditable (SKIP_DIRS would drown that list in node_modules noise).
TEST_DIRS = {"test", "tests", "__tests__", "__mocks__", "spec", "e2e", "cypress"}
# Test files living beside source (user.test.js, user.spec.js) -- same rationale.
TEST_FILE_SUFFIXES = (".test.js", ".spec.js", ".test.mjs", ".spec.mjs", ".test.cjs", ".spec.cjs")
SOURCE_EXTENSIONS = {".js", ".mjs", ".cjs"}  # .ts handled by NestJS parser later
MAX_FILES = 3000

ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}

# Tree-sitter queries, compiled once at module load
CALL_QUERY = Query(JS_LANGUAGE, "(call_expression) @call")
FUNCTION_DECL_QUERY = Query(
    JS_LANGUAGE,
    """
    (function_declaration name: (identifier) @name) @func
    """,
)
REQUIRE_QUERY = Query(
    JS_LANGUAGE,
    """
    (call_expression
        function: (identifier) @fn_name
        arguments: (arguments (string (string_fragment) @path))
    ) @call
    """,
)


class ExpressParser(BaseParser):
    framework_name = "express"

    def detect(self, repo_path: Path) -> bool:
        pkg_json = repo_path / "package.json"
        if not pkg_json.exists():
            return False
        try:
            data = json.loads(pkg_json.read_text())
        except (json.JSONDecodeError, OSError):
            return False
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        return "express" in deps

    def parse(self, repo_path: Path) -> ParsedRepository:
        warnings: list[str] = []
        files_skipped: list[str] = []

        pkg_json = repo_path / "package.json"
        pkg_data = json.loads(pkg_json.read_text()) if pkg_json.exists() else {}
        framework_version = (
            pkg_data.get("dependencies", {}).get("express")
            or pkg_data.get("devDependencies", {}).get("express")
        )

        modules: list[ModuleNode] = []
        functions: list[FunctionNode] = []
        classes: list[ClassNode] = []
        api_endpoints: list[ApiEndpoint] = []
        internal_deps: list[InternalDependencyEdge] = []

        source_files = self._collect_source_files(repo_path, files_skipped)

        if len(source_files) > MAX_FILES:
            warnings.append(
                f"Repo has {len(source_files)} source files, exceeding MAX_FILES={MAX_FILES}. "
                f"Only the first {MAX_FILES} were parsed."
            )
            source_files = source_files[:MAX_FILES]

        parser = Parser(JS_LANGUAGE)
        path_to_module_id: dict[Path, str] = {}
        routes_by_module: dict[str, list[ApiEndpoint]] = {}
        # Same-file router.use('/x', <local router>) prefixes -- local to one module.
        own_prefixes: dict[str, str] = {}
        # Cross-file mounts as graph edges (target_module_id -> (source_module_id,
        # local_prefix)) rather than composed immediately, so a router mounted
        # through another router's own mount point resolves to the full chained
        # prefix. See _resolve_mount_chain.
        mount_edges: dict[str, tuple[str, str]] = {}

        # Keyed on the RESOLVED path: _resolve_imports() resolves its candidates, and on
        # macOS a clone under /var/folders resolves to /private/var/folders, so keying on
        # the unresolved path made every internal-import lookup miss silently.
        for i, file_path in enumerate(source_files):
            module_id = f"mod_{i}"
            path_to_module_id[file_path.resolve()] = module_id

        for file_path in source_files:
            module_id = path_to_module_id[file_path.resolve()]
            rel_path = file_path.relative_to(repo_path).as_posix()
            try:
                source_bytes = file_path.read_bytes()
            except OSError as e:
                warnings.append(f"Could not read {rel_path}: {e}")
                continue

            try:
                tree = parser.parse(source_bytes)
            except Exception as e:  # tree-sitter parse failures are rare but possible
                warnings.append(f"Failed to parse {rel_path}: {e}")
                continue

            root = tree.root_node

            cjs_imports, import_warnings = self._extract_requires(root, source_bytes)
            es_imports = self._extract_es_imports(root, source_bytes)
            resolved_import_ids = self._resolve_imports(
                cjs_imports + es_imports, file_path, repo_path, path_to_module_id
            )
            for target_id in resolved_import_ids:
                internal_deps.append(
                    InternalDependencyEdge(from_module_id=module_id, to_module_id=target_id)
                )

            module = ModuleNode(
                id=module_id,
                path=rel_path,
                imports=resolved_import_ids,
            )
            modules.append(module)

            file_functions = self._extract_functions(root, source_bytes, module_id)
            functions.extend(file_functions)
            func_name_to_id = {f.name: f.id for f in file_functions}

            # Routes are held back rather than appended immediately: a router's mount
            # prefix (app.use('/api/tutorials', router)) may be declared in a *different*
            # file than the routes themselves, so prefixes can only be applied once every
            # file has been scanned. See _apply_mount_prefixes below.
            routes_by_module[module_id] = self._extract_routes(
                root, source_bytes, module_id, func_name_to_id
            )
            self._collect_mounts(
                root,
                source_bytes,
                module_id,
                file_path,
                repo_path,
                path_to_module_id,
                own_prefixes,
                mount_edges,
            )

        mount_prefixes = self._resolve_mount_chain(own_prefixes, mount_edges)
        api_endpoints = self._apply_mount_prefixes(routes_by_module, mount_prefixes)

        external_deps = [
            ExternalDependency(name=name, version=version, dep_type=DependencyType.RUNTIME)
            for name, version in pkg_data.get("dependencies", {}).items()
        ] + [
            ExternalDependency(name=name, version=version, dep_type=DependencyType.DEV)
            for name, version in pkg_data.get("devDependencies", {}).items()
        ]

        config_files = self._find_config_files(repo_path)

        metadata = RepoMetadata(
            name=repo_path.name,
            source_url="",  # filled in by caller (acquisition layer knows the URL)
            detected_language="javascript",
            detected_framework="express",
            framework_version=framework_version,
            files_scanned=len(source_files),
            files_skipped=files_skipped,
            parse_warnings=warnings,
        )

        return ParsedRepository(
            metadata=metadata,
            modules=modules,
            classes=classes,
            functions=functions,
            api_endpoints=api_endpoints,
            database_entities=[],  # Express has no standard ORM; left empty by design
            dependencies=Dependencies(internal=internal_deps, external=external_deps),
            config_files=config_files,
        )

    # -- helpers --------------------------------------------------------

    def _collect_source_files(self, repo_path: Path, files_skipped: list[str]) -> list[Path]:
        results = []
        for f in repo_path.rglob("*"):
            if not f.is_file():
                continue
            rel_parts = f.relative_to(repo_path).parts
            if any(part in SKIP_DIRS for part in rel_parts):
                continue
            # Hidden directories (.husky, .install-scripts, .github, ...) hold tooling
            # and scaffolding, not the service being described.
            if any(part.startswith(".") for part in rel_parts[:-1]):
                continue
            if f.suffix in SOURCE_EXTENSIONS:
                if f.name.endswith(TEST_FILE_SUFFIXES) or any(
                    part in TEST_DIRS for part in rel_parts
                ):
                    files_skipped.append(
                        f.relative_to(repo_path).as_posix() + " (test file, skipped)"
                    )
                    continue
                results.append(f)
            elif f.suffix in {".ts", ".tsx", ".jsx"}:
                files_skipped.append(f.relative_to(repo_path).as_posix() + " (non-.js source, skipped)")
        # Case-sensitive, part-by-part -- what PurePosixPath ordering does. Sorting Path
        # objects directly is case-INsensitive on Windows, which reorders modules (and
        # with them module ids, endpoints, and which facts survive the 40-per-category
        # cap) relative to a parse of the same commit on Linux or macOS.
        return sorted(results, key=lambda f: f.relative_to(repo_path).parts)

    def _extract_requires(
        self, root: Node, source: bytes
    ) -> tuple[list[str], list[str]]:
        """Returns list of string arguments passed to require(...) calls."""
        requires = []
        warnings: list[str] = []
        cursor = QueryCursor(REQUIRE_QUERY)
        matches = cursor.captures(root)
        fn_nodes = matches.get("fn_name", [])
        path_nodes = matches.get("path", [])
        # naive pairing by matching call node ranges; good enough for require() extraction
        for fn_node in fn_nodes:
            if source[fn_node.start_byte:fn_node.end_byte] != b"require":
                continue
            call_node = fn_node.parent
            for path_node in path_nodes:
                if call_node and call_node.start_byte <= path_node.start_byte <= call_node.end_byte:
                    requires.append(source[path_node.start_byte:path_node.end_byte].decode("utf-8", "replace"))
        return requires, warnings

    def _extract_es_imports(self, root: Node, source: bytes) -> list[str]:
        """Returns the string source of every `import ... from '<source>'` -- ES
        module syntax, which real Express repos increasingly use even in .js/.mjs
        files (via a bundler or Node's native ESM support). Complements
        _extract_requires rather than replacing it, since a repo (or a single file
        mid-migration) may mix both styles; previously a file written this way had
        an entirely empty imports list regardless of what it actually imported,
        which meant its whole subtree was invisible to dependency_graph. Re-exports
        (`export ... from`) are not handled, matching the same disclosed gap the
        NestJS parser's equivalent method already documents."""
        imports = []
        for node in self._find_all(root, "import_statement"):
            source_node = node.child_by_field_name("source")
            if source_node is None:
                continue
            fragment = next(
                (c for c in source_node.named_children if c.type == "string_fragment"), None
            )
            if fragment is not None:
                imports.append(
                    source[fragment.start_byte:fragment.end_byte].decode("utf-8", "replace")
                )
        return imports

    def _resolve_imports(
        self,
        raw_imports: list[str],
        current_file: Path,
        repo_path: Path,
        path_to_module_id: dict[Path, str],
    ) -> list[str]:
        """Resolves relative require() paths ('./routes/users') to module ids.
        External package requires ('express') are not resolved to module ids --
        they show up in `dependencies.external` via package.json instead."""
        resolved = []
        for imp in raw_imports:
            if not imp.startswith("."):
                continue  # external package, not an internal module edge
            candidate = (current_file.parent / imp).resolve()
            for suffix in ("", ".js", "/index.js", ".mjs", ".cjs"):
                probe = Path(str(candidate) + suffix)
                if probe in path_to_module_id:
                    resolved.append(path_to_module_id[probe])
                    break
        return resolved

    def _extract_functions(
        self, root: Node, source: bytes, module_id: str
    ) -> list[FunctionNode]:
        functions = []
        cursor = QueryCursor(FUNCTION_DECL_QUERY)
        matches = cursor.captures(root)
        for i, name_node in enumerate(matches.get("name", [])):
            name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace")
            functions.append(
                FunctionNode(
                    id=f"{module_id}_fn_{i}",
                    name=name,
                    module_id=module_id,
                )
            )
        return functions

    def _extract_routes(
        self,
        root: Node,
        source: bytes,
        module_id: str,
        func_name_to_id: dict[str, str],
    ) -> list[ApiEndpoint]:
        """Extracts Express routes in both supported styles:
          * direct:  app.get('/x', handler) / router.post('/x', handler)
          * chained: app.route('/x').get(handler).post(handler)  (and router.route(...))
        In the chained style the path lives on the `.route('/x')` call, and each
        HTTP-verb call hanging off the chain is a separate endpoint sharing that path.
        Both styles are gated on the chain rooting at an `app` or `router` object, so
        this stays general across Express repos without matching unrelated
        .get()/.post() calls on other objects (e.g. a promise or an array)."""
        routes = []
        cursor = QueryCursor(CALL_QUERY)
        matches = cursor.captures(root)
        route_index = 0
        for call_node in matches.get("call", []):
            func_node = call_node.child_by_field_name("function")
            if func_node is None or func_node.type != "member_expression":
                continue
            obj_node = func_node.child_by_field_name("object")
            prop_node = func_node.child_by_field_name("property")
            if obj_node is None or prop_node is None:
                continue

            method_name = source[prop_node.start_byte:prop_node.end_byte].decode("utf-8", "replace")
            if method_name not in ROUTE_METHODS:
                continue

            args_node = call_node.child_by_field_name("arguments")
            obj_name = source[obj_node.start_byte:obj_node.end_byte].decode("utf-8", "replace")

            if obj_node.type == "identifier" and obj_name in {"app", "router"}:
                # direct style: app.get('/x', handler) -- path is the first argument.
                # Requires a path AND a handler: app.get('port') / app.set('view engine', ...)
                # is Express's single-argument app-settings getter/setter overload, not a
                # route registration, and would otherwise be misread as a handler-less GET.
                if args_node is None or args_node.named_child_count < 2:
                    continue
                route_path = self._first_string_arg(call_node, source)
                base_name = obj_name
                handler_node = self._last_handler_arg(args_node, skip_first=True)
            else:
                # chained style: app.route('/x').get(handler) -- path comes from the
                # `.route('/x')` call up the chain; this call's only arg is the handler
                chain = self._chain_route_path(obj_node, source)
                if chain is None:
                    continue
                route_path, base_name = chain
                handler_node = self._last_handler_arg(args_node, skip_first=False)

            if route_path is None:
                continue

            handler_id = self._resolve_handler_node(handler_node, source, func_name_to_id)

            routes.append(
                ApiEndpoint(
                    id=f"{module_id}_ep_{route_index}",
                    method=HttpMethod(method_name.upper()) if method_name.upper() in HttpMethod.__members__ else HttpMethod.UNKNOWN,
                    path=route_path,
                    handler_function_id=handler_id,
                    framework_annotation=f"{base_name}.{method_name}",
                )
            )
            route_index += 1
        return routes

    def _first_string_arg(self, call_node: Node, source: bytes) -> str | None:
        """The value of the first argument of a call, if it's a plain string literal."""
        args_node = call_node.child_by_field_name("arguments")
        if args_node is None or args_node.named_child_count == 0:
            return None
        first = args_node.named_children[0]
        if first.type != "string":
            return None
        fragment = next(
            (c for c in first.named_children if c.type == "string_fragment"), None
        )
        if fragment is None:
            return None
        return source[fragment.start_byte:fragment.end_byte].decode("utf-8", "replace")

    def _chain_route_path(self, chain_obj: Node, source: bytes) -> tuple[str, str] | None:
        """Walk down a member/call chain from `chain_obj` looking for a `.route('/path')`
        call rooted at an `app`/`router` object -- the chained routing style. Returns
        (path, base_name) or None if this isn't such a chain."""
        node = chain_obj
        while node is not None and node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is None or fn.type != "member_expression":
                return None
            prop = fn.child_by_field_name("property")
            obj = fn.child_by_field_name("object")
            if prop is None or obj is None:
                return None
            prop_name = source[prop.start_byte:prop.end_byte].decode("utf-8", "replace")
            if prop_name == "route":
                path = self._first_string_arg(node, source)
                base_name = self._root_identifier(obj, source)
                if path is not None and base_name in {"app", "router"}:
                    return path, base_name
                return None
            node = obj  # keep walking down the chain (e.g. .post -> .get -> .route)
        return None

    def _root_identifier(self, node: Node, source: bytes) -> str | None:
        """The leftmost identifier a member/call chain hangs off (e.g. 'app' in
        app.route('/x').get(...))."""
        while node is not None:
            if node.type == "identifier":
                return source[node.start_byte:node.end_byte].decode("utf-8", "replace")
            if node.type == "member_expression":
                node = node.child_by_field_name("object")
            elif node.type == "call_expression":
                fn = node.child_by_field_name("function")
                node = fn.child_by_field_name("object") if (fn is not None and fn.type == "member_expression") else None
            else:
                return None
        return None

    def _last_handler_arg(self, args_node: Node | None, skip_first: bool) -> Node | None:
        """The handler node for an endpoint: the last argument, optionally skipping a
        leading path-string argument. Direct style is (path, handler); chained style
        is just (handler)."""
        if args_node is None:
            return None
        args = list(args_node.named_children)
        if skip_first:
            args = args[1:]
        return args[-1] if args else None

    def _resolve_handler_node(
        self, handler_node: Node | None, source: bytes, func_name_to_id: dict[str, str]
    ) -> str | None:
        """If the handler is a named identifier (e.g. router.post('/x', createUser)),
        resolve it to a known function id. Inline arrow/anonymous handlers, or handlers
        referenced through an object (e.g. controller.create), have no stable id here."""
        if handler_node is None or handler_node.type != "identifier":
            return None
        name = source[handler_node.start_byte:handler_node.end_byte].decode("utf-8", "replace")
        return func_name_to_id.get(name)

    def _collect_mounts(
        self,
        root: Node,
        source: bytes,
        module_id: str,
        current_file: Path,
        repo_path: Path,
        path_to_module_id: dict[Path, str],
        own_prefixes: dict[str, str],
        mount_edges: dict[str, tuple[str, str]],
    ) -> None:
        """Records where routers get mounted, so router-relative paths can be resolved
        into the real external paths. Two forms are handled, both common in real apps:

            app.use('/api/tutorials', router)        -- router declared in THIS file
            app.use('/users', require('./routes/users'))  -- router lives in ANOTHER file

        The second argument may also be an identifier bound earlier to a require()
        (`const routes = require('./routes/v1'); app.use('/v1', routes)`), which is
        resolved via the file's identifier -> require-path bindings.

        Cross-file mounts are recorded as graph edges (mount_edges) rather than
        composed immediately: if app.js mounts routes/api.js at /api, and
        routes/api.js itself mounts routes/v1/users.js at /v1, a single flat
        module_id -> prefix map can only ever record the innermost /v1, since it
        has no way to know routes/api.js was itself mounted anywhere. Recording
        the edge and letting _resolve_mount_chain walk it after every file has
        been scanned resolves the full /api/v1 chain instead. Same-file mounts
        (own_prefixes) are local to one module and compose with whatever chain
        that module is itself eventually mounted through.

        Not handled: mounts whose path comes from data rather than a literal, e.g.
        iterating an array of {path, route} objects and calling router.use(r.path,
        r.route). Those routes keep their router-relative paths."""
        local_requires = self._identifier_require_bindings(root, source)

        for call_node in QueryCursor(CALL_QUERY).captures(root).get("call", []):
            func_node = call_node.child_by_field_name("function")
            if func_node is None or func_node.type != "member_expression":
                continue
            obj_node = func_node.child_by_field_name("object")
            prop_node = func_node.child_by_field_name("property")
            if obj_node is None or prop_node is None:
                continue
            if source[prop_node.start_byte:prop_node.end_byte] != b"use":
                continue
            obj_name = source[obj_node.start_byte:obj_node.end_byte].decode("utf-8", "replace")
            if obj_name not in {"app", "router"}:
                continue

            args = call_node.child_by_field_name("arguments")
            if args is None or args.named_child_count < 2:
                continue  # app.use(middleware) with no path mounts nothing routable

            prefix = self._string_literal(args.named_children[0], source)
            if prefix is None:
                continue  # non-literal mount path -- can't resolve statically

            target = args.named_children[1]
            required_path = self._require_path(target, source)
            if required_path is None and target.type == "identifier":
                name = source[target.start_byte:target.end_byte].decode("utf-8", "replace")
                required_path = local_requires.get(name)

            if required_path is not None:
                target_id = self._resolve_single_import(
                    required_path, current_file, path_to_module_id
                )
                if target_id is not None:
                    _, existing_prefix = mount_edges.get(target_id, (module_id, ""))
                    mount_edges[target_id] = (
                        module_id, self._join_paths(existing_prefix, prefix)
                    )
            elif target.type == "identifier":
                # Router object mounted in the same file it was built in -- e.g.
                # `const router = express.Router(); app.use('/x', router)`. Restricted
                # to identifiers: a call expression second argument (`express.static(...)`,
                # an inline middleware factory, ...) is never a same-file router and must
                # not be attributed here -- doing so previously corrupted this module's
                # own mount prefix with the static-mount's unrelated path.
                own_prefixes[module_id] = self._join_paths(
                    own_prefixes.get(module_id, ""), prefix
                )
            # else: target is neither a resolvable require()/import nor a plain
            # identifier (e.g. an inline call expression) -- nothing safe to attribute.

    def _resolve_mount_chain(
        self, own_prefixes: dict[str, str], mount_edges: dict[str, tuple[str, str]]
    ) -> dict[str, str]:
        """Composes own_prefixes and mount_edges into one effective external prefix
        per module, walking mount_edges transitively so a router mounted through
        another router's own mount point gets the full composed prefix rather than
        just the innermost hop (the single-level limitation this replaces). A
        module with no incoming mount_edges entry is either mounted directly on
        `app` (the implicit root, external prefix '') or never externally mounted
        at all; either way the recursion terminates there. `seen` guards against a
        cyclic require() graph (A requires and mounts B, B requires and mounts A)
        turning into infinite recursion -- resolves to '' for whichever module
        closes the cycle, the same "give up cleanly on unresolvable input" stance
        the rest of this parser already takes elsewhere."""
        resolved: dict[str, str] = {}

        def effective(mid: str, seen: frozenset[str]) -> str:
            if mid in resolved:
                return resolved[mid]
            if mid in seen:
                return ""
            seen = seen | {mid}
            if mid in mount_edges:
                source_id, local_prefix = mount_edges[mid]
                external = self._join_paths(effective(source_id, seen), local_prefix)
            else:
                external = ""
            result = self._join_paths(external, own_prefixes.get(mid, ""))
            resolved[mid] = result
            return result

        for mid in set(own_prefixes) | set(mount_edges):
            effective(mid, frozenset())
        return resolved

    def _identifier_require_bindings(self, root: Node, source: bytes) -> dict[str, str]:
        """Maps `const routes = require('./routes/v1')` -> {'routes': './routes/v1'}."""
        bindings: dict[str, str] = {}
        for declarator in self._find_all(root, "variable_declarator"):
            name_node = declarator.child_by_field_name("name")
            value_node = declarator.child_by_field_name("value")
            if name_node is None or value_node is None or name_node.type != "identifier":
                continue
            required = self._require_path(value_node, source)
            if required is not None:
                name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace")
                bindings[name] = required
        return bindings

    def _find_all(self, node: Node, type_: str):
        if node.type == type_:
            yield node
        for child in node.children:
            yield from self._find_all(child, type_)

    @staticmethod
    def _string_literal(node: Node, source: bytes) -> str | None:
        if node.type != "string":
            return None
        fragment = next((c for c in node.named_children if c.type == "string_fragment"), None)
        if fragment is None:
            return ""
        return source[fragment.start_byte:fragment.end_byte].decode("utf-8", "replace")

    def _require_path(self, node: Node, source: bytes) -> str | None:
        """Returns the path string of a `require('...')` call node, else None. Also
        unwraps the immediately-invoked form `require('./routes')(app)`."""
        if node.type != "call_expression":
            return None
        func = node.child_by_field_name("function")
        if func is None:
            return None
        if func.type == "call_expression":  # require('./routes')(app)
            return self._require_path(func, source)
        if source[func.start_byte:func.end_byte] != b"require":
            return None
        args = node.child_by_field_name("arguments")
        if args is None or args.named_child_count == 0:
            return None
        return self._string_literal(args.named_children[0], source)

    @staticmethod
    def _resolve_single_import(
        import_path: str, current_file: Path, path_to_module_id: dict[Path, str]
    ) -> str | None:
        if not import_path.startswith("."):
            return None
        candidate = (current_file.parent / import_path).resolve()
        for suffix in ("", ".js", "/index.js", ".mjs", ".cjs"):
            probe = Path(str(candidate) + suffix)
            if probe in path_to_module_id:
                return path_to_module_id[probe]
        return None

    @staticmethod
    def _join_paths(*parts: str) -> str:
        joined = "/".join(p.strip("/") for p in parts if p and p.strip("/"))
        return "/" + joined if joined else "/"

    def _apply_mount_prefixes(
        self, routes_by_module: dict[str, list[ApiEndpoint]], mount_prefixes: dict[str, str]
    ) -> list[ApiEndpoint]:
        """Rewrites router-relative paths into external paths using the mount prefixes
        gathered across every file. Routes registered directly on `app` are already
        absolute and are left alone."""
        endpoints: list[ApiEndpoint] = []
        for module_id, routes in routes_by_module.items():
            prefix = mount_prefixes.get(module_id, "")
            for route in routes:
                if prefix and (route.framework_annotation or "").startswith("router."):
                    route.path = self._join_paths(prefix, route.path)
                endpoints.append(route)
        return endpoints

    def _find_config_files(self, repo_path: Path) -> list[ConfigFile]:
        configs = []
        candidates = {
            "package.json": ConfigFileType.BUILD,
            ".env": ConfigFileType.ENV,
            ".env.example": ConfigFileType.ENV,
            "Procfile": ConfigFileType.FRAMEWORK_CONFIG,
            "docker-compose.yml": ConfigFileType.FRAMEWORK_CONFIG,
            "Dockerfile": ConfigFileType.FRAMEWORK_CONFIG,
        }
        for filename, ctype in candidates.items():
            if (repo_path / filename).exists():
                configs.append(ConfigFile(path=filename, config_type=ctype))
        return configs
