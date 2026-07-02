# 错误报告 / 反馈机制 规划

目标：有了真实用户后，把「报错与反馈」从私信收拢到可跟踪、可复用、尊重隐私的正规渠道，并支撑「保持更新」的节奏。

## 隐私底线（先定死）

本项目是「绕过登录」性质的工具，**绝不做任何自动遥测 / 后台崩溃上报 / 静默数据回传**。
所有诊断信息都由用户**手动**提交、内容由用户决定。这条是硬约束，任何反馈功能都不得违反。

## 现状（v0.1.2 已落地）

- **GitHub Issue 模板**：`.github/ISSUE_TEMPLATE/` 下
  - `bug_report.yml`：结构化收集 版本 / macOS+芯片 / provider / 复现步骤 / 报错文字 / 日志，并强制勾选「已删除密钥」。
  - `feature_request.yml`：功能与「想支持的 API / Skill / MCP」。
  - `config.yml`：把安装/使用类问题先引导到 README。
- **面板内入口**：右下角「反馈 / 报 bug」直接跳预填的 Bug 模板；「日志」一键在访达打开 `~/.csswitch/logs/`。
- **版本可见**：面板页脚显示当前版本，「检查更新」比对 GitHub 最新 Release。
- **日志**：代理与沙箱子进程的 stdout/stderr 落 `~/.csswitch/logs/`（`proxy.log`、`sandbox.log`，0600）。
- **README**：新增「反馈与报错」小节 + 隐私声明。

## 下一步（按优先级）

1. **日志脱敏导出**：一键「导出诊断包」= doctor 输出 + 日志尾部，且**自动把疑似 key/token 打码**后再落一个 zip，降低用户误贴密钥的风险。
2. **报错更友好**：面板把后端错误归类成人话（端口占用 / key 无效 / 缺 python·node / Science 未安装），每类附「怎么办」一句话与对应链接。
3. **崩溃可见（本地）**：app 自身 panic 时写一份本地崩溃日志到 `~/.csswitch/logs/`，用户可在 bug 反馈里附上（仍不自动上报）。
4. **更新提示常态化**：启动时静默查一次最新 Release（失败无声），有新版在页脚点一下小红点提示，不打扰。
5. **常见问题沉淀**：把重复的私信问题整理进 README FAQ 或 GitHub Discussions。
6. **可选·真·自动更新**：走 `tauri-plugin-updater`，需要一套更新签名密钥 + 托管 `latest.json`；等有 Apple Developer ID / 公证后再上，属较后期。

## 更新节奏

- 语义化版本：修 bug 走 patch（0.1.x），加功能走 minor（0.x）。
- 每次发布：改 → `cargo test` + 离线套件 + 启动冒烟 → gitleaks（历史+工作树）→ 打包 ad-hoc 签名 dmg → `gh release create vX` 附 dmg。
- README 下载链接固定指向 `releases/latest`，无需每版改文档。
