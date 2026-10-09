"""Exercise the hook with real curl and Git, a local API, and fake credentials."""

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
TOKEN = "job-token-sentinel"
AGENT_TOKEN = "agent-session-secret-sentinel"
REF = "0123456789abcdef0123456789abcdef01234567"


class PluginTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.home = self.directory / "home"
        self.home.mkdir()
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        # Keep host Entire installations out of these tests.
        for name in ("bash", "curl", "git", "jq", "mkdir", "cp", "dirname",
                     "uname", "mktemp", "sha256sum", "tar", "gzip", "rm"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.env = {
            "PATH": str(self.bin), "HOME": str(self.home), "TMPDIR": str(self.directory),
            "BUILDKITE_REPO": URL, "BUILDKITE_JOB_ID": "job-123",
            "BUILDKITE_AGENT_ACCESS_TOKEN": AGENT_TOKEN,
            "ENTIRE_TOKEN": "personal-token-must-be-replaced",
            "EXPECTED_TOKEN": TOKEN,
        }
        self.script(self.bin / "entire", "#!/bin/sh\nexit 0\n")
        self.script(self.bin / "git-remote-entire", f"""#!/bin/sh
[ "$ENTIRE_TOKEN" = "$EXPECTED_TOKEN" ] || exit 1
while read -r line; do
  case "$line" in
    capabilities) printf '\\n' ;;
    list) printf '{REF} refs/heads/main\\n\\n' ;;
    '') exit 0 ;;
    *) exit 1 ;;
  esac
done
""")
        self.requests = []
        self.response = (200, json.dumps({"token": TOKEN}))
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
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.env["BUILDKITE_AGENT_ENDPOINT"] = f"http://127.0.0.1:{server.server_port}/v3"

    def script(self, path, source):
        path.write_text(source)
        path.chmod(0o755)

    def hook(self, command='git ls-remote "$BUILDKITE_REPO"'):
        return subprocess.run([
            shutil.which("bash"), "-x", "-c", '''
source "$1/hooks/pre-checkout"
[[ "$BUILDKITE_REPO" == "$2" ]]
eval "$3"
''', "test", str(ROOT), URL,
            command,
        ], env=self.env, cwd=self.directory, text=True, capture_output=True, timeout=240)

    def assert_secret_free(self, result):
        for secret in (TOKEN, AGENT_TOKEN):
            self.assertNotIn(secret, result.stdout + result.stderr)
            for path in self.home.rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes())

    def test_existing_install_and_standard_repo_use_job_credentials(self):
        result = self.hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"{REF}\trefs/heads/main")
        self.assert_secret_free(result)
        self.assertEqual(self.requests, [
            ("/v3/jobs/job-123/repository_access_token", f"Token {AGENT_TOKEN}", {"repo_url": URL})
        ])
        self.assertFalse((self.home / ".local").exists())

    def test_install_is_left_in_place_and_reused(self):
        for name in ("entire", "git-remote-entire"):
            (self.bin / name).unlink()
        archive = os.environ.get("ENTIRE_TEST_ARCHIVE")
        curl = shutil.which("curl")
        (self.bin / "curl").unlink()
        self.env["TEST_ARCHIVE"] = str(Path(archive).resolve()) if archive else ""
        self.script(self.bin / "curl", f"""#!/bin/bash
printf '%s\\n' "$@" >> "$HOME/curl-argv"
if [[ "$*" == *entire_linux_amd64.tar.gz* && -n "$TEST_ARCHIVE" ]]; then
  cp "$TEST_ARCHIVE" "${{@: -1}}"
  exit
fi
exec {curl} "$@"
""")
        command = 'entire version; test -x "$(command -v git-remote-entire)"; [[ "$ENTIRE_TOKEN" == "$EXPECTED_TOKEN" ]]'
        result = self.hook(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installing Entire", result.stdout)
        self.assertIn("Entire CLI 0.11.4", result.stdout)
        self.assert_secret_free(result)
        self.assertTrue((self.home / ".local/bin/git-remote-entire").is_file())
        # A new hook process must discover the persisted installation.
        result = self.hook(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Installing Entire", result.stdout)
        self.assertIn("Entire CLI 0.11.4", result.stdout)
        self.assert_secret_free(result)
        self.assertEqual((self.home / "curl-argv").read_text().count("entire_linux_amd64.tar.gz"), 1)

    def test_corrupt_download_does_not_install(self):
        (self.bin / "entire").unlink()
        (self.bin / "curl").unlink()
        self.script(self.bin / "curl", '#!/bin/bash\nprintf corrupt > "${@: -1}"\n')
        result = self.hook()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertFalse((self.home / ".local").exists())
        self.assertEqual(self.requests, [])

    def test_api_errors_redirects_and_invalid_tokens_fail_without_leaking(self):
        for status, body in [(401, TOKEN), (500, TOKEN), (307, json.dumps({"token": TOKEN})),
                             (200, "not-json " + TOKEN), (200, '{}'),
                             (200, '{"token":""}'), (200, '{"token":null}')]:
            with self.subTest(status=status, body=body):
                self.response = status, body
                result = self.hook()
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(REF, result.stdout)
                self.assert_secret_free(result)
        self.assertEqual(len(self.requests), 7)


if __name__ == "__main__":
    unittest.main()
