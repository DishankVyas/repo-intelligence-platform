"""
NestJS parser: walks a repo's .ts files, uses tree-sitter to extract classes
(controllers/services/modules), methods, decorator-based routes (@Get/@Post/etc. on
a controller method, combined with the class's @Controller('prefix') path), ES
`import` statements, and package.json for external dependencies.

Establishes the same parser-schema contract as express_parser.py (Phase 1), but for
a genuinely class-based framework -- unlike Express, NestJS route handlers are
always named class methods (never inline), so there's no "inline handler with no
function id" case here the way there is for Express.

Heuristic, not a full semantic parser (matches the Phase 1 Express parser's stance):
tree-sitter finds decorator/class/method shapes, it does not resolve NestJS's
dependency-injection graph, understand `@Module()` metadata (controllers/providers
arrays), or decorator factories beyond a single string-literal argument.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import tree_sitter_typescript as tsts
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

TS_LANGUAGE = Language(tsts.language_typescript())

SKIP_DIRS = {"node_modules", ".git", "dist", "build", "coverage"}
# Test trees are not application architecture -- counting them as modules inflates the
# module set with files that don't describe the running service. Unlike SKIP_DIRS these
# are *recorded* in files_skipped rather than dropped silently, so the exclusion stays
# auditable (SKIP_DIRS would drown that list in node_modules noise).
TEST_DIRS = {"test", "tests", "__tests__", "__mocks__", "spec", "e2e", "cypress"}
SOURCE_EXTENSIONS = {".ts"}  # .tsx is a frontend concern -- NestJS backends don't use it
MAX_FILES = 3000

# NestJS route decorators -> HttpMethod. @All() matches any method, which HttpMethod
# has no member for -- mapped to UNKNOWN like other unresolved cases in this schema.
ROUTE_DECORATORS = {
    "Get": HttpMethod.GET,
    "Post": HttpMethod.POST,
    "Put": HttpMethod.PUT,
    "Patch": HttpMethod.PATCH,
    "Delete": HttpMethod.DELETE,
    "Options": HttpMethod.OPTIONS,
    "Head": HttpMethod.HEAD,
    "All": HttpMethod.UNKNOWN,
}

FUNCTION_DECL_QUERY = Query(
    TS_LANGUAGE,
    """
    (function_declaration name: (identifier) @name) @func
    """,
)


class NestJSParser(BaseParser):
    framework_name = "nestjs"

    def detect(self, repo_path: Path) -> bool:
        pkg_json = repo_path / "package.json"
        if not pkg_json.exists():
            return False
        try:
            data = json.loads(pkg_json.read_text())
        except (json.JSONDecodeError, OSError):
            return False
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        return "@nestjs/core" in deps or "@nestjs/common" in deps

    def parse(self, repo_path: Path) -> ParsedRepository:
        warnings: list[str] = []
        files_skipped: list[str] = []

        pkg_json = repo_path / "package.json"
        pkg_data = json.loads(pkg_json.read_text()) if pkg_json.exists() else {}
        framework_version = (
            pkg_data.get("dependencies", {}).get("@nestjs/core")
            or pkg_data.get("dependencies", {}).get("@nestjs/common")
            or pkg_data.get("devDependencies", {}).get("@nestjs/core")
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

        parser = Parser(TS_LANGUAGE)
        tsconfig_aliases = self._load_tsconfig_aliases(repo_path)
        # Keyed on the RESOLVED path: _resolve_imports() resolves its candidates, and on
        # macOS a clone under /var/folders resolves to /private/var/folders, so keying on
        # the unresolved path made every internal-import lookup miss silently.
        path_to_module_id: dict[Path, str] = {}
        for i, file_path in enumerate(source_files):
            path_to_module_id[file_path.resolve()] = f"mod_{i}"

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

            imports = self._extract_imports(root, source_bytes)
            resolved_import_ids = self._resolve_imports(
                imports, file_path, repo_path, path_to_module_id, tsconfig_aliases
            )
            for target_id in resolved_import_ids:
                internal_deps.append(
                    InternalDependencyEdge(from_module_id=module_id, to_module_id=target_id)
                )

            modules.append(ModuleNode(id=module_id, path=rel_path, imports=resolved_import_ids))

            file_classes, file_functions, file_endpoints = self._extract_classes(
                root, source_bytes, module_id
            )
            classes.extend(file_classes)
            functions.extend(file_functions)
            api_endpoints.extend(file_endpoints)

            functions.extend(self._extract_free_functions(root, source_bytes, module_id))

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
            detected_language="typescript",
            detected_framework="nestjs",
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
            database_entities=[],  # ORM entity detection (TypeORM etc.) not implemented
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
            if f.suffix not in SOURCE_EXTENSIONS:
                continue
            # Path.suffix only returns the last extension (".ts"), never ".spec.ts" --
            # checked against the full filename instead. Covers both Jest's default
            # unit-test convention (*.spec.ts) and NestJS's scaffolded e2e convention
            # (*.e2e-spec.ts, e.g. test/app.e2e-spec.ts in every `nest new` project) --
            # found missing by testing against the real nestjs/typescript-starter repo.
            if (
                f.name.endswith(".spec.ts")
                or f.name.endswith(".e2e-spec.ts")
                or any(part in TEST_DIRS for part in rel_parts)
            ):
                files_skipped.append(f.relative_to(repo_path).as_posix() + " (test file, skipped)")
                continue
            results.append(f)
        # Case-sensitive, part-by-part -- what PurePosixPath ordering does. Sorting Path
        # objects directly is case-INsensitive on Windows, which reorders modules (and
        # with them module ids, endpoints, and which facts survive the 40-per-category
        # cap) relative to a parse of the same commit on Linux or macOS.
        return sorted(results, key=lambda f: f.relative_to(repo_path).parts)

    def _extract_imports(self, root: Node, source: bytes) -> list[str]:
        """Returns the string source of every `import ... from '<source>'` --
        ES import syntax only, not CommonJS require() (that's Express's world) or
        re-exports (`export ... from`, a known gap)."""
        imports = []
        for node in self._find_all(root, "import_statement"):
            source_node = node.child_by_field_name("source")
            if source_node is None:
                continue
            fragment = next(
                (c for c in source_node.named_children if c.type == "string_fragment"), None
            )
            if fragment is not None:
                imports.append(source[fragment.start_byte:fragment.end_byte].decode("utf-8", "replace"))
        return imports

    def _resolve_imports(
        self,
        raw_imports: list[str],
        current_file: Path,
        repo_path: Path,
        path_to_module_id: dict[Path, str],
        tsconfig_aliases: "dict[str, list[str]] | None" = None,
    ) -> list[str]:
        """Resolves relative import paths ('./users.service') to module ids, and
        non-relative imports against tsconfig.json path aliases when one matches
        (e.g. '@app/users' -> 'src/users' per compilerOptions.paths) -- common in
        projects generated via the Nest CLI or laid out with a '@app/*'-style
        alias instead of long relative chains. A non-relative import that matches
        no alias is a real external package ('@nestjs/common') and is not
        resolved to a module id -- it shows up in `dependencies.external` via
        package.json instead."""
        resolved = []
        tsconfig_aliases = tsconfig_aliases or {}
        for imp in raw_imports:
            if imp.startswith("."):
                candidate = (current_file.parent / imp).resolve()
                for suffix in ("", ".ts", "/index.ts"):
                    probe = Path(str(candidate) + suffix)
                    if probe in path_to_module_id:
                        resolved.append(path_to_module_id[probe])
                        break
            else:
                target_id = self._resolve_ts_alias(imp, tsconfig_aliases, path_to_module_id)
                if target_id is not None:
                    resolved.append(target_id)
        return resolved

    def _load_tsconfig_aliases(self, repo_path: Path) -> dict[str, list[str]]:
        """Reads compilerOptions.paths (resolved against baseUrl) from the
        repo-root tsconfig.json, following a single 'extends' hop -- the common
        case of a project tsconfig extending a shared base config. Handles JSONC
        (comments, trailing commas), which is the Nest CLI's own default
        tsconfig.json format. Scoped to one repo-root config rather than a full
        monorepo-aware walk up the directory tree, consistent with this parser's
        existing dataset-selection criteria, which already excludes nested
        package.json monorepo structures (see the dataset construction docs).
        The tsconfig-walk-and-alias-match approach here was informed by the
        public resolution logic in Graphify (github.com/Graphify-Labs/graphify,
        Apache-2.0), reimplemented independently against this project's own
        data structures rather than ported."""
        data = self._read_jsonc(repo_path / "tsconfig.json")
        if data is None:
            return {}

        extends = data.get("extends")
        parent_options: dict = {}
        if isinstance(extends, str) and extends and not extends.startswith("@"):
            parent_path = (repo_path / extends).resolve()
            if not parent_path.suffix:
                parent_path = parent_path.with_suffix(".json")
            parent_data = self._read_jsonc(parent_path)
            if parent_data is not None:
                parent_options = parent_data.get("compilerOptions", {})

        compiler_options = {**parent_options, **data.get("compilerOptions", {})}
        base_url = (repo_path / (compiler_options.get("baseUrl") or ".")).resolve()

        aliases: dict[str, list[str]] = {}
        for alias, targets in compiler_options.get("paths", {}).items():
            candidates = [str(base_url / t) for t in targets if isinstance(t, str) and t]
            if candidates:
                aliases[alias] = candidates
        return aliases

    @staticmethod
    def _read_jsonc(path: Path) -> "dict | None":
        """Parses a JSON file, falling back to stripping comments first --
        tsconfig.json is JSONC by convention and plain json.loads rejects that.
        Returns None for a missing, unreadable, or unparseable file, so a
        malformed config degrades to "no aliases" rather than raising."""
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        for candidate in (raw, NestJSParser._strip_jsonc(raw)):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            return parsed if isinstance(parsed, dict) else None
        return None

    @staticmethod
    def _strip_jsonc(text: str) -> str:
        """Strips // line comments and /* */ block comments while leaving string
        contents (including a // or /* that happens to appear inside a string)
        untouched, then strips trailing commas before a closing } or ] -- the
        Nest CLI's scaffolded tsconfig.json routinely has both, and plain
        json.loads rejects either on its own."""
        pattern = re.compile(r'"(?:\\.|[^"\\])*"' r"|/\*.*?\*/" r"|//[^\n]*", re.DOTALL)
        without_comments = pattern.sub(
            lambda m: m.group(0) if m.group(0).startswith('"') else "", text
        )
        return re.sub(r",(\s*[}\]])", r"\1", without_comments)

    @staticmethod
    def _match_alias(raw: str, pattern: str) -> "tuple[int, str] | None":
        """Returns (specificity, captured-wildcard-text) if pattern matches raw,
        else None. Exact aliases always outrank wildcard ones; among wildcards,
        the longer prefix wins, matching TypeScript's own longest-prefix rule."""
        if "*" in pattern:
            if pattern.count("*") != 1:
                return None
            prefix, suffix = pattern.split("*", 1)
            if not raw.startswith(prefix) or not raw.endswith(suffix):
                return None
            end = len(raw) - len(suffix) if suffix else len(raw)
            if end < len(prefix):
                return None
            return len(prefix), raw[len(prefix):end]
        if raw == pattern:
            return len(pattern) + 1, ""  # +1: an exact match always beats any wildcard
        return None

    def _resolve_ts_alias(
        self, raw: str, aliases: dict[str, list[str]], path_to_module_id: dict[Path, str]
    ) -> "str | None":
        """Resolves a non-relative import against the most specific matching
        tsconfig alias pattern, trying each declared target in order (tsc itself
        tries each until one exists on disk) and returning the first that
        resolves to a known module."""
        best: "tuple[int, str, list[str]] | None" = None
        for pattern, targets in aliases.items():
            match = self._match_alias(raw, pattern)
            if match is None:
                continue
            specificity, captured = match
            if best is None or specificity > best[0]:
                best = (specificity, captured, targets)
        if best is None:
            return None
        _, captured, targets = best
        for target in targets:
            candidate = Path(target.replace("*", captured, 1) if "*" in target else target)
            for suffix in ("", ".ts", "/index.ts"):
                probe = Path(str(candidate) + suffix)
                if probe in path_to_module_id:
                    return path_to_module_id[probe]
        return None

    def _extract_classes(
        self, root: Node, source: bytes, module_id: str
    ) -> tuple[list[ClassNode], list[FunctionNode], list[ApiEndpoint]]:
        classes: list[ClassNode] = []
        functions: list[FunctionNode] = []
        endpoints: list[ApiEndpoint] = []

        for i, cls_node in enumerate(self._find_all(root, "class_declaration")):
            name_node = cls_node.child_by_field_name("name")
            if name_node is None:
                continue
            class_id = f"{module_id}_cls_{i}"
            name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace")

            decorators = self._preceding_decorators(cls_node)
            controller_prefix = self._decorator_string_arg(decorators, "Controller", source)

            interfaces = self._implemented_interfaces(cls_node, source)

            classes.append(
                ClassNode(
                    id=class_id,
                    name=name,
                    module_id=module_id,
                    interfaces_implemented=interfaces,
                )
            )

            body = cls_node.child_by_field_name("body")
            if body is None:
                continue

            pending_decorators: list[Node] = []
            method_index = 0
            for child in body.named_children:
                if child.type == "decorator":
                    pending_decorators.append(child)
                    continue
                if child.type == "method_definition":
                    method_name_node = child.child_by_field_name("name")
                    if method_name_node is None:
                        pending_decorators = []
                        continue
                    method_name = source[
                        method_name_node.start_byte:method_name_node.end_byte
                    ].decode("utf-8", "replace")
                    if method_name == "constructor":
                        pending_decorators = []
                        continue

                    func_id = f"{class_id}_fn_{method_index}"
                    method_index += 1
                    route_method, route_path, route_decorator_name = self._route_decorator(
                        pending_decorators, source
                    )
                    # A route decorator only creates a real, routable endpoint if its
                    # class also has @Controller -- without that, NestJS wouldn't wire
                    # it up either, so is_api_handler tracks endpoint creation exactly
                    # rather than "has a route decorator" alone.
                    creates_endpoint = route_method is not None and controller_prefix is not None
                    functions.append(
                        FunctionNode(
                            id=func_id,
                            name=method_name,
                            module_id=module_id,
                            class_id=class_id,
                            is_api_handler=creates_endpoint,
                        )
                    )
                    if creates_endpoint:
                        endpoints.append(
                            ApiEndpoint(
                                id=f"{func_id}_ep",
                                method=route_method,
                                path=self._join_route_path(controller_prefix, route_path),
                                handler_function_id=func_id,
                                framework_annotation=f"@{route_decorator_name}",
                            )
                        )
                    pending_decorators = []
                else:
                    pending_decorators = []

        return classes, functions, endpoints

    def _preceding_decorators(self, node: Node) -> list[Node]:
        """Class/export decorators are NOT the immediate preceding sibling -- for
        `export class Foo`, the parent's children are [decorator*, export, class_declaration],
        so `export` (and `default`, for default exports) sit between the decorators
        and the class. Walk backward past those keywords to find them."""
        parent = node.parent
        if parent is None:
            return []
        siblings = parent.children
        idx = siblings.index(node)
        decorators = []
        idx -= 1
        while idx >= 0 and siblings[idx].type in ("export", "default"):
            idx -= 1
        while idx >= 0 and siblings[idx].type == "decorator":
            decorators.append(siblings[idx])
            idx -= 1
        decorators.reverse()
        return decorators

    def _decorator_call(self, decorator: Node) -> Node | None:
        for child in decorator.named_children:
            if child.type == "call_expression":
                return child
        return None

    def _decorator_name(self, decorator: Node, source: bytes) -> str | None:
        call = self._decorator_call(decorator)
        if call is None:
            return None
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "identifier":
            return None
        return source[fn.start_byte:fn.end_byte].decode("utf-8", "replace")

    def _decorator_string_arg(
        self, decorators: list[Node], name: str, source: bytes
    ) -> str | None:
        """Returns the route prefix declared by the named decorator, or None if the
        decorator isn't present at all. Three argument forms are handled, all of
        which occur in real NestJS code:

            @Controller('users')                       -> 'users'
            @Controller({ path: 'users', version: '1' }) -> 'users'
            @Controller(['v1/users', 'v2/users'])      -> 'v1/users'  (first entry)

        Anything else (no args, computed values) resolves to ''. The array form
        deliberately keeps only the first path: ApiEndpoint carries a single path,
        and the alternatives are the same handler under another prefix."""
        for dec in decorators:
            if self._decorator_name(dec, source) != name:
                continue
            call = self._decorator_call(dec)
            args = call.child_by_field_name("arguments") if call else None
            if args is None:
                return ""
            for arg in args.named_children:
                if arg.type == "string":
                    text = self._string_text(arg, source)
                    if text is not None:
                        return text
                if arg.type == "object":
                    # options form: pull the `path` property, ignore version/host/etc.
                    for pair in arg.named_children:
                        if pair.type != "pair":
                            continue
                        key = pair.child_by_field_name("key")
                        value = pair.child_by_field_name("value")
                        if key is None or value is None:
                            continue
                        key_text = source[key.start_byte:key.end_byte].decode(
                            "utf-8", "replace"
                        ).strip("'\"")
                        if key_text != "path":
                            continue
                        if value.type == "string":
                            text = self._string_text(value, source)
                            if text is not None:
                                return text
                        elif value.type == "array":
                            for element in value.named_children:
                                text = self._string_text(element, source)
                                if text is not None:
                                    return text
                if arg.type == "array":
                    for element in arg.named_children:
                        text = self._string_text(element, source)
                        if text is not None:
                            return text
            return ""
        return None

    @staticmethod
    def _string_text(node: Node, source: bytes) -> str | None:
        """Unwraps a tree-sitter `string` node to its literal text, or None if the
        node isn't a plain string literal (e.g. a template string with substitutions)."""
        if node.type != "string":
            return None
        fragment = next(
            (c for c in node.named_children if c.type == "string_fragment"), None
        )
        if fragment is None:
            return ""  # empty string literal
        return source[fragment.start_byte:fragment.end_byte].decode("utf-8", "replace")

    def _route_decorator(
        self, decorators: list[Node], source: bytes
    ) -> tuple[HttpMethod | None, str, str]:
        for dec in decorators:
            name = self._decorator_name(dec, source)
            if name in ROUTE_DECORATORS:
                path = self._decorator_string_arg([dec], name, source) or ""
                return ROUTE_DECORATORS[name], path, name
        return None, "", ""

    @staticmethod
    def _join_route_path(prefix: str, path: str) -> str:
        prefix = prefix.strip("/")
        path = path.strip("/")
        joined = "/".join(p for p in (prefix, path) if p)
        return "/" + joined

    def _implemented_interfaces(self, cls_node: Node, source: bytes) -> list[str]:
        """Returns interface NAMES (not resolved ids -- interface_declarations
        aren't extracted as ClassNodes by this parser, see module docstring), so
        this is best-effort/informational rather than a resolvable graph edge like
        Express's class hierarchy would be."""
        names = []
        for heritage in cls_node.named_children:
            if heritage.type != "class_heritage":
                continue
            for clause in heritage.named_children:
                if clause.type != "implements_clause":
                    continue
                for type_id in clause.named_children:
                    if type_id.type == "type_identifier":
                        names.append(source[type_id.start_byte:type_id.end_byte].decode("utf-8", "replace"))
        return names

    def _extract_free_functions(
        self, root: Node, source: bytes, module_id: str
    ) -> list[FunctionNode]:
        """Top-level `function foo() {}` declarations -- rare in idiomatic NestJS
        (almost everything is a class method) but supported for parity with the
        Express parser's function extraction."""
        functions = []
        cursor = QueryCursor(FUNCTION_DECL_QUERY)
        matches = cursor.captures(root)
        for i, name_node in enumerate(matches.get("name", [])):
            name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace")
            functions.append(FunctionNode(id=f"{module_id}_freefn_{i}", name=name, module_id=module_id))
        return functions

    def _find_all(self, node: Node, type_: str):
        if node.type == type_:
            yield node
        for child in node.children:
            yield from self._find_all(child, type_)

    def _find_config_files(self, repo_path: Path) -> list[ConfigFile]:
        configs = []
        candidates = {
            "package.json": ConfigFileType.BUILD,
            "tsconfig.json": ConfigFileType.BUILD,
            "nest-cli.json": ConfigFileType.FRAMEWORK_CONFIG,
            ".env": ConfigFileType.ENV,
            ".env.example": ConfigFileType.ENV,
            "docker-compose.yml": ConfigFileType.FRAMEWORK_CONFIG,
            "Dockerfile": ConfigFileType.FRAMEWORK_CONFIG,
        }
        for filename, ctype in candidates.items():
            if (repo_path / filename).exists():
                configs.append(ConfigFile(path=filename, config_type=ctype))
        return configs
