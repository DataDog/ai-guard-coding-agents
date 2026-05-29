# Changelog

## [0.3.0](https://github.com/DataDog/ai-guard-coding-agents/compare/v0.2.0...v0.3.0) (2026-05-29)


### 🚀 Features

* honour CLAUDE_CONFIG_DIR for Claude Code config paths ([#20](https://github.com/DataDog/ai-guard-coding-agents/issues/20)) ([c0a972e](https://github.com/DataDog/ai-guard-coding-agents/commit/c0a972e15c7903ffe9f0e8f1aa71ecbb82ba750e))
* tag ai_guard.usr.id with os-user@hostname instead of OAuth email ([#16](https://github.com/DataDog/ai-guard-coding-agents/issues/16)) ([ae83baa](https://github.com/DataDog/ai-guard-coding-agents/commit/ae83baa1f0fffddc1ac2638d8c2985df9a25f8e0))


### 🐛 Bug Fixes

* **install:** reattach stdin to /dev/tty before handing off to ai-guard ([#15](https://github.com/DataDog/ai-guard-coding-agents/issues/15)) ([4e5d84d](https://github.com/DataDog/ai-guard-coding-agents/commit/4e5d84dd3adac05cff1879270a01d69d537b0be0))
* isolate subagent and main-session histories in proxy storage ([#17](https://github.com/DataDog/ai-guard-coding-agents/issues/17)) ([f2b224e](https://github.com/DataDog/ai-guard-coding-agents/commit/f2b224e573f5cafd480c2d2cfc4f56009e1b79ff))
* keep subagent and main-session histories in separate storage slots ([f2b224e](https://github.com/DataDog/ai-guard-coding-agents/commit/f2b224e573f5cafd480c2d2cfc4f56009e1b79ff))

## [0.2.0](https://github.com/DataDog/ai-guard-coding-agents/compare/v0.1.0...v0.2.0) (2026-05-26)


### 🚀 Features

* Initial version of the CLI for coding agents ([#1](https://github.com/DataDog/ai-guard-coding-agents/issues/1)) ([37d0466](https://github.com/DataDog/ai-guard-coding-agents/commit/37d0466c0e757a44a80e6eec332ac42852153b4b))
* Include a link in case of blocking a tool ([#5](https://github.com/DataDog/ai-guard-coding-agents/issues/5)) ([636cd5c](https://github.com/DataDog/ai-guard-coding-agents/commit/636cd5c2e2eb6677b266e5f8bbd056ca3084447a))
* Initial shell script based installer for macOS and Linux ([#6](https://github.com/DataDog/ai-guard-coding-agents/issues/6)) ([a17f74f](https://github.com/DataDog/ai-guard-coding-agents/commit/a17f74f09b2582a1b53e0e5c7bae7edec170aac4))
