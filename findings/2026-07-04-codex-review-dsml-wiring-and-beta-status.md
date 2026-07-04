# 2026-07-04 会话记录：Codex 审 + DSML 接线 + v0.3.0-beta.1 状态订正 + PR 分诊

> 本文件记录 2026-07-04 一次会话的处置结果与**当前待决状态**，供后续接手。要点：DSML shim 已端到端接进代理并实机验证；但对外发出的 `v0.3.0-beta.1` **名不副实、待撤重发**；3 个 open PR 待处置；`main` 已加分支保护。

## 1. Codex 审 6 条 → 逐条评估

| 条目 | 结论 | 处置 |
|---|---|---|
| P0 DSML shim 从未接进代理（`csswitch_proxy` 从不 import，三路径全透传，绿测只覆盖孤立模块） | 属实 | **已修**：接进 `_handle_anthropic`，`shim_mode()` 定 off/detect/rewrite，仅 deepseek+有 tools 时介入 |
| P1 EOF `flush_tail` 形参未用（末帧无空行→吞 message_stop，实测 `b''`） | 属实 | **已修**：`finalize()` 补吐残留末帧 |
| P1 非法布尔臆断 false 过校验（`maybe`→`{flag:false}`） | 属实 | **已修**：只认 true/1/yes、false/0/no，余留字符串判非法整块作废 |
| P1 rewrite 合法 DSML 示例可能被误执行 | 已知阶段二闸门 | **默认 off 化解**，rewrite 仅供显式验证 |
| P1 lib.rs:1524「健康≠登录可用/应打 `/auth/status`」 | **架构性驳回** | CSSwitch 登录是本地虚拟伪造，打 claude.ai/auth/status 会破坏越过机制且可能 hang；正解是 0.2.1 已发的 `login_intact` 本地自洽校验。仅「丢弃 stop 错误」是小 nit → 归多 profile 轨道 |
| P2 `--auth-token` argv 泄漏（`ps` 可见） | 属实但轻 | 代理已支持 `CSSWITCH_AUTH_TOKEN` env，2 行可改；触未提交多 profile lib.rs → 归多 profile 轨道 |

## 2. DSML shim 接线 + 实机验证

- **接线**（`proxy/csswitch_proxy.py`）：启动时 `SHIM_MODE = dsml_shim.shim_mode(PROV_NAME, PROV)`（读 `CSSWITCH_TOOLUSE_SHIM`，默认 off）。`off`=字节透传（零回归）；`detect`=透传+`DsmlDetector` 遥测；`rewrite`=流式 `DsmlStreamRewriter` / 非流式 `rewrite_nonstream_body`。
- **新增测试**：`test/test_proxy_dsml_e2e.py`（起真实代理子进程打假上游，证明 rewrite 泄漏→tool_use、off 逐字）。
- **清洁无上下文 agent 实机验证 = READY-WITH-CAVEATS**：e2e 4/4；off/detect 双向逐字（零回归）；rewrite 端到端泄漏→tool_use；**真实 DeepSeek 正常工具调用经 shim 无损往返**（原生 tool_use 保真、不臆造）；真实泄漏偶发未复现。抓出 1 真 bug：**rewrite 非流式无泄漏时 `rewrite_nonstream_body` 无条件 json 往返 → 不逐字 + 遥测误报** → **已修**（`changed=False` 原样返回原字节）。
- 全绿：python 90 / rust 113 / node 5 / bash / gitleaks 0。session 新 commit `5038de4`/`8ae7238`/`e8fe92c`（分支 `feat/dsml-tooluse-shim` 已 push）。

## 3. ⚠️ v0.3.0-beta.1 名不副实，待撤重发

打 tag 时把 DSML 分支底下**已提交的 relay + 多 profile** 一并扫进了 prerelease：

- `git diff origin/main v0.3.0-beta.1` = **+5092 行 / 24 文件**：relay(proxy 2 处) + 已提交多 profile(`config.rs +670`/`lib.rs +1523`/`templates.rs +316`/lifecycle/proc/scratch/config_legacy) + DSML。**绝非「DSML-only」**。
- **两个真问题**：① CHANGELOG `0.3.0-beta.1` 段谎称「桌面 app 本版无改动」= 错（实际桌面侧 ~+2900 Rust/+780 前端）；② tag 里多 profile 是**较早已提交版本**，缺工作树 +470 行未提交真机修复（RM-04/06/13、native 无效 key 拦截），即发了个 `#8` 自标「未 ready」的半成品多 profile。
- **拔 node/python 治本本版 0 进度**（proxy 仍 python、伪造器仍 node）。
- **待用户决 A/B（都需先撤当前 beta 再重发）**：
  - **A** 从 `main` 重切**真·DSML-only**（注意 DSML 的 proxy 改动叠在 relay proxy 上，往无 relay 的 main 摘需调和 `_handle_anthropic`）。
  - **B** 有意「大预览版」：补齐多 profile 未提交修复 + 用户真机复测多 profile + 重写 CHANGELOG 如实描述。
- 撤 release+tag（先 export 代理）：`gh release delete v0.3.0-beta.1 --cleanup-tag`（或 `-y` + `git push origin :refs/tags/v0.3.0-beta.1`）。

## 4. 3 个 open PR 分诊（用户想收 #7+#4、愿接受贡献）

全部 fork→`main`（main 现停 v0.2.1 时代，无 relay/多 profile/DSML）：

- **#10** 本地 bio 连接器 docs+安装脚本（yeqingmo，**MERGEABLE**）：脚本已审 = 守铁律（拒真实 `~/.claude-science`、拒 8765、只连沙箱、幂等），解「虚拟登录下 HCLS 托管连接器 401 失效」的真缺口 → **可直接收**（合前再过一遍脚本后半段 API 调用）。
- **#7** WSL2/Docker 部署（visail，CONFLICTING，Closes #6）：用户想要。落地=请他 rebase 到最新 main + 我审容器铁律合规。收它=接下 Windows/Docker 维护负担。
- **#4** 中转站 relay（chimeraHHH，CONFLICTING）：**与自研 relay/多 profile 重叠**（同一批文件）。需选底座：**A（建议）自研多 profile 为底、credit 贡献者（吸收 WAF UA 绕过 / `/v1/models` 归一化 / 裸 id 贴合）**；B 用 #4 先合再返工（不划算）。
- **真瓶颈**：先把自研功能线合进 `main` 形成新基线，再让人 rebase 到新 main。「rebase 到 0.3.0」是伪需求（0.3.0 不在 main）。回复外部 PR = 定处置后草稿给用户过目才发。

## 5. main 分支保护已开

`gh api -X PUT .../branches/main/protection`：`allow_force_pushes=false` + `allow_deletions=false` + `enforce_admins=true`；无 required PR / 无 required status check（直提 main 照常，只挡强推与删分支）。以后要强推 main 需先临时关规则。

## 6. 后续（同日）：用户选 B「大预览版」，已执行

- **已发 `v0.3.0-beta.2` 大预览版**（relay + 多 profile + DSML，prerelease + dmg `CSSwitch_0.3.0-beta.2_aarch64.dmg` 3.7M），撤回 beta.1。新增 commit：`acdcdd8`（多 profile 真机/外审修复落库）、`c306607`（release，CHANGELOG 如实描述）、`f02d659`（gitleaks allowlist 修 `sk-definitely-invalid` 假阳性）。全绿 cargo113/clippy0/run_all90/gitleaks0。
- **已装机 + 清理**：`/Applications/CSSwitch.app` 更新到 beta.2，全机冗余拷贝（桌面测试副本、旧 Acceptance.app、target 构建产物）已删，只留 /Applications 一份。doctor 0/0。
- **实机测试步骤**：`test/RM_0.3.0-beta.2_ALL_FEATURES.md`（覆盖核心冒烟 / 多 profile RM-04/06/13 / relay 四家 / DSML detect+rewrite+代理层 e2e）。
- ⚠️ 真实 Science 全程在 8765，未碰。

## 待决清单（更新）
1. ~~v0.3.0-beta 方向~~ → **已定 B，beta.2 已发**。
2. **用户真机跑 `test/RM_0.3.0-beta.2_ALL_FEATURES.md`**（多 profile/relay/DSML 全测；登录只用户做）。
3. 据真机结果决：DSML / 多 profile / relay **往 main 合的顺序**。
4. PR #4 底座：自研多 profile（建议）/ #4；PR #7 WSL2 收（需 rebase + 铁律审）；PR #10 bio 连接器可收。
5. 审 #10 脚本尾段 + 起草 3 条 PR 中文回复（用户过目才发）。
