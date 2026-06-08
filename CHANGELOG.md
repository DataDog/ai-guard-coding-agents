# Changelog

## [0.5.1](https://github.com/DataDog/ai-guard-coding-agents/compare/v0.5.0...v0.5.1) (2026-06-08)


### 🐛 Bug Fixes

* map Anthropic content blocks to valid AI Guard part types ([#39](https://github.com/DataDog/ai-guard-coding-agents/issues/39)) ([0a9aef5](https://github.com/DataDog/ai-guard-coding-agents/commit/0a9aef55f3b7cd9b1ea59fd711bb2860e242dee8))

## [0.5.0](https://github.com/DataDog/ai-guard-coding-agents/compare/v0.4.0...v0.5.0) (2026-06-07)


### 🚀 Features

* add UserPromptExpansion hook for Claude Code ([#33](https://github.com/DataDog/ai-guard-coding-agents/issues/33)) ([bcb1e86](https://github.com/DataDog/ai-guard-coding-agents/commit/bcb1e86c74798a4798dc43f9c19583367b56cb58))
* vendor AI Guard client to depend on released ddtrace ([#31](https://github.com/DataDog/ai-guard-coding-agents/issues/31)) ([8cd15be](https://github.com/DataDog/ai-guard-coding-agents/commit/8cd15be08613d4c8a3baa11d5dea6818206fe0f8))


### ♻️ Code Refactoring

* drop the HTTP proxy in favour of in-process Claude Code hooks ([#30](https://github.com/DataDog/ai-guard-coding-agents/issues/30)) ([f9e67f7](https://github.com/DataDog/ai-guard-coding-agents/commit/f9e67f7e10c110767adcb384f495ed59e198209f))

## [0.4.0](https://github.com/DataDog/ai-guard-coding-agents/compare/v0.3.0...v0.4.0) (2026-06-05)


### 🚀 Features

* add DD_AI_GUARD_PRIVACY_MODE to control UI content surfacing ([#28](https://github.com/DataDog/ai-guard-coding-agents/issues/28)) ([d2a4810](https://github.com/DataDog/ai-guard-coding-agents/commit/d2a48103e38eeab7d0617b51eaa6ffe285799811))
* store DD_API_KEY/DD_APP_KEY in the OS keychain ([#25](https://github.com/DataDog/ai-guard-coding-agents/issues/25)) ([5ac21c5](https://github.com/DataDog/ai-guard-coding-agents/commit/5ac21c5a0c2dd90301e6e91f7829ca0cded6f17a))


### 🐛 Bug Fixes

* recover session id from request metadata when header is absent ([#26](https://github.com/DataDog/ai-guard-coding-agents/issues/26)) ([de8d5d3](https://github.com/DataDog/ai-guard-coding-agents/commit/de8d5d3cba8457de9708a771a222f0410a14cb8c))

## [0.3.0](https://github.com/DataDog/ai-guard-coding-agents/compare/v0.2.0...v0.3.0) (2026-05-29)


### 🚀 Features

* honour CLAUDE_CONFIG_DIR for Claude Code config paths ([#20](https://github.com/DataDog/ai-guard-coding-agents/issues/20)) ([c0a972e](https://github.com/DataDog/ai-guard-coding-agents/commit/c0a972e15c7903ffe9f0e8f1aa71ecbb82ba750e))
* tag ai_guard.usr.id with os-user@hostname instead of OAuth email ([#16](https://github.com/DataDog/ai-guard-coding-agents/issues/16)) ([ae83baa](https://github.com/DataDog/ai-guard-coding-agents/commit/ae83baa1f0fffddc1ac2638d8c2985df9a25f8e0))


### 🐛 Bug Fixes

* **install:** reattach stdin to /dev/tty before handing off to ai-guard ([#15](https://github.com/DataDog/ai-guard-coding-agents/issues/15)) ([4e5d84d](https://github.com/DataDog/ai-guard-coding-agents/commit/4e5d84dd3adac05cff1879270a01d69d537b0be0))
* isolate subagent and main-session histories in proxy storage ([#17](https://github.com/DataDog/ai-guard-coding-agents/issues/17)) ([f2b224e](https://github.com/DataDog/ai-guard-coding-agents/commit/f2b224e573f5cafd480c2d2cfc4f56009e1b79ff))

## [0.2.0](https://github.com/DataDog/ai-guard-coding-agents/compare/v0.1.0...v0.2.0) (2026-05-26)


### 🚀 Features

* Initial version of the CLI for coding agents ([#1](https://github.com/DataDog/ai-guard-coding-agents/issues/1)) ([37d0466](https://github.com/DataDog/ai-guard-coding-agents/commit/37d0466c0e757a44a80e6eec332ac42852153b4b))
* Include a link in case of blocking a tool ([#5](https://github.com/DataDog/ai-guard-coding-agents/issues/5)) ([636cd5c](https://github.com/DataDog/ai-guard-coding-agents/commit/636cd5c2e2eb6677b266e5f8bbd056ca3084447a))
* Initial shell script based installer for macOS and Linux ([#6](https://github.com/DataDog/ai-guard-coding-agents/issues/6)) ([a17f74f](https://github.com/DataDog/ai-guard-coding-agents/commit/a17f74f09b2582a1b53e0e5c7bae7edec170aac4))
