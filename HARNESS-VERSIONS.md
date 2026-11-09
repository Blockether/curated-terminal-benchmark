# Harness version audit

Checked on 5 November 2026 against official npm metadata and GitHub releases.
This audit does not change pins or frozen benchmark inputs.

| Harness | Current pin | Latest stable | Result |
| --- | --- | --- | --- |
| Codex | 0.166.0 | 0.166.0 | Keep. 0.168.0 is an alpha release. |
| OpenCode | `@opencode/cli` 2.2.5 | 2.2.5 | Keep. The GitHub latest-release endpoint can return the old 1.19.5 release. |
| Pi | `@earendil-works/pi-coding-agent` 1.5.0 | 1.5.0 | Keep. Requires Node 22.19 or later. |
| Dirge | `453776330d4f44a6b07ab21974f8534c1318d596` with local patches | 0.26.1, `7eb18de27eaf5775a00220e74ee5b2c40375c706` | Update candidate. Tool logging still needs the local patch. |
| Kmet | `e2f9684f006a4b8b8e9d705c51db97ce5ce741da` | 0.2.0 | Pinned to development HEAD on 9 November 2026. The build needs Babashka 1.13.229 or later. |
| Wisp | `01b850ef05fd40038d6b27a15908b92566ed91c5` | none | Development HEAD, package version 0.3.0. The repository has no release tags. It runs on Node 26.13.0. |
| Samizdat | `9411f00c87d5ddb725f46eb8794e0d93a479835f` | 0.1.0 | Current pin equals development HEAD. |
| Vis | 0.5.3 | 0.5.4, `7681f3685de40521f2a864a15378f261020d29d3` | Update candidate. Pin the commit, Linux x64 archive and checksum together. |

## Dirge

Upstream `src/provider/run.rs` is unchanged between the current pin and
`3f8a63862911fe00ca16eae9b352012a79a1805b` (development HEAD).
It stores tool events until the end of a turn. A forced stop can lose that turn's events.
It does not record individual tool times. `--no-session` disables session storage,
not stream JSON output. Version 0.26.1 fixes a freeze after an nREPL evaluation error.
It still uses rig-core 0.41.0 and requires Rust 1.89 or later.

The benchmark's `tools-v4` patch writes tool calls and results immediately.
It does not change the model, prompt, tool dispatch or task scoring.
The report reads both old and new logs. Old measurements are not added or estimated.

## Upgrade policy

Do not change a running or frozen round. Use a new round for updated pins.
Kmet uses Babashka in this benchmark; its separate Jolt build constraints do not apply here.
Samizdat's upstream repository moved to `samizdat-code/samizdat`.

## Sources

- [Codex 0.166.0](https://github.com/openai/codex/releases/tag/rust-v0.166.0)
- [OpenCode 2.2.5](https://github.com/anomalyco/opencode/releases/tag/v2.2.5)
- [Pi 1.5.0](https://github.com/earendil-works/pi/releases/tag/v1.5.0)
- [Dirge 0.26.1](https://github.com/dirge-code/dirge/releases/tag/v0.26.1)
- [Kmet 0.2.0](https://github.com/kmetia/kmet-agent/releases/tag/v0.2.0)
- [Kmet changes](https://github.com/kmetia/kmet-agent/compare/b141b2bdf9ee26f6366552b94259ab8a4a40195b...1e4e76aa2d95abedc8ee82258a6feae982ab2e58)
- [Samizdat 0.1.0](https://github.com/samizdat-code/samizdat/releases/tag/v0.1.0)
- [Vis 0.5.4](https://github.com/Blockether/vis/releases/tag/v0.5.4)
