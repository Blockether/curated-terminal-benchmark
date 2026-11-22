# Harness version audit

Checked on 22 November 2026 against official npm metadata and GitHub releases.
This audit does not change pins or frozen benchmark inputs.

| Harness | Current pin | Latest stable | Result |
| --- | --- | --- | --- |
| Codex | 0.166.0 | 0.171.0 | Update candidate. A new pin needs new rounds of every agent. |
| OpenCode | `@opencode/cli` 2.2.5 | 2.3.2 | Update candidate. The GitHub latest-release endpoint can return the old 1.19.5 release; npm is the source. |
| Pi | `@earendil-works/pi-coding-agent` 1.5.0 | 1.6.0 | Update candidate. Requires Node 22.19 or later. The experiment arm `pi-shell-only` selects only bash with `--tools bash` (from pull request 3). |
| Dirge | `453776330d4f44a6b07ab21974f8534c1318d596` with local patches | 0.26.1, `7eb18de27eaf5775a00220e74ee5b2c40375c706` | Update candidate. Tool logging still needs the local patch. |
| Kmet | `b52aa1186c5f6e3968b15f2bfc23ea59e5851f07` | v0.3.1-106 | Pinned to development HEAD on 20 November 2026. The build needs Babashka 1.13.229 or later. The arms `kmet-eval-only`, `kmet-codemode` and `kmet-shell-only` select one tool with `--tools`. The local `kmet-read-tail-zero` patch reads `tail` 0 of the read tool as no tail: GPT sends the unused optional `tail` as 0 next to `offset`, and kmet refused 2246 such calls in round `kmet-tools-3cafc66/r1`. Development HEAD `71d73991a931` (21 November 2026) is 17 commits newer; its commit "absorb model tail 0" can make the local patch unnecessary. |
| Wisp | `9cddabcdd3cbff0b461d5026482658fdaba6e3c4` | none | Development HEAD of `xificurC/wisp` on 19 November 2026. The script is the same as at `13e2552`; the new commit adds only design notes. The repository has no release tags. It is one Python script and runs on the toolkit Python. The local `wisp-missing-git` patch lets it start in task images without git. Development HEAD `e6e20f05b2e4` replaced the history; it adds a bench harness, tests and notes, and shows no line change in the `wisp/wisp` script. |
| Samizdat | `9411f00c87d5ddb725f46eb8794e0d93a479835f` | 0.1.0 | Paused. Development HEAD is `daf7a373e1f0` (21 November 2026). |
| Vis | 0.5.12, `55d9a4e6a7e532b0d9667442e8e914a4180d4345` | 0.5.30 | Pinned on 11 November 2026 with the Linux arm64 release archive, its checksum and `vis-lang-python` 1.16.10. |

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

## Not added

- Nodecode (pull request 4): Linux releases up to 0.1.16 are x86_64 only. The benchmark runs natively on arm64, and an emulated arm is not comparable.
- Xi (pull request 5): `xi prompt` fixes the reasoning effort at high and has no switch. The other agents use medium, so the results are not comparable.

## Upgrade policy

Do not change a running or frozen round. Use a new round for updated pins.
Kmet uses Babashka in this benchmark; its separate Jolt build constraints do not apply here.
Samizdat's upstream repository moved to `samizdat-code/samizdat`.

## Sources

- [Codex 0.166.0](https://github.com/openai/codex/releases/tag/rust-v0.166.0)
- [OpenCode 2.2.5](https://github.com/anomalyco/opencode/releases/tag/v2.2.5)
- [Pi 1.5.0](https://github.com/earendil-works/pi/releases/tag/v1.5.0)
- [Pi 1.6.0](https://www.npmjs.com/package/@earendil-works/pi-coding-agent/v/1.6.0)
- [Codex 0.171.0](https://github.com/openai/codex/releases/tag/rust-v0.171.0)
- [Nodecode releases](https://github.com/nodecode-ai/nodecode/releases)
- [Dirge 0.26.1](https://github.com/dirge-code/dirge/releases/tag/v0.26.1)
- [Kmet 0.2.0](https://github.com/kmetia/kmet-agent/releases/tag/v0.2.0)
- [Kmet changes](https://github.com/kmetia/kmet-agent/compare/b141b2bdf9ee26f6366552b94259ab8a4a40195b...1e4e76aa2d95abedc8ee82258a6feae982ab2e58)
- [Samizdat 0.1.0](https://github.com/samizdat-code/samizdat/releases/tag/v0.1.0)
- [Vis 0.5.4](https://github.com/Blockether/vis/releases/tag/v0.5.4)
