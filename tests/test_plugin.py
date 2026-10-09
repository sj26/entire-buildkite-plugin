"""Black-box hook and Git remote-helper tests. Only fake credentials are used."""

import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
URL = "entire://region.entire.io/et/project/repository"
TOKEN = "eyJhbGciOiJFUzI1NiJ9.eyJhdWQiOiJodHRwczovL2NvcmUuZW50aXJlLmlvIn0.fake_signature"
AGENT_TOKEN = "agent-session-secret-sentinel"
REF = "0123456789abcdef0123456789abcdef01234567"


class PluginTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.home = self.directory / "home"
        self.home.mkdir()
        self.env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "TMPDIR": str(self.directory),
            "BUILDKITE_ENTIRE_CLONE_URL": URL,
            "BUILDKITE_REPO": "https://region.entire.io/et/project/repository",
            "BUILDKITE_JOB_ID": "job-123",
            "BUILDKITE_AGENT_ACCESS_TOKEN": AGENT_TOKEN,
        }

    def script(self, path, source):
        path.write_text(source)
        path.chmod(0o755)

    def run_command(self, *args, **kwargs):
        return subprocess.run(args, env=self.env, cwd=self.directory,
                              text=True, capture_output=True,
                              timeout=kwargs.pop("timeout", 30), **kwargs)

    def assert_secret_free(self, result):
        for secret in (TOKEN, AGENT_TOKEN):
            self.assertNotIn(secret, result.stdout + result.stderr)
            for path in self.home.rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes())

    def prepare_helper(self):
        shutil.copy(ROOT / "lib/git-remote-entire", self.bin)
        libexec = self.directory / "libexec"
        libexec.mkdir()
        self.script(libexec / "git-remote-entire", f"""#!/usr/bin/env python3
import json, os, sys
assert os.environ['ENTIRE_TOKEN'] == {TOKEN!r}
assert 'ENTIRE_DEBUG' not in os.environ
assert 'ENTIRE_TLS_SKIP_VERIFY' not in os.environ
assert 'GIT_TRACE2_ENV_VARS' not in os.environ
with open(os.environ['HOME'] + '/helper-argv.json', 'w') as f:
    json.dump(sys.argv[1:], f)
for line in sys.stdin:
    if line.strip() == 'capabilities':
        print('option\\n', flush=True)
    elif line.startswith('option '):
        print('unsupported', flush=True)
    elif line.strip() == 'list':
        print('{REF} refs/heads/main\\n', flush=True)
    elif not line.strip():
        break
    else:
        raise SystemExit('unexpected Git protocol request')
""")
        # Observe argv without replacing curl's HTTP or config parsing behavior.
        curl = shutil.which("curl")
        self.script(self.bin / "curl", f"""#!/usr/bin/env bash
printf '%s\\n' "$@" > "$HOME/curl-argv"
exec {curl} "$@"
""")
        self.requests = []
        self.response = (200, json.dumps({"token": TOKEN, "username": "x-access-token"}))
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append((self.path, self.headers["Authorization"], json.loads(body)))
                status, body = owner.response
                self.send_response(status)
                self.send_header("Location", owner.env["BUILDKITE_AGENT_ENDPOINT"] + "/redirected")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.env["BUILDKITE_AGENT_ENDPOINT"] = f"http://127.0.0.1:{server.server_port}/v3"

    def test_git_uses_job_credentials_without_persisting_or_tracing_secrets(self):
        self.prepare_helper()
        self.env.update(ENTIRE_TOKEN="personal-token-must-not-be-used", ENTIRE_DEBUG="1",
                        ENTIRE_TLS_SKIP_VERIFY="true", GIT_TRACE2_ENV_VARS="ENTIRE_TOKEN")
        for _ in range(2):  # Each Git invocation obtains fresh job credentials.
            result = self.run_command("git", "ls-remote", URL)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), f"{REF}\trefs/heads/main")
            self.assert_secret_free(result)
        self.assertEqual(self.requests, [
            ("/v3/jobs/job-123/repository_access_token", f"Token {AGENT_TOKEN}", {"repo_url": URL})
        ] * 2)
        self.assertEqual(json.loads((self.home / "helper-argv.json").read_text()), [URL, URL])
        result = self.run_command("bash", "-x", str(self.bin / "git-remote-entire"), "origin", URL,
                                  input="capabilities\n\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_secret_free(result)
        self.assertEqual(self.env["ENTIRE_TOKEN"], "personal-token-must-not-be-used")

    def test_api_failures_and_redirects_do_not_leak_bodies_or_fall_back_to_login(self):
        self.prepare_helper()
        for status, body in [(400, TOKEN), (401, TOKEN), (500, TOKEN), (307, TOKEN),
                             (200, "not-json " + TOKEN), (200, '{"token":""}'),
                             (200, '{"token":"not a JWT"}')]:
            with self.subTest(status=status, body_type=body[:8]):
                self.response = status, body
                result = self.run_command("bash", "-x", str(self.bin / "git-remote-entire"), "origin", URL)
                self.assertNotEqual(result.returncode, 0)
                self.assert_secret_free(result)
                self.assertFalse((self.home / "helper-argv.json").exists())
                if status != 200:
                    self.assertIn(f"HTTP {status}", result.stderr)
        self.assertEqual(len(self.requests), 7)  # In particular, no 307 follow-up.

    def test_other_remotes_and_missing_job_credentials_fail_before_request(self):
        self.prepare_helper()
        result = self.run_command(str(self.bin / "git-remote-entire"), "origin", URL + "-other")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)
        del self.env["BUILDKITE_AGENT_ACCESS_TOKEN"]
        result = self.run_command(str(self.bin / "git-remote-entire"), "origin", URL)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("credentials are missing", result.stderr)
        self.assertEqual(self.requests, [])

    def test_missing_or_unsafe_authoritative_url_fails_before_install(self):
        for url in ("", "https://region.entire.io/et/p/r", "entire://entire.io.evil.test/et/p/r",
                    "entire://token@entire.io/et/p/r", "entire://entire.io/et/p/r?token=secret"):
            with self.subTest(url=url):
                self.env["BUILDKITE_ENTIRE_CLONE_URL"] = url
                result = self.run_command("bash", str(ROOT / "hooks/pre-checkout"))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("missing or invalid", result.stderr)
                self.assertEqual(list(self.directory.glob("buildkite-entire.*")), [])

    def test_unsupported_platform_fails_without_installing(self):
        self.script(self.bin / "uname", "#!/bin/sh\necho Darwin\n")
        result = self.run_command(str(ROOT / "lib/install"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Linux x86_64 only", result.stderr)
        self.assertEqual(list(self.directory.glob("buildkite-entire.*")), [])

    def test_corrupt_release_is_rejected_and_temporary_files_removed(self):
        self.script(self.bin / "curl", '#!/usr/bin/env bash\nprintf corrupt > "${@: -1}"\n')
        result = self.run_command(str(ROOT / "lib/install"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertEqual(list(self.directory.glob("buildkite-entire.*")), [])

    def test_verified_release_installs_and_hook_preserves_agent_checkout(self):
        # Use the real, pinned upstream archive. Set this env var to a previously
        # downloaded archive to run offline; the plugin still checks its digest.
        archive = os.environ.get("ENTIRE_TEST_ARCHIVE")
        if archive:
            self.env["TEST_ARCHIVE"] = str(Path(archive).resolve())
            self.script(self.bin / "curl", '#!/usr/bin/env bash\ncp "$TEST_ARCHIVE" "${@: -1}"\n')
        result = self.run_command("bash", "-c", '''
set -euo pipefail
source "$1/hooks/pre-checkout"
[[ "$BUILDKITE_REPO" == "$BUILDKITE_ENTIRE_CLONE_URL" ]]
[[ -z ${ENTIRE_TOKEN+x} ]]
[[ -x "$BUILDKITE_ENTIRE_INSTALL_DIR/libexec/git-remote-entire" ]]
[[ $(stat -c %a "$BUILDKITE_ENTIRE_INSTALL_DIR") == 700 ]]
entire version
directory="$BUILDKITE_ENTIRE_INSTALL_DIR"
source "$1/hooks/pre-exit"
[[ ! -e "$directory" ]]
''', "test", str(ROOT), timeout=600)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Entire CLI 0.11.4", result.stdout)
        self.assertIn("OS/Arch: linux/amd64", result.stdout)
        self.assert_secret_free(result)
        self.assertEqual(list(self.directory.glob("buildkite-entire.*")), [])


if __name__ == "__main__":
    unittest.main()
