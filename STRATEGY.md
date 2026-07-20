# Strategy: CI + packaging for `mithwire` / `mithwire-mcp`

Written 2026-06-05. Decision-oriented; assumes the maintainer reads `MithwireBrowser`, `.cursor/skills/mithwire-mcp-dev-flow/SKILL.md`, `scripts/SITE_PARSING.md`, the CI workflow, and the latest two CI runs as background.

---

## 1. Mental model: what "CI" actually is here

Three things hide inside the current `ci.yml`. Confusing them is what makes the current matrix failure look ambiguous. Separate them, and the decisions become obvious.

**Class A — code-correctness.** `ruff`, `pyright` (when we add it), unit tests under `tests/test_proxy.py`, `tests/test_runtime.py`, `tests/test_align_timezone_retry.py`, etc. These run pure Python with no browser. They are deterministic and OS-independent within reason. Verdict: binary, equal across every supported runner. Today's `unit-fast` job is exactly this. **Optimize for cost and speed.**

**Class B — Chrome-binding.** Does `MithwireBrowser.start()` actually launch Chromium? Does `apply_fingerprint` push the values it claims into `navigator.*`, `userAgentData`, the worker scope, etc.? `tests/test_fingerprint_application.py` is the canonical Class B suite. The verdict is **"the code talks to Chrome correctly,"** not **"the page looks human."** It needs a real Chromium and benefits from a pinned version, but the underlay OS only matters insofar as Chrome runs there. Today's `e2e-application` job is exactly this. **Optimize for a known-good Chrome build on a single underlay.**

**Class C — anti-detect ("does a real-world bot detector call us a bot").** `scripts/profile_matrix.py` driving `baseline_probe.py` against DAB, Sannysoft, CreepJS, Fingerprint Pro. The verdict is **environmental**, not just code. It depends on: (1) the egress IP's reputation, (2) the underlay OS the spoofed profile is claiming to *not* be, (3) the Chrome version, (4) whether the proxy is on, (5) whether the profile is internally consistent with the host (the "same-OS-family rule" in the dev-flow skill). A Class C run can fail for reasons unrelated to the diff under review. Today's `detection-matrix` job is exactly this, and the GHA failure pattern proves the point.

The single most important strategic move in this document is **stop running Class C on GitHub Actions as a hard gate** and confine GHA to A + B. Sections 3 and 6 spell out how.

## 2. Where the product actually runs in production

Talking to the code, not the README:

- **Primary deploy target: Linux VPS, headful or headless, MCP stdio over a remote agent connection.** `MithwireBrowser.start()` refuses to fall back to `--no-sandbox` for stealth reasons (`browser.py:204–216`). The MCP architecture in `SKILL.md` describes `pipx install mithwire-mcp` and `~/.cursor/mcp.json` as the canonical config. The proxy auth relay (`LocalProxyRelay`) and the proxy-aware timezone alignment exist for one reason: an operator running the MCP on a server in front of a mobile/residential exit and driving real workflows from a remote AI.
- **Secondary target: macOS desktop dev/personal use via Cursor.** This is the maintainer's own loop (the dev-flow skill, the dev/stable worktree split, `register-dev-mcp.py`). It is the cheapest, fastest place to iterate. The mac-* profile presets and the same-OS-family rule both imply Mac is the dominant *test* host, not the dominant *deploy* host.
- **Tertiary target: Windows desktop.** Mentioned in the README (`pipx install` instructions), but no design choice in `MithwireBrowser` is Windows-specific and the test suite has no Windows-only paths. Realistically: a small minority of hobbyist users will install it on Windows. We owe them Class A code-correctness ("import works, parse_proxy works, server starts") and that's it. Anyone running headed automation in production on Windows is unusual.

This map flows directly into the rest of the document. We owe a **stealth guarantee** to the Linux VPS deployer and to the macOS desktop user. We owe **code-correctness only** to the Windows user. We owe **observability** (so the maintainer can see regressions) to himself.

## 3. CI environment options

For each option: which test class it serves, true cost, what it actually measures.

### Local macOS pre-commit on the maintainer's box

- **Serves:** A (cheap), B (real Chrome on Mac is fast), and a *meaningful slice of C* — namely, mac-* profiles tested through a residential broadband egress and/or the falconproxy mobile proxy listed in the dev-flow skill. This is the *only* environment where the matrix's gate (`mac-*` profile → DAB `human`) is honest, because the underlay is actually a Mac.
- **Failure mode:** the maintainer forgets to run it, or it's slow enough that he skips it. A 5-second `ruff` + `pytest -m 'not stealth_e2e'` is fine for every commit; the matrix is multiple minutes and only makes sense before merge or before tagging a release.
- **Wiring:** `pre-commit` framework with two hooks — one fast (ruff + unit tests), one opt-in (`make matrix` invoking `scripts/profile_matrix.py` with the mobile proxy). I'd prefer a plain `Makefile` over `lefthook` because the project is already a uv-managed Python project and a `make verify` is one line. `pre-commit` only for the fast hook so the slow matrix doesn't ambush commits.

### GitHub Actions Ubuntu runners (`ubuntu-latest`)

- **Serves:** A perfectly, B perfectly with the AppArmor `sysctl` workaround, **C only as a stale signal**.
- **The AppArmor workaround we landed is the official Chromium recipe.** Ubuntu 23.10+ restricts unprivileged user namespaces by default; Chromium's docs explicitly call out `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` as the runner-side fix ([Chromium docs](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md), [Ubuntu announcement](https://ubuntu.com/blog/ubuntu-23-10-restricted-unprivileged-user-namespaces.)). The alternative — installing Chrome to `/opt/google/chrome/chrome` so Ubuntu's shipped AppArmor profile applies — would require not using `browser-actions/setup-chrome@v1`'s default install path. Our current `sysctl` step is the right call for CI; production users should *not* copy this verbatim.
- **Cost (2026 pricing, [GitHub Actions billing docs](https://docs.github.com/billing/managing-billing-for-github-actions/about-billing-for-github-actions)):** Linux 2-core at `$0.006/min`, and standard-runner usage on a **public** repo is free (no quota consumption). For private, the project's Free-plan pool is 2,000 Linux-equivalent minutes/month. Even the current 4-profile matrix at ~3 min/profile per run + ~30 s setup overhead is comfortably under the free pool for either case.
- **What we're actually measuring with Class C on GHA Ubuntu.** This is the heart of the question. The current matrix failure on `main` is *identical* across all four `mac-*` profiles: `isBot=true flags=hasInconsistentWorkerValues`. Per DAB's own documentation, that flag fires when values collected in a Web Worker (userAgent, languages, hardwareConcurrency, platform, WebGL vendor/renderer) **differ from the main JS context** ([deviceandbrowserinfo.com](https://deviceandbrowserinfo.com/are_you_a_bot)). The exact same failure is recorded against mithwire in [browser-use/browser-use#360](https://github.com/browser-use/browser-use/issues/360), where the reporter notes that when mithwire fails DAB, it is **always** on `hasInconsistentWorkerValues`. The DAB `details` map *also* exposes IP-related flags, and **none of those are currently true** on the GHA runs — meaning Azure IP reputation is not the gate failure here. The gate failure is a real cross-OS spoofing leak: when `mac-uk` runs on a Linux underlay, the spoof layer covers main-thread navigator and the fields the worker bootstrap re-asserts (`languages`, `deviceMemory`, `hardwareConcurrency`, WebGL — see `browser.py:_worker_bootstrap_js`), but the worker still leaks fields the bootstrap doesn't reach (most likely the worker's `userAgent` and/or `userAgentData.platform` plus `platform`, since `type: 'module'` workers and CDP UA-override propagation to workers are both partial). The same `mac-uk` profile passes locally on macOS *because the host platform happens to agree with the spoofed identity*, so there is nothing for DAB's worker check to disagree with.
- **Conclusion:** GHA Ubuntu Class C testing is not garbage, but the signal it gives is **"can we lie about being a Mac from a Linux box?"** Today the honest answer is "no, not at depth." That's a real product limitation, not a CI artifact. It belongs on a wall-of-shame somewhere, not as a green/red blocker on every PR. It is also evidence that **the matrix really wants `linux-*` profiles when it runs on Linux**, which we don't have yet.

### GitHub Actions macOS runners

- Pricing: `$0.062/min` wall-clock, *and* macOS minutes consume the included pool at a **10×** multiplier ([CostOps Guides](https://costops.dev/guides/how-github-actions-billing-works)). For a private repo on the Free plan, the 2,000 included minutes hold ~200 wall-clock minutes of macOS time — one and a half hours, total, per month. For a public repo macOS standard runners are free at billing time, but they are also slower to provision and have deeper queue depth.
- What they buy us: an honest Class C run for mac-* profiles, because the underlay is actually a Mac. But the egress IP is still a datacenter IP (Microsoft-owned macOS runner fleet); a smart bot detector that weights datacenter ASNs would still see "Apple-claiming UA from an Azure ASN," which is itself an inconsistency.
- **Verdict: not worth it.** macOS Class C runs belong on the maintainer's actual box (which is on a residential broadband IP, and where the falconproxy mobile proxy is one shell variable away). A GHA macOS runner costs real money to give us a Class C result whose IP layer is still wrong. Skip it.

### Self-hosted runner on a VPS

- **What it would actually have to be.** A self-hosted runner is just a small agent the maintainer installs on a box of his choosing. To be useful for Class C the box has to satisfy *two* properties: the OS underlay needs to match the spoofed profile family (Linux box for `linux-*` profiles, Mac box for `mac-*` profiles), AND the egress IP has to look like a real user (residential / mobile carrier ASN, not Hetzner/DigitalOcean/Linode). A vanilla Hetzner CX21 is a datacenter IP. A Mac mini at home behind the user's residential ISP, or a Linux box that egresses through the falconproxy mobile proxy, is what we actually want.
- **Cost shapes.** A Hetzner CX22 or equivalent runs about $5–7/month, but its public IP is a datacenter IP and gives no Class C uplift over GHA. A residential-proxy frontend (BrightData/Soax/IPRoyal residential plans) is $50–500+/month — wildly out of scale for an OSS project. The honest answer is: **the residential VPS isn't really a VPS; it is the maintainer's own home network**, or a tiny box at home, or a Mac mini.
- **Security model — critical.** A self-hosted runner on a **public** repo is a textbook compromise vector. From GitHub's own security hardening guide: "Self-hosted runners should almost never be used for public repositories on GitHub, because any user can open pull requests against the repository and compromise the environment" ([GitHub security hardening](https://github.com/github/docs/blob/962a1c8dccb8c0f66548b324e5b921b5e4fbc3d6/content/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions.md), [OWASP cheat sheet](https://cheatsheetseries.owasp.org/cheatsheets/GitHub_Actions_Security_Cheat_Sheet.html)). The Shai-Hulud worm of late 2025 [used exactly this vector at scale](https://www.sysdig.com/blog/how-threat-actors-are-using-self-hosted-github-actions-runners-as-backdoors). Two acceptable shapes for this project:
  1. **Self-hosted runner restricted to `on: push` to `main` only** (no `pull_request`, no `workflow_dispatch` from outside collaborators), behind `Require approval for all outside contributors`, with a runner group scoped to just this repo.
  2. **Not a runner at all** — a tiny `release-smoke.sh` the maintainer runs locally before tagging a release, hitting the matrix through the falconproxy mobile proxy. Same artifact (a JSON report on the egress IP and DAB verdicts), zero attack surface.
- **Verdict: option 2.** A self-hosted runner buys nothing the local matrix can't, while adding a real attack surface that the project's threat model (a stealth-browser tool maintained by one person) is poorly equipped to defend. Revisit this when the project has a maintainer team and a private fork for paid customers.

### Managed runner services (BuildJet, Blacksmith, Namespace, Depot)

- For Class A and B: marginally faster Linux at marginally lower cost than `ubuntu-latest`. None of them changes the IP-class story for Class C (they're all datacenter clouds). The MCP test suite isn't slow enough to justify the integration cost.
- **Verdict: skip until the test suite crosses ~30 minutes per CI run.**

### Windows coverage

- A full Windows matrix is overkill — Chromium's behavior is essentially identical across Win/Linux/Mac for the surfaces we test, and we have no Windows-specific code paths in `MithwireBrowser`.
- Cheapest meaningful signal: **one Windows GHA job that runs only the Class A unit tests** (`uv run pytest -m 'not stealth_e2e'`). At Windows 2-core `$0.010/min` it's roughly half a cent per CI run; on a public repo, free. That catches `pathlib`/`asyncio.subprocess`/Windows-path bugs without paying for Chromium-on-Windows.
- Class B on Windows is genuinely useful but not free: add it as a manual `workflow_dispatch`-triggered job that the maintainer runs before each minor release.

### Recommended default + fallback

**Default:** GHA `ubuntu-latest` for Class A (every push) + Class B (every push, the `e2e-application` job). Add a Windows `ubuntu-latest`-equivalent — i.e. `windows-latest` running Class A only. Drop Class C from the required-to-merge set; either move it to a `workflow_dispatch`-only manual job, or keep it on push as a `continue-on-error: true` informational job that publishes the matrix table to the job summary. The artifact stays visible; the build doesn't red because of a known cross-OS leak.

**Fallback / "I really want a green Class C in CI":** a Mac mini or M-series home machine registered as a self-hosted runner scoped to this repo only, configured for `push` to `main` and `workflow_dispatch` only (never `pull_request`), running the matrix against `mac-*` profiles through the maintainer's residential IP. This is the only configuration where the "deviceandbrowserinfo says human" gate becomes meaningful as a CI signal. Don't ship it until there are `linux-*` profiles for the Linux-deployed users to test against too.

## 4. Docker — yes or no, and why

User's specific question: "could it affect browser antidetect, can we spoof it?"

**Short answer: ship Docker, with the same sandbox stance the codebase already takes. It is not materially worse than bare-metal Linux for the surfaces a JS-sandboxed page can probe, as long as you do the boring things right.**

What a JS-sandboxed page can actually see that differs between bare-metal Chrome and Chrome-in-Docker:

- **`navigator.hardwareConcurrency`.** Reflects the cores Chromium sees. Cgroup CPU limits (`--cpus=2`, Kubernetes CPU limits, ECS reservations) reduce this *visibly* ([Secure Tools Guide on hardwareConcurrency fingerprinting](https://securetoolsguide.com/hardware-concurrency-fingerprinting-how-cpu-core-count-ident/)). Unusual values (e.g. `3`, which no consumer CPU has) narrow your population dramatically. We already let `FingerprintConfig.hardware_concurrency` pin this through CDP `Emulation.setHardwareConcurrencyOverride`, so a Docker deployment that pins it to `8` looks like any other 8-core Chrome.
- **`navigator.deviceMemory`.** Returns a power-of-two rounded GB ([BotBrowser navigator fingerprinting writeup](https://botbrowser.io/en/blog/navigator-properties-fingerprinting/)). Heavy cgroup memory limits don't usually flip this (Chrome reads the host's total RAM, not cgroup limits, on most kernels), but `FingerprintConfig.device_memory` overrides it via injected JS regardless. Already handled.
- **WebGL vendor/renderer.** On a headless container with no GPU, Chrome falls back to SwiftShader, and a `SwiftShader` renderer string is itself a strong bot tell (the Open Bullet 2 analysis on DAB explicitly cites it — see [analyze-open-bullet2-puppeteer-mode](https://deviceandbrowserinfo.com/learning_zone/articles/analyze-open-bullet2-puppeteer-mode)). `FingerprintConfig.webgl_vendor` / `webgl_renderer` rewrite this in both main and worker scopes. Already handled.
- **Kernel version / platform leakage via UA-CH high-entropy hints.** The values returned by `userAgentData.getHighEntropyValues(['platformVersion', ...])` come from Chromium itself, not from `uname`. Docker's "host kernel, container userland" model means the kernel version is the host's; Chromium doesn't expose a kernel-version field over UA-CH (`platformVersion` reflects the Chromium-encoded OS major version, not `uname -r`). So this is invisible from a page.
- **`/.dockerenv`, `/proc/1/cgroup`.** Filesystem-only signals. Pages cannot read these; only server-side Node/Python code can (it's how libraries detect "am I in a container" — see the [browser-use Docker detection issue](https://github.com/browser-use/browser-use/issues/4650)). Irrelevant from a fingerprint angle.
- **The `--no-sandbox` tell.** This is the big one. Most Docker-in-CI Chrome recipes silently add `--no-sandbox`, and an `--no-sandbox`-launched Chrome shows a yellow "unsupported command-line flag" banner that bot detectors directly observe via the browser chrome region screenshot (and indirectly via subtle behavior changes). `MithwireBrowser.start()` already refuses this fallback explicitly. **The right Docker recipe is to keep the sandbox enabled** by either running with `--cap-add=SYS_ADMIN` + `--security-opt seccomp=path/to/chrome.json`, or — cleaner — running as a non-root user with unprivileged user namespaces enabled at the host level. Chromium docs cover this; modern Debian-based images "just work" if the kernel allows unprivileged userns.
- **mDNS hostnames, `/dev/random` entropy, timing.** Theoretical leaks. None observable from a same-origin page at meaningful scale. The Crypto subtle timing argument is a paper attack, not a real-world detector.

What mature stealth stacks actually ship as their deployment unit:

- **Camoufox** (Firefox-fork, C++-level spoofing) has an [official Docker build flow](https://github.com/daijro/camoufox) and a thriving ecosystem of community Docker servers like [`camofox-browser`](https://github.com/redf0x1/camofox-browser).
- **`undetected-chromedriver`** does not ship an official Docker image, but every production deployment I can find runs it in containers — usually a Debian-slim base with Chrome and the Python script.
- **`puppeteer-stealth` / `playwright-stealth`** ship Node packages, and the dominant deployment shape is Docker (Browserless, Browserbase, every "scraping API" startup runs them in containers).
- **`mithwire` upstream** doesn't ship Docker, but its README explicitly recommends Linux+container deployment for headless scale.
- **`Patchright`** (the merged result of the browser-use stealth experiments) ships containers.

**Concrete recommendation.** Ship `mithwire-mcp` as a Docker image (section 5 spells out the shape) and document it as the recommended VPS deployment path. **Do not** turn on `--no-sandbox` in the image. **Do not** apply cgroup CPU/memory limits unless you also pin `hardware_concurrency` / `device_memory` in the session's `FingerprintConfig`. Don't bother shipping the image as "anti-detect optimized" — the spoofing layer is what does that work. The image is just a convenient way to run the MCP next to a residential proxy.

## 5. Packaging

Current state: engine is `mithwire` 0.50.3 with no PyPI release evident in the engine repo (no release workflow, no published wheel) and the MCP's `pyproject.toml` pulls it from `../mithwire` via `[tool.uv.sources]`. MCP is `mithwire-mcp` 0.1.0 with floor `mithwire>=0.50,<0.60`. The README's install URL still references the *monorepo* path (`#subdirectory=packages/mithwire-mcp`), which is **stale** after the repo split — it's a pre-existing bug worth fixing soon, but not the strategy question.

### Engine packaging

- **Publish `mithwire` to PyPI now.** It's a fork with real users via this MCP; not shipping it forces every MCP install to either build from a git URL or run from a local checkout. Take the version that's there (0.50.3), tag it, and publish.
- **Naming.** Keep `mithwire` — it's already in `pyproject.toml`, the upstream package is `mithwire`, the "reforged" suffix is the project's identity. The import name stays `mithwire` so existing `import mithwire as uc` calls keep working.
- **Version compatibility contract.** The current MCP pin `>=0.50,<0.60` is a healthy 6-month window. Document it as: **engine 0.50.x is the supported floor; minor bumps (0.51, 0.52) are tested in CI; majors (0.60, 1.0) are breaking and require a coordinated MCP minor bump.** Encode the contract in CI by running the matrix against both the floor pin (`==0.50.3`) and the latest (`>=0.50,<0.60`) — catches the case where a 0.51 release silently breaks `MithwireBrowser._cdp_emulation.set_user_agent_override` kwargs.
- **Release flow.** Engine first. Tag → CI builds wheel → publishes to PyPI. Then bump the MCP floor in a separate PR that runs the full CI matrix against the new floor before merging.

### MCP packaging

Two artifacts, both maintained:

1. **PyPI: `mithwire-mcp`.** Primary install path for Cursor / Claude Desktop / any local MCP client. `pipx install mithwire-mcp` puts a `mithwire-mcp` binary on PATH; the `mcp.json` `command` line is unchanged. This is what the README already documents (modulo the stale subdirectory URL).
2. **Docker: `ghcr.io/codeisalifestyle/mithwire-mcp:<version>` and `:latest`.** Opinionated bundled image: Debian-slim base, Chromium installed (NOT Chrome — the licensing on Google Chrome makes Chromium the right base), sandbox enabled, the MCP binary as `ENTRYPOINT`. Exposes stdio over a `docker exec` or via the streamable-HTTP transport (`--transport streamable-http`) for remote MCP clients. **This image is the answer to "where do I deploy the MCP on a VPS without spending an hour on Chrome dependency setup."**

### Browser distribution

- **PyPI / pipx install: BYO Chrome.** Same as today — `MithwireBrowser` auto-discovers via the env `CHROME`, the macOS app bundle, and the Linux `google-chrome`/`chromium` PATH lookups. Users on macOS already have Chrome. Users on Linux desktop have it (or install one apt away). The MCP doesn't need to be in the business of shipping a browser to laptops.
- **Docker image: bundled Chromium.** Reproducible, sandbox-enabled, version-pinned. The MCP image and the Chromium it ships are versioned together (e.g. the `0.1.0-chromium-122` tag), so a user choosing this path gets a tested combination.
- **Trade-off:** the docker image is ~400-600 MB on disk (Chromium is heavy). That's the standard cost of a stealth-automation container — Camoufox's debloated Firefox is the outlier at ~200 MB, and Camoufox is a forked browser engine. We're shipping stock Chromium, so we pay the stock size.

### Cursor / Smithery / marketplace specifics

- **Cursor.** The `mcp.json` format expects `command` + `args` for stdio (or `url` + `headers` for streamable-HTTP). Our existing `mithwire-mcp --transport stdio` invocation already fits. Document both the `pipx install` form and a `docker run` form in the README — Cursor supports `command: "docker"` with `args: ["run", "-i", "--rm", "ghcr.io/..."]` for users on the VPS / WSL path.
- **Smithery.** The registry is real and increasingly the default discovery path for MCP servers, but it's npm-centric for "one-click install" — the easiest publish path for a Python server is either a URL-based hosted endpoint or an MCPB bundle (`smithery mcp publish ./server.mcpb -n codeisalifestyle/mithwire-mcp`, per [Smithery CLI docs](https://smithery.ai/docs/concepts/cli)). **Don't optimize for Smithery yet.** PyPI + the README's manual `mcp.json` snippet covers most of the realistic install surface. Add a Smithery listing once the engine is on PyPI and there's a stable v0.2 to announce.
- **Cursor's own marketplace / Plugin system.** It exists ([Smithery integration discussion](https://forum.cursor.com/t/integration-with-mcp-smithery/57005)) but isn't required. A README snippet with a copy-pasteable `mcp.json` block is enough today.

### Release flow (engine + MCP coupling)

Concretely:

```
# engine release
cd mithwire
git tag v0.50.4 && git push --tags
# CI publishes the wheel to PyPI

# MCP catches up
cd mithwire-mcp
$EDITOR pyproject.toml   # bump floor to >=0.50.4,<0.60
# Remove (or comment) [tool.uv.sources] in the release commit if it still
# points at ../mithwire — published wheels strip it, but keeping it
# in main muddies the "what does `uv sync` actually do for a contributor".
uv lock --upgrade-package mithwire
uv run pytest -m 'not stealth_e2e'
git commit -am "bump engine floor to 0.50.4"
# PR -> green CI -> merge
git tag v0.1.1 && git push --tags
# CI publishes mithwire-mcp wheel + Docker image
```

Two CI things need to exist for this to be smooth:

- An engine release workflow on the engine repo. There isn't one today (no `.github/workflows/` in `mithwire`); add a trusted-publisher OIDC workflow that builds the wheel on tag and pushes to PyPI. Same shape on the MCP repo for releases.
- The MCP CI grows a "two-axis dependency check" job: matrix over the floor pin and the latest pin, runs Class A + B against both. This is the safety net that lets you bump the engine floor confidently.

## 6. Action plan

1. **Demote the detection-matrix CI job from a hard gate to informational.** Add `continue-on-error: true` on the job and rename it in the job summary to "Anti-detect matrix (informational; gate is local pre-release)." Keep the artifact upload and the Markdown summary so regressions are visible at a glance. Justification: the current failure pattern is a real cross-OS leak, not a CI bug, but it's a known limitation that shouldn't block merges. **Effort: S. No dependencies.**

2. **Add a Class A Windows job.** `windows-latest` + `uv sync --group dev` + `uv run pytest -m 'not stealth_e2e' --maxfail=1`. Catches Windows-only path/asyncio bugs at trivial cost. **Effort: S. No dependencies.**

3. **Move the matrix to a `make matrix` target + a pre-release script + a `pre-commit` config that only runs the fast suite.** The matrix should be one shell command the maintainer (or a release script) invokes, ideally with `--proxy "$FALCONPROXY"` for the realistic mode. Document it in `RELEASING.md`. **Effort: S. Depends on step 1 in spirit (the matrix is now an offline tool, not a CI job).**

4. **Publish `mithwire` to PyPI as 0.50.3.** Add an engine-repo release workflow using trusted-publisher OIDC, tag v0.50.3, ship. **Effort: M. Blocks step 5.**

5. **Remove the `[tool.uv.sources]` override from `main` once the engine is on PyPI.** Document the "uncomment for cross-layer dev" pattern in the dev-flow skill (it already does) but keep `main` clean so a contributor's `uv sync` Just Works. **Effort: S. Depends on step 4.**

6. **Fix the stale README install URL.** The current `pipx install "git+...mithwire.git#subdirectory=packages/mithwire-mcp"` references the pre-split monorepo. After step 4, the canonical install is `pipx install mithwire-mcp`. **Effort: S. Depends on step 4.**

7. **Add a `dependency-floor` CI matrix.** Run the existing `unit-fast` and `e2e-application` jobs against both `mithwire==0.50.3` (floor) and the unbounded `>=0.50,<0.60` (latest). Catches an engine release that breaks the MCP's CDP usage. **Effort: M. Depends on step 4.**

8. **Build and publish a Docker image.** `Dockerfile` in the MCP repo: `python:3.12-slim` base + `chromium` apt package + the MCP wheel. Build on tag in CI, push to `ghcr.io`. Default `CMD` runs `mithwire-mcp --transport stdio`. Add a small docker-compose example to the README for the VPS-with-residential-proxy deployment story. **Effort: M. Depends on step 4 (so the wheel can be pip-installed in the image) but can be designed in parallel.**

9. **Introduce `linux-*` profile presets.** Without them, a Linux operator running this MCP has no preset that lets them honestly claim a Linux identity — they're forced into the cross-OS leak the matrix is currently catching. Two or three `linux-*-residential` profiles (Linux/X11, Wayland, common screen sizes) are a small fingerprint-config exercise and immediately make the matrix actionable on any future Linux runner. **Effort: M. No dependencies on infra steps; can ship anytime.**

10. **Document the same-OS-family rule prominently in the README** (it currently lives only in the dev-flow skill, which is `.gitignore`d). It's the most user-affecting stealth caveat we have, and quietly burying it is a worse outcome than letting the matrix go red. **Effort: S. No dependencies.**

11. **Defer Smithery, defer a self-hosted runner, defer GHA macOS / Windows Class B.** Revisit in 3–6 months once there is a v0.2 release, a Docker image with operator feedback, and either a contributor base big enough to justify a runner group or a paid offering that funds the security overhead. **Effort: 0 today; the discipline is *not* doing them prematurely.**

## 7. Open questions / things I couldn't determine

- **Is `hasInconsistentWorkerValues` the only DAB flag that fails on `mac-*` from a residential IP, or does it survive only because GHA's Azure IP happens to not fire the other flags?** DAB's `details` map has flags I can identify from public writeups (`isBot`, `hasBotUserAgent`, `isHeadlessChrome`, `isAutomatedWithCDP`, `hasInconsistentWorkerValues`, `hasInconsistentClientHints`, `isWebGLInconsistent`, `hasInconsistentGPUFeatures`, `isIframeOverridden`, etc.) but no explicit `isDataCenterIp`-style flag in the same surface. **How to find out:** run the current matrix locally on macOS with `--proxy "$FALCONPROXY"` (mobile, residential-class egress) and read the `details` map verbatim. If the only failing flag stays `hasInconsistentWorkerValues`, the conclusion in §3 holds. If the failure pattern changes when the IP changes, my "IP is not the gate" claim is wrong and a residential self-hosted runner climbs back up the priority list.

- **Which exact worker fields are leaking on Linux underlay?** The matrix gives us "isBot=true, flags=hasInconsistentWorkerValues" but not the per-field worker-vs-main diff. The Open Bullet 2 / PuppeteerExtraSharp analysis on DAB ([analyze-open-bullet2-puppeteer-mode](https://deviceandbrowserinfo.com/learning_zone/articles/analyze-open-bullet2-puppeteer-mode)) shows `webGLVendor`, `webGLRenderer`, `languages`, `platform`, and `hardwareConcurrency` as the typical leak set. Our `_worker_bootstrap_js` covers `languages`, `deviceMemory`, `hardwareConcurrency`, and WebGL via the constructor-wrap, but **only for `new Worker()` non-module workers** — DAB likely instantiates a module worker or a Blob-URL worker that bypasses the wrap. **How to find out:** run the matrix in a debug mode that dumps DAB's `workerData` (the same field the Open Bullet writeup parses), or add a dedicated probe in `baseline_probe.py` that runs `NAV_PROBE` inside both a classic and a module worker and diffs the result.

- **Does Cursor's `cursor://` MCP install URL flow (the "Add to Cursor" button) work for Python servers?** The TrueFoundry 2026 MCP-in-Cursor guide and the Smithery docs both describe the flow as primarily for `npx`/Node servers, with Python servers expected to be configured manually. **How to find out:** publish v0.1.1 to PyPI, attempt to generate an "Add to Cursor" deeplink, and see whether Cursor accepts a `pipx`-installed Python binary cleanly. Not a blocker for the strategy in this document, but informs whether a Smithery listing has user-experience value beyond the README snippet.

- **What's the realistic CI cost on the project's actual plan?** I've assumed Free + public repo throughout (which is free for standard Linux runners). If the repo is private or moves to private, the monthly minute pool becomes the binding constraint and the Docker-image-build CI step in particular needs to be sized carefully. **How to find out:** the maintainer knows.
