"""TypeScript language support conformance tests.

These tests verify that the TypeScript-specific components work together:
parser -> normaliser -> F2P computation, test-file classification, test
command construction, Docker image generation, and Harbor emission.
"""

import json
import subprocess
import textwrap

from swebenchify.backends import get_backend
from swebenchify.models import EnvironmentSpec, compute_typescript_env_spec_hash


def _ts_spec(**overrides) -> EnvironmentSpec:
    fields = dict(
        language="typescript",
        language_version="20",
        package_manager="npm",
        install_cmd="npm ci || npm install",
        test_cmd="npx vitest run",
    )
    fields.update(overrides)
    spec = EnvironmentSpec(**fields)
    spec.env_spec_hash = compute_typescript_env_spec_hash(spec)
    return spec


class TestTypeScriptBackendRegistration:
    def test_backend_registered(self):
        backend = get_backend("typescript")
        assert backend is not None
        assert backend.name == "typescript"
        assert backend.test_file_pattern == ".ts"
        assert callable(backend.parser.parse)

    def test_parser_registered(self):
        from swebenchify.parsers import get_parser
        assert get_parser("typescript") is not None


class TestTypeScriptTestCommand:
    def test_vitest_default(self):
        cmd = get_backend("typescript").make_test_cmd(_ts_spec())
        assert "--outputFile=/tmp/swebenchify-ts-report.json" in cmd
        assert "CI=1" in cmd
        # Ordering is the contract: stale report removed BEFORE the run,
        # scope args forwarded to the runner, report cat'ed AFTER.
        assert (
            cmd.index("rm -f /tmp/swebenchify-ts-report.json")
            < cmd.index("vitest run")
            < cmd.index("--reporter=json")
            < cmd.index('"$@"')
            < cmd.index("cat /tmp/swebenchify-ts-report.json")
        )

    def test_bare_vitest_normalized_to_run_mode(self):
        """Bare `vitest` would enter watch mode and hang the container."""
        cmd = get_backend("typescript").make_test_cmd(_ts_spec(test_cmd="npx vitest"))
        assert "vitest run" in cmd

    def test_jest_uses_json_flag(self):
        cmd = get_backend("typescript").make_test_cmd(_ts_spec(test_cmd="npx jest"))
        assert "--json" in cmd
        assert "--reporter=json" not in cmd

    def test_npm_test_forwards_flags(self):
        """Report flags must land AFTER the npm `--` separator."""
        cmd = get_backend("typescript").make_test_cmd(_ts_spec(test_cmd="npm test"))
        assert "npm test -- --json" in cmd


class TestTypeScriptSharedNormalization:
    """normalize_ts_runner_cmd / ts_report_command are shared by the F2P
    wrapper, the Harbor emitter, and the eval harness."""

    def test_vitest_run_idempotent(self):
        from swebenchify.backends import normalize_ts_runner_cmd
        assert normalize_ts_runner_cmd("npx vitest run") == "npx vitest run"

    def test_bare_vitest(self):
        from swebenchify.backends import normalize_ts_runner_cmd
        assert normalize_ts_runner_cmd("npx vitest") == "npx vitest run"

    def test_npm_run_script(self):
        from swebenchify.backends import normalize_ts_runner_cmd
        assert normalize_ts_runner_cmd("npm run test:unit") == "npm run test:unit --"

    def test_npm_with_existing_separator(self):
        from swebenchify.backends import normalize_ts_runner_cmd
        assert normalize_ts_runner_cmd("npm test --") == "npm test --"

    def test_report_command_flags_by_runner(self):
        from swebenchify.backends import ts_report_command
        assert "--reporter=json" in ts_report_command("npx vitest run")
        assert "--json" in ts_report_command("npx jest")
        # npm-style: flags after the separator
        assert "npm test -- --json" in ts_report_command("npm test")

    def test_eval_harness_single_test_cmd(self):
        from swebenchify.eval_harness import _ts_single_test_cmd
        cmd = _ts_single_test_cmd("npx vitest", "src/a.test.ts::adds (edge) case")
        assert "vitest run" in cmd
        assert "CI=1" in cmd
        assert "src/a.test.ts" in cmd
        assert "-t" in cmd
        # regex metacharacters in the name are escaped
        assert "\\(edge\\)" in cmd

    def test_failure_grep_matches_report(self):
        """The failure grep is a BRE evaluated by grep in the run script."""
        backend = get_backend("typescript")
        matching = [
            '{"numFailedTests":3,"success":false}',
            '{"numFailedTests": 12}',
            '{"numFailedTestSuites":1,"numFailedTests":0}',
        ]
        non_matching = [
            '{"numFailedTests":0,"numFailedTestSuites":0,"success":true}',
        ]
        for sample in matching:
            proc = subprocess.run(
                ["grep", "-q", backend.failure_grep],
                input=sample, text=True,
            )
            assert proc.returncode == 0, f"failure_grep should match: {sample}"
        for sample in non_matching:
            proc = subprocess.run(
                ["grep", "-q", backend.failure_grep],
                input=sample, text=True,
            )
            assert proc.returncode != 0, f"failure_grep should NOT match: {sample}"


class TestTypeScriptTestScope:
    DIFF = textwrap.dedent("""\
        diff --git a/src/util.ts b/src/util.ts
        --- a/src/util.ts
        +++ b/src/util.ts
        diff --git a/src/__tests__/util.test.ts b/src/__tests__/util.test.ts
        --- a/src/__tests__/util.test.ts
        +++ b/src/__tests__/util.test.ts
        diff --git a/tests/cli.spec.tsx b/tests/cli.spec.tsx
        --- a/tests/cli.spec.tsx
        +++ b/tests/cli.spec.tsx
        diff --git a/src/__tests__/fixture.json b/src/__tests__/fixture.json
        --- a/src/__tests__/fixture.json
        +++ b/src/__tests__/fixture.json
        diff --git a/README.md b/README.md
        --- a/README.md
        +++ b/README.md
    """)

    def test_scope_selects_runner_visible_files_only(self):
        scope = get_backend("typescript").test_scope(self.DIFF)
        files = scope.split()
        assert "src/__tests__/util.test.ts" in files
        assert "tests/cli.spec.tsx" in files
        # source file, JSON fixture, and docs are not test entry points
        assert "src/util.ts" not in files
        assert "src/__tests__/fixture.json" not in files
        assert "README.md" not in files

    def test_tests_dir_plain_source_included(self):
        """A plain .ts file under __tests__/ is picked up (jest convention)."""
        diff = (
            "diff --git a/__tests__/helpers.ts b/__tests__/helpers.ts\n"
            "--- a/__tests__/helpers.ts\n"
            "+++ b/__tests__/helpers.ts\n"
        )
        assert "__tests__/helpers.ts" in get_backend("typescript").test_scope(diff)

    def test_empty_when_no_test_files(self):
        diff = (
            "diff --git a/src/util.ts b/src/util.ts\n"
            "--- a/src/util.ts\n"
            "+++ b/src/util.ts\n"
        )
        assert get_backend("typescript").test_scope(diff) == ""


class TestTypeScriptFileClassification:
    def test_tests_dunder_dir_is_test(self):
        from swebenchify.extractor import is_test_file
        assert is_test_file("src/__tests__/utils.ts") is True

    def test_dot_test_file_is_test(self):
        from swebenchify.extractor import is_test_file
        assert is_test_file("src/components/Button.test.tsx") is True

    def test_source_file_is_not_test(self):
        from swebenchify.extractor import is_test_file
        assert is_test_file("src/components/Button.tsx") is False


class TestTypeScriptDockerfile:
    def test_minimal_spec(self):
        dockerfile = get_backend("typescript").make_dockerfile(
            "owner/repo", "abc123", _ts_spec())
        assert "FROM node:20-slim" in dockerfile
        assert "git" in dockerfile
        assert "corepack enable" in dockerfile
        assert "( npm ci || npm install )" in dockerfile
        assert "COPY test.patch /patches/test.patch" in dockerfile
        assert "COPY gold.patch /patches/gold.patch" in dockerfile

    def test_version_from_spec(self):
        dockerfile = get_backend("typescript").make_dockerfile(
            "owner/repo", "abc123", _ts_spec(language_version="22"))
        assert "FROM node:22-slim" in dockerfile

    def test_base_image_override(self):
        dockerfile = get_backend("typescript").make_dockerfile(
            "owner/repo", "abc123", _ts_spec(base_image="node:20-bookworm"))
        assert "FROM node:20-bookworm" in dockerfile

    def test_system_dependencies(self):
        dockerfile = get_backend("typescript").make_dockerfile(
            "owner/repo", "abc123",
            _ts_spec(system_dependencies=["python3", "make", "g++"]))
        assert "python3" in dockerfile
        assert "g++" in dockerfile

    def test_repo_tarball(self):
        dockerfile = get_backend("typescript").make_dockerfile(
            "owner/repo", "abc123", _ts_spec(), repo_tarball=True)
        assert "COPY repo.tar.gz" in dockerfile


class TestTypeScriptF2pPipeline:
    """Full pipeline: two-phase output -> parser -> normalize -> F2P."""

    def test_pre_fail_post_pass(self):
        from swebenchify.grader import _parse_f2p_output_generic
        from swebenchify.parsers import (
            TypeScriptJestJSONParser,
            normalize_typescript_f2p,
        )

        pre = json.dumps({
            "numFailedTests": 1, "numFailedTestSuites": 0, "success": False,
            "testResults": [{
                "name": "/repo/src/a.test.ts",
                "assertionResults": [
                    {"fullName": "a fixes the bug", "status": "failed"},
                    {"fullName": "a stays stable", "status": "passed"},
                ],
            }],
        })
        post = json.dumps({
            "numFailedTests": 0, "numFailedTestSuites": 0, "success": True,
            "testResults": [{
                "name": "/repo/src/a.test.ts",
                "assertionResults": [
                    {"fullName": "a fixes the bug", "status": "passed"},
                    {"fullName": "a stays stable", "status": "passed"},
                ],
            }],
        })
        raw = (
            "===SWEBENCHIFY_PHASE_SEPARATOR===_RUN_1_PRE\n"
            f"{pre}\n"
            "===SWEBENCHIFY_PHASE_SEPARATOR===_RUN_1_POST\n"
            f"{post}\n"
        )
        result = _parse_f2p_output_generic(
            raw, TypeScriptJestJSONParser(), normalize_typescript_f2p, n_runs=1)
        assert result.status == "valid"
        assert result.FAIL_TO_PASS == ["src/a.test.ts::a fixes the bug"]
        assert result.PASS_TO_PASS == ["src/a.test.ts::a stays stable"]


class TestTypeScriptHarborEmission:
    def _instance(self):
        from swebenchify.models import TaskInstance
        return TaskInstance(
            repo="owner/repo",
            instance_id="owner__repo-123",
            base_commit="a" * 40,
            patch="diff --git a/src/util.ts b/src/util.ts\n",
            test_patch=(
                "diff --git a/src/util.test.ts b/src/util.test.ts\n"
                "--- a/src/util.test.ts\n"
                "+++ b/src/util.test.ts\n"
            ),
            problem_statement="Fix the util bug",
            hints_text="",
            created_at="2026-01-01T00:00:00Z",
            version="20-abcd1234",
            FAIL_TO_PASS=json.dumps(["src/util.test.ts::util works"]),
            PASS_TO_PASS=json.dumps([]),
            repo_language="typescript",
            env_spec_hash="abcd1234" * 8,
        )

    def test_task_generation(self, tmp_path):
        from swebenchify.harbor_emitter import HarborTaskGenerator

        instance = self._instance()
        gen = HarborTaskGenerator(
            [instance], env_specs={instance.instance_id: _ts_spec()})
        generated = gen.generate_all(tmp_path)
        assert generated == [instance.instance_id]

        task_dir = tmp_path / instance.instance_id
        dockerfile = (task_dir / "environment" / "Dockerfile").read_text()
        assert "node:20-slim" in dockerfile

        test_sh = (task_dir / "tests" / "test.sh").read_text()
        # graded with node (node:slim images have no python3)
        assert "node -e" in test_sh
        assert "python3 -c" not in test_sh
        assert "--reporter=json" in test_sh
        assert "--outputFile=/tmp/swebenchify-ts-report.json" in test_sh
        assert "src/util.test.ts" in test_sh  # scope from the test patch
        # string.Template left no unexpanded harness variables behind
        assert "$test_command" not in test_sh
        assert "$$" not in test_sh

        config = json.loads((task_dir / "tests" / "config.json").read_text())
        assert config["repo_language"] == "typescript"

    def test_npm_style_test_cmd_forwards_flags(self, tmp_path):
        """Harbor and validation must agree on npm `--` flag forwarding."""
        from swebenchify.harbor_emitter import HarborTaskGenerator

        instance = self._instance()
        gen = HarborTaskGenerator(
            [instance],
            env_specs={instance.instance_id: _ts_spec(test_cmd="npm test")})
        gen.generate_all(tmp_path)
        test_sh = (tmp_path / instance.instance_id / "tests" / "test.sh").read_text()
        assert "npm test -- --json" in test_sh

    def test_bare_vitest_cmd_normalized(self, tmp_path):
        from swebenchify.harbor_emitter import HarborTaskGenerator

        instance = self._instance()
        gen = HarborTaskGenerator(
            [instance],
            env_specs={instance.instance_id: _ts_spec(test_cmd="npx vitest")})
        gen.generate_all(tmp_path)
        test_sh = (tmp_path / instance.instance_id / "tests" / "test.sh").read_text()
        assert "vitest run" in test_sh


class TestTypeScriptEnvDetection:
    """Precedence contract of _detect_typescript in discover_and_validate."""

    @staticmethod
    def _detect(root):
        import importlib.util
        from pathlib import Path
        script = (Path(__file__).parent.parent / "scripts"
                  / "discover_and_validate.py")
        spec = importlib.util.spec_from_file_location("dav_ts_test", script)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod._detect_typescript("owner/repo", root)

    def test_package_manager_field_beats_lockfile(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps(
            {"packageManager": "pnpm@9.0.0"}))
        (tmp_path / "yarn.lock").write_text("")
        assert self._detect(tmp_path)["package_manager"] == "pnpm"

    def test_lockfile_fallback(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "package-lock.json").write_text("{}")
        assert self._detect(tmp_path)["package_manager"] == "npm"

    def test_engines_beats_nvmrc(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps(
            {"engines": {"node": ">=22"}}))
        (tmp_path / ".nvmrc").write_text("18\n")
        assert self._detect(tmp_path)["node_version"] == "22"

    def test_nvmrc_fallback(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / ".nvmrc").write_text("18\n")
        assert self._detect(tmp_path)["node_version"] == "18"

    def test_vitest_beats_jest(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps(
            {"devDependencies": {"jest": "^29.0.0", "vitest": "^2.0.0"}}))
        assert self._detect(tmp_path)["test_runner"] == "vitest"

    def test_jest_detected(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps(
            {"devDependencies": {"jest": "^29.0.0"}}))
        assert self._detect(tmp_path)["test_runner"] == "jest"
