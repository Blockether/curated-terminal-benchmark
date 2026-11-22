# Harness version audit

Checked on 22 November 2026 against official npm metadata and GitHub releases.
This audit does not change pins or frozen benchmark inputs.

| Harness | Current pin | Latest stable | Result |
| --- | --- | --- | --- |
| Codex | 0.166.0 | 0.171.0 | Update candidate. A new pin needs new rounds of every agent. |
| OpenCode | `@opencode/cli` 2.2.5 | 2.3.2 | Update candidate. The GitHub latest-release endpoint can return the old 1.19.5 release; npm is the source. |
| Pi | `@earendil-works/pi-coding-agent` 1.5.0 | 1.6.0 | Update candidate. Requires Node 22.19 or later. The experiment arm `pi-shell-only` selects only bash with `--tools bash` (from pull request 3). |
| Kmet | `b52aa1186c5f6e3968b15f2bfc23ea59e5851f07` | v0.3.1-106 | Pinned to development HEAD on 20 November 2026. The build needs Babashka 1.13.229 or later. The arms `kmet-eval-only`, `kmet-codemode` and `kmet-shell-only` select one tool with `--tools`. The local `kmet-read-tail-zero` patch reads `tail` 0 of the read tool as no tail: GPT sends the unused optional `tail` as 0 next to `offset`, and kmet refused 2246 such calls in round `kmet-tools-3cafc66/r1`. Development HEAD `71d73991a931` (21 November 2026) is 17 commits newer; its commit "absorb model tail 0" can make the local patch unnecessary. |
| Xi | `xi-agent` 0.6.0 | 0.6.0 | Added from pull request 5 on 22 November 2026, with the npm tarball and its SHA-256. Runs on Bun 1.4.4, pinned with checksums. Three local patches. `xi-codex-settings`: Xi sends no reasoning effort to its Codex provider, so GPT got the provider default low with text verbosity medium; the patch lets the run set effort medium, verbosity low and summary auto, as for every agent. `xi-trace`: Xi keeps the token usage and the times only in memory; the patch writes each model request (start, milliseconds, usage) and each tool call (start, milliseconds, error) to a file. The report reads the calls and tool times from it. `xi-python-claim`: the base prompt says that Python is not available, and in a test run Xi refused a Python task for this reason; the patch removes only that sentence. |
| Wisp | `9cddabcdd3cbff0b461d5026482658fdaba6e3c4` | none | Development HEAD of `xificurC/wisp` on 19 November 2026. The script is the same as at `13e2552`; the new commit adds only design notes. The repository has no release tags. It is one Python script and runs on the toolkit Python. The local `wisp-missing-git` patch lets it start in task images without git. Development HEAD `e6e20f05b2e4` replaced the history; it adds a bench harness, tests and notes, and shows no line change in the `wisp/wisp` script. |
| Samizdat | `daf7a373e1f0164ea3c688c832a849de68620696` | 0.1.0 | Development HEAD of `samizdat-code/samizdat` on 21 November 2026. It speaks the OpenAI Responses wire through the host proxy. On arm64 the fetch builds Samizdat and Jolt v0.8.28 from source, because Jolt has no arm64 release archive. |
| Vis | 0.5.12, `55d9a4e6a7e532b0d9667442e8e914a4180d4345` | 0.5.30 | Pinned on 11 November 2026 with the Linux arm64 release archive, its checksum and `vis-lang-python` 1.16.10. |

## Not added

- Dirge: removed on 22 November 2026. No sealed or published round has a Dirge trial.
- Nodecode (pull request 4): Linux releases up to 0.1.16 are x86_64 only. The benchmark runs natively on arm64, and an emulated arm is not comparable.

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
- [Kmet 0.2.0](https://github.com/kmetia/kmet-agent/releases/tag/v0.2.0)
- [Kmet changes](https://github.com/kmetia/kmet-agent/compare/b141b2bdf9ee26f6366552b94259ab8a4a40195b...1e4e76aa2d95abedc8ee82258a6feae982ab2e58)
- [Xi 0.6.0](https://github.com/floscr/xi/tree/v0.6.0)
- [xi-agent 0.6.0](https://www.npmjs.com/package/xi-agent/v/0.6.0)
- [Samizdat 0.1.0](https://github.com/samizdat-code/samizdat/releases/tag/v0.1.0)
- [Vis 0.5.4](https://github.com/Blockether/vis/releases/tag/v0.5.4)
