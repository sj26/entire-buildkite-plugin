# Entire Buildkite Plugin

Install the [Entire CLI](https://github.com/entireio/cli) and authenticate the
Buildkite agent's checkout with job-issued credentials.

```yaml
plugins:
  - sj26/entire
```

Uses the native `entire://` URL in `BUILDKITE_REPO` and the agent's normal
checkout. No plugin configuration or Entire login is needed. The job must have
access to Buildkite's repository-token endpoint for its Entire repository.

An existing CLI and native Git remote helper are reused. If either is missing,
the plugin installs checksum-verified [Entire v0.11.4](https://github.com/entireio/cli/releases/tag/v0.11.4)
into `~/.local/bin` on Linux x86_64. On other platforms, install Entire first.
The installation is left in place for later jobs.

Before checkout, the plugin requests a token for `BUILDKITE_REPO` and exports
`ENTIRE_TOKEN` for the native helper. This token is also available to later job
commands and the agent's hook environment snapshots. Entire does not use Git
credential helpers. No remote wrapper, Git configuration, or CLI login is needed.

The token is installation-wide and expires after at most 45 minutes. It is
requested once before checkout, not refreshed; later Git operations fail after
it expires.
