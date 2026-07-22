# Changelog

All notable changes to `mithwire-mcp` are documented here. This file is
maintained automatically by [release-please](https://github.com/googleapis/release-please);
entries below are generated from [Conventional Commit](https://www.conventionalcommits.org/)
messages. Do not edit released sections by hand.

## [0.4.1](https://github.com/codeisalifestyle/mithwire-mcp/compare/v0.4.0...v0.4.1) (2026-07-22)


### Features

* expose native CDP mouse input tools ([#26](https://github.com/codeisalifestyle/mithwire-mcp/issues/26)) ([a633dcd](https://github.com/codeisalifestyle/mithwire-mcp/commit/a633dcdf1077dae1c50bccc443383f72d480200b))

## [0.4.0](https://github.com/codeisalifestyle/mithwire-mcp/compare/v0.3.2...v0.4.0) (2026-07-20)


### ⚠ BREAKING CHANGES

* rename BridgeBrowser → MithwireBrowser, stock → cdp, wire warmup status
* The 4 preset MCP tools (session_preset_list/get/set/delete) and the preset parameter on session_start/session_profile_set are removed. Profiles are now the single source of identity and launch configuration.

### Features

* add profile warming infrastructure ([5d8a8cc](https://github.com/codeisalifestyle/mithwire-mcp/commit/5d8a8cc029e2530cfb7ba44bb8fc8f3efc2f4965))
* enable stealth mode on macOS, fix language propagation ([e4822af](https://github.com/codeisalifestyle/mithwire-mcp/commit/e4822af858d6b97d0048a5f01b1303defa3f48d3))
* enable stealth on macOS, fix language propagation ([7e1b5f3](https://github.com/codeisalifestyle/mithwire-mcp/commit/7e1b5f3380961826a910c204fc8cbcb857cd8a4b))
* profile architecture — persisted fingerprint, bound proxy, lifecycle metadata ([67cc10a](https://github.com/codeisalifestyle/mithwire-mcp/commit/67cc10af20aebd44a2f9744de328a396331ff3dc))
* remove presets, simplify to 3-layer profile model ([d63e8f3](https://github.com/codeisalifestyle/mithwire-mcp/commit/d63e8f3c12fa212f711ad0c5ad3c2916a8a0efbf))
* rename BridgeBrowser → MithwireBrowser, stock → cdp, wire warmup status ([a0137cf](https://github.com/codeisalifestyle/mithwire-mcp/commit/a0137cf9c8058220f0b0b114e314ef066c49e020))
* stealth on macOS + language propagation fix ([63aff70](https://github.com/codeisalifestyle/mithwire-mcp/commit/63aff70dc1e2cfd695766f24024ee9f2fe81e6c8))


### Bug Fixes

* add --no-sandbox for CloakBrowser on Linux only ([cd20dc2](https://github.com/codeisalifestyle/mithwire-mcp/commit/cd20dc2989ef86c81e136d8d9dc8fb6f2ea52b04))
* conditional --no-sandbox for CloakBrowser (Linux only) ([c2de032](https://github.com/codeisalifestyle/mithwire-mcp/commit/c2de0323c6e08327fe55a5126a3875bf2440a133))
* remove unconditional --no-sandbox from CloakBrowser adapter ([8525a14](https://github.com/codeisalifestyle/mithwire-mcp/commit/8525a14b6313607d1682320ba7f324f1b906d0cd))


### Refactors

* delegate infrastructure modules to engine ([deec182](https://github.com/codeisalifestyle/mithwire-mcp/commit/deec182e9c02607c9ce503067c94ec8d71bb9e4c))
* delegate proxy, fingerprint, cloakbrowser, virtual_display to engine ([85805cd](https://github.com/codeisalifestyle/mithwire-mcp/commit/85805cd38cdcfa874678ea67b9f9df3ce65c2dcf))

## [0.3.2](https://github.com/codeisalifestyle/mithwire-mcp/compare/v0.3.1...v0.3.2) (2026-07-19)


### Bug Fixes

* **ci:** refine CI/CD pipeline and remove leaked test credential ([503d098](https://github.com/codeisalifestyle/mithwire-mcp/commit/503d0984a052bab277f0a7e7ae83f2ac6e125661))
* **ci:** refine CI/CD pipeline and security hardening ([d0939e3](https://github.com/codeisalifestyle/mithwire-mcp/commit/d0939e3632e9e61fc716ed33fecfd65c728790da))

## [0.3.1](https://github.com/codeisalifestyle/mithwire-mcp/compare/v0.3.0...v0.3.1) (2026-07-19)


### Features

* **ci:** run detection matrix with stealth engine on Linux ([d7ba86b](https://github.com/codeisalifestyle/mithwire-mcp/commit/d7ba86bd0ff371b0228355284c41c05ef9e1f3f2))

## [0.3.0](https://github.com/codeisalifestyle/mithwire-mcp/compare/v0.2.2...v0.3.0) (2026-07-19)


### Features

* **stealth:** CloakBrowser engine integration with fingerprint-platform auto-detection ([730cfdb](https://github.com/codeisalifestyle/mithwire-mcp/commit/730cfdb))
* **stealth:** BridgeDriver resolves CloakBrowser binary for engine=stealth ([e94fb74](https://github.com/codeisalifestyle/mithwire-mcp/commit/e94fb74))
* **test-suite:** BrowserLeaks probes (JS, canvas, WebGL, WebRTC, fonts, TLS) ([e7a7b1e](https://github.com/codeisalifestyle/mithwire-mcp/commit/e7a7b1e))
* **test-suite:** detection site probes (BrowserScan, Incolumitas, Pixelscan) ([4383139](https://github.com/codeisalifestyle/mithwire-mcp/commit/4383139))
* **test-suite:** captcha probes (reCAPTCHA v3, Cloudflare Turnstile) ([4383139](https://github.com/codeisalifestyle/mithwire-mcp/commit/4383139))
* **test-suite:** OVP.js integration with Doppler proxy and IP-quality site support ([3f19336](https://github.com/codeisalifestyle/mithwire-mcp/commit/3f19336))
* **runtime:** Xvfb virtual display manager and BrowserForge fingerprint generation ([16fc2eb](https://github.com/codeisalifestyle/mithwire-mcp/commit/16fc2eb))


### Bug Fixes

* **stealth:** CloakBrowser fingerprint-platform defaults to host OS, not windows ([730cfdb](https://github.com/codeisalifestyle/mithwire-mcp/commit/730cfdb))
* **test-suite:** OVP probe handles API-key demo layout and null-safety ([e13ccc1](https://github.com/codeisalifestyle/mithwire-mcp/commit/e13ccc1))

## [0.2.2](https://github.com/codeisalifestyle/mithwire-mcp/compare/v0.2.1...v0.2.2) (2026-06-20)


### Refactors

* **baseline-probe:** consume engine stealth_diagnostic probes ([#10](https://github.com/codeisalifestyle/mithwire-mcp/issues/10)) ([e9102cf](https://github.com/codeisalifestyle/mithwire-mcp/commit/e9102cfc3037935a844cf29ab6c95e42e50f9f7b))

## [0.2.1](https://github.com/codeisalifestyle/mithwire-mcp/compare/v0.2.0...v0.2.1) (2026-06-13)


### Features

* consume engine-owned anti-detect stealth ([#7](https://github.com/codeisalifestyle/mithwire-mcp/issues/7)) ([a04b6b3](https://github.com/codeisalifestyle/mithwire-mcp/commit/a04b6b3f4ef8b58df98a0d90bfdb33a31d949687))

## [0.2.0](https://github.com/codeisalifestyle/mithwire-mcp/compare/v0.1.0...v0.2.0) (2026-06-13)


### ⚠ BREAKING CHANGES

* **state:** presets and first-class proxy registry

### Features

* **state:** migrate-state subcommand, cookies/ inbox, README refresh ([#4](https://github.com/codeisalifestyle/mithwire-mcp/issues/4)) ([aa8b77f](https://github.com/codeisalifestyle/mithwire-mcp/commit/aa8b77fb33c802a16840b90ab2f57d6950ba7c47))
* **state:** presets and first-class proxy registry ([ed96654](https://github.com/codeisalifestyle/mithwire-mcp/commit/ed966548319882f06fb812e47d5cf6912e26cbb9))

## [0.1.0] - 2026-06-04

Initial published release: MCP browser bridge exposing a live, stealth-capable
browser to AI clients, backed by the `mithwire` engine. State store with
profiles, presets, a first-class proxy registry, cookie injection, and a
dashboard.
