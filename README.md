# Entire Buildkite Plugin

Install the [Entire CLI](https://github.com/entireio/cli) and let the existing
Buildkite agent check out an Entire repository using job-issued credentials.
This is a development plugin in `sj26`; it has not been promoted to
`buildkite-plugins`.

## Use

A compatible Buildkite Entire repository-provider integration injects a pinned
plugin reference when its checkout setting is enabled. No plugin configuration
or personal login is needed. This integration is under development; adding the
plugin alone cannot grant repository access.

For an explicit declaration (for example, a signed pipeline), replace
`FULL_COMMIT_SHA` with a reviewed 40-character commit from this repository:

```yaml
steps:
  - command: ./test.sh
    plugins:
      - github.com/sj26/entire-buildkite-plugin#FULL_COMMIT_SHA: null
```

The provider must supply `BUILDKITE_ENTIRE_CLONE_URL` from the selected
repository's current `entire_clone_url`, validated server-side. Do not derive
this URL from the HTTPS URL or put credentials in it. A missing or invalid URL
fails checkout. There are no plugin options.

## Requirements and limits

* **Linux x86_64 only.** Initial target: Debian 12 with Buildkite Agent 4.3.0 and
  Git 2.55.0. macOS, Windows, ARM, and other architectures are not supported and
  fail before installation. Other Linux distributions have not been validated.
* Bash, curl, Git, jq, GNU `sha256sum`, tar, and standard coreutils must be on PATH.
* The Buildkite job must be an eligible **hosted command job**, with a connected,
  selected Entire repository. The credential endpoint refuses self-hosted jobs.
* The agent needs outbound access to GitHub release downloads and Entire, and
  a writable executable temporary directory. No root or package-manager access
  is needed. A fresh installation is made for each job.
* Only the selected repository's exact Entire URL is authenticated. Other
  repositories, submodules, Git LFS, and container-based checkout are not covered.
  Do not combine this plugin with a plugin that replaces checkout or shadows its
  remote helper. Host installation does not install binaries inside containers.

## Checkout and authentication

`pre-checkout` installs Entire **v0.11.4**, including `git-remote-entire`, in a
job-private temporary directory. The release URL and SHA-256 are pinned in the
plugin. HTTPS-only download redirects are allowed, and a checksum mismatch
fails before extraction or execution. No `curl | bash`, `latest`, dynamic
checksum trust, global installation, or CLI login is used. Updating the CLI
requires a reviewed plugin change. `pre-exit` removes the installation; an
uncatchable process/host failure can leave temporary binaries, but no token file.

The hook prepends that directory to PATH and changes `BUILDKITE_REPO` to
`BUILDKITE_ENTIRE_CLONE_URL`. It does **not** replace the agent's checkout hook:
the agent still manages clone/fetch, commit selection, and checkout retries.
The working copy's remote remains a credential-free `entire://` URL.

When Git invokes `git-remote-entire`, the plugin wrapper requests:

```text
POST $BUILDKITE_AGENT_ENDPOINT/jobs/$BUILDKITE_JOB_ID/repository_access_token
Authorization: Token <agent session credential>
Content-Type: application/json

{"repo_url":"<BUILDKITE_ENTIRE_CLONE_URL>"}
```

The existing endpoint authorizes the running job and checks the selected
repository's current URL. The response is `{token, username}`. The wrapper uses
`token` as `ENTIRE_TOKEN` and execs the verified native helper. The username is
not used: Entire's helper uses bearer authentication, not HTTPS Basic auth.
This calls the API directly, not the agent's HTTPS-only credential helper, and
requires no agent change. Buildkite must disable its global repository-provider
credential helper for jobs using this plugin.

The agent session credential reaches curl through stdin, never argv. The
Entire token exists only in the wrapper/helper process environment. It is not
exported by a hook, captured in a hook environment snapshot, or written to plugin
configuration, Git config, a token file, or a login store. Later commands do not
inherit it. Git commands later in the same job can request a new token through
the wrapper. This is process isolation, not protection from other code running
as the same OS user; job code already has access to the agent credential.

API redirects are refused. Errors report HTTP status without response bodies.
Shell tracing is disabled before credentials are read; helper debug/trace and
TLS-skip settings are removed. An inherited personal `ENTIRE_TOKEN` is never used
as a fallback. The native helper can cache cluster/replica addresses under HOME,
but does not persist this token.

**Upstream scope limit:** the token is installation-wide, not repository-scoped,
and can last up to 45 minutes. Buildkite requests `contents:read`, but Entire can
also grant `installations:read` and `checks:write`. The URL check is not token
attenuation. Jobs sharing an installation must be trusted to access its selected
repositories. The plugin does not claim to reduce the token's permissions.

## Why the native helper

The public HTTPS clone host can challenge for authentication and then redirect
to a regional node. Git/libcurl strips credentials on that cross-host redirect
without asking the credential helper for the new host. Adding a credential
helper alone does not fix this flow.

Entire's supported `entire://` transport uses cluster discovery and replica
routing. The v0.11.4 helper accepts `ENTIRE_TOKEN` directly, derives a core origin
from the JWT audience, checks it against cluster metadata, and sends it as a
bearer token. It does not need a user token or an OAuth exchange:

* [Token resolution and trust check](https://github.com/entireio/cli/blob/30277e8855c04590010933155906063ccb9d699d/cmd/git-remote-entire/main.go#L317-L399)
* [JWT audience contract](https://github.com/entireio/cli/blob/30277e8855c04590010933155906063ccb9d699d/cmd/entire/cli/auth/env_token.go#L38-L97)
* [Release and archives](https://github.com/entireio/cli/releases/tag/v0.11.4)

The installation-token contract was checked against a live Entire repository:
`ls-remote`, clone, exact-commit checkout, and fetch succeeded with an empty
environment and fresh HOME, without CLI login. Only cluster/replica metadata was
cached. This probe is separate from full plugin/agent integration validation.

## Tests

On Linux x86_64 with the runtime dependencies plus Python 3 and ShellCheck:

```sh
shellcheck hooks/* lib/*
python3 -m unittest discover -s tests -v
```

The installation test downloads the pinned release. For an offline run, set
`ENTIRE_TEST_ARCHIVE` to a previously downloaded archive; the plugin still
verifies the pinned digest. Tests use a local HTTP server and fake credentials
to check actual curl/Git behavior, argv/log/file secret safety, per-invocation
token requests, API error and redirect refusal, URL rejection, corrupt downloads,
installation cleanup, and unsupported-platform failure. No live secrets are
needed by the test suite.
