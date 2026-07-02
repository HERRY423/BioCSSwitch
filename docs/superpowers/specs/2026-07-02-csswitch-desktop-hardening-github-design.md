# CSSwitch 设计文档：菜单栏桌面 app + 代码加固 + GitHub 上传准备

日期：2026-07-02
状态：待用户审阅
范围：三块合一份大计划（Tauri 菜单栏 app + GPT 审查的 8 个问题 + GitHub 上传准备）

---

## 1. 背景与目标

CSSwitch 让 Claude Science 的模型推理走第三方 API（DeepSeek 默认、Qwen 备选），保留 Science 的「AI Jupyter」体验，把推理换成便宜或开源的模型。当前形态是三个命令行脚本：翻译代理 `proxy/csswitch_proxy.py`、虚拟 OAuth 伪造器 `scripts/make-virtual-oauth.mjs`、沙箱启动器 `scripts/launch-virtual-sandbox.sh`。

本次要做三件事：

1. 给这套命令行工具套一个 macOS 菜单栏桌面 app（Tauri，风格参考 CC Switch），实现一键越过登录、配置 DeepSeek key、起停代理与沙箱、看状态。
2. 修掉 GPT 代码审查发现的 8 个问题（4 高 4 中）。
3. 准备 GitHub 上传：README、LICENSE、密钥扫描、忽略规则、免责声明。

核心目标：用最小的新增代码把已验证的能力包成好用的桌面 app，同时先加固再套壳，避免 GUI 继承已知 bug。

## 2. 范围

含：
- Tauri 菜单栏 app（Rust 后端 + 网页前端）。
- 8 个审查项的修复。
- GitHub 上传所需的文档与卫生工作。
- 重写针对 `csswitch_proxy.py` 的回归测试。

不含：
- 不重写代理、伪造器、启动器的核心逻辑，只加固与包装。
- 不做 Windows/Linux 版（Science 与沙箱逻辑都是 macOS 专属）。
- 不打包 Python/Node 解释器进 app（v1 依赖本机已装的 python3 与 node，缺失时给清晰报错）。
- 不碰真实 `~/.claude-science` 与端口 8765（铁律）。

## 3. 决策记录

已定：
- GUI 形态：macOS 菜单栏 app。
- 技术路线：Tauri（Rust 后端 + 网页前端）。
- key 存储：参考 CC Switch，存 app 自己目录下的本地配置文件（明文 JSON），不走钥匙串。app 目录取 `~/.csswitch/`，对齐 CC Switch 的 `~/.cc-switch/`。完整文件安全要求（不止 0600）：目录 0700；读写前 `lstat` 拒绝符号链接；写用「临时文件 + 原子 rename」，写后复核 chmod 0600；目标文件已存在时先复核并重置为 0600；key 绝不写进任何日志（日志脱敏）；前端读配置时永远只回显掩码（如末 4 位），绝不把完整 key 送回前端。
- LICENSE：MIT。
- 免责声明：写。口径为个人与研究用途，推理请求不经过 Anthropic、与 Anthropic 无从属或背书关系、不提供任何担保。注意措辞避免「零 Anthropic 接触」这类绝对断言：Science 启动阶段仍会尝试访问硬编码的 profile/account 接口（api.anthropic.com），失败无害（见 CLAUDE.md 第三节第 5 点）。若要宣称完全零接触，须另加网络阻断并以抓包为验收，本次不做此承诺。

本轮已定（原为阻塞项，见第 12.1 节）：
- 公开边界：公开全量仓库 + 显著免责声明，含伪造器与逆向细节。伪造器进包，一键登录开箱即用。用户在知悉 DMCA/条款/个人关系潜在风险后选择承担。公开推送前仍过一遍 12.2 的法律/条款 go-no-go 检查点，作为最后可反悔的闸。

实现阶段实测项（不阻塞设计）：见第 12.3 节（Qwen 多工具 `any` 落点、path secret 与 base_url 拼接兼容性）。

## 4. 架构

菜单栏 app 是「进程管家」加「配置面板」。Rust 后端只负责编排：起停子进程、注入环境变量、读写配置、探活。已验证的越权与翻译逻辑仍留在 Python / Node / shell 里，作为子进程被调用。这样保住铁律护栏和已验证行为，Rust 侧代码量最小。

```
托盘图标  Tauri Rust 后端（进程管家）
   │
   ├─ 网页前端面板：DeepSeek key 输入 / provider 选择 / 一键越登录 / 起停 / 状态灯
   │
   ├─ 子进程①  csswitch_proxy.py    常驻，是 ANTHROPIC_BASE_URL 的目标
   │            后端把 key 经【子进程环境变量】注入，绝不作为命令行参数（避免 ps 泄露）
   │
   ├─ 子进程②  launch-virtual-sandbox.sh
   │            内部再调 make-virtual-oauth.mjs 写虚拟登录，然后 Science serve
   │
   └─ 本地配置  ~/.csswitch/config.json（0600）：provider keys、端口、上次选择
```

数据流（推理）：沙箱 Science 带着自造的虚拟 OAuth 启动，`ANTHROPIC_BASE_URL` 指向本地代理，代理剥离入站鉴权、换第三方 key、按 provider 决定透传或翻译，打到 DeepSeek 或 Qwen。

## 5. 组件分解

### 5.1 Rust 后端（进程管家）
- 职责：管理代理与沙箱两个子进程的生命周期；读写 `~/.csswitch/config.json`；把 key 以环境变量注入代理子进程；探活（代理 `/health`、沙箱 `/health`、上游连通性）；把沙箱 URL 交给系统浏览器打开。
- 接口（Tauri command，供前端调用）：
  - `get_config()` / `set_config(partial)`：读写配置。
  - `save_provider_key(provider, key)`：写 key 进 config.json。
  - `start_proxy()` / `stop_proxy()`：起停代理，返回 pid 与端口。
  - `one_click_login()`：确保代理在跑，起沙箱，返回 URL。
  - `stop_sandbox()`。
  - `status()`：返回代理、沙箱、上游三处健康。
- 依赖：Tauri shell/process API；本机 python3（优先 conda）与 node 的路径（配置或自动探测）；打包进 app 资源的 `proxy/` 与 `scripts/`。
- 隔离要点：绝不把 key 写进命令行；探活失败给可读错误；子进程 stderr 收集进 `~/.csswitch/logs/`。
- 生命周期规则（明确定义各边界情形）：
  - 重复启动：`start_proxy` 幂等，已在跑则复用不新起，面板反映现状。
  - 端口占用：起前探端口，被占则报明确错误（区分「上次没退干净」与「别的程序占了」），不盲目另起。
  - PID 记账与防误杀：记录子进程 pid、启动时的可执行文件路径、该次 auth secret；`stop_*` 前三者都核对（pid 存活、可执行文件仍是我们的代理/Science、secret 匹配），任一不符就拒绝 kill，避免 PID 重用杀错进程。
  - app 崩溃后重启：读上次记账，探活确认孤儿子进程身份后再决定接管或清理，不认领来路不明的进程。
  - secret 轮换：代理重启即换新 secret，沙箱的 `ANTHROPIC_BASE_URL` 必须随之更新（否则旧沙箱打不通），面板提示需要重开沙箱。
  - 退出 app：默认「退 app 时停代理、保留沙箱运行」，并提供「一并停沙箱」选项，默认值写进文档。

### 5.2 网页前端面板
- 职责：一个单页面板。字段：DeepSeek API key（密码框）、provider 下拉（deepseek 默认 / qwen）、代理端口、沙箱端口。按钮：保存、启动代理、一键越过登录、停止。状态区：三盏灯（代理 / 沙箱 / 上游）加最近日志尾巴。
- 接口：只调用 5.1 的 Tauri command。
- 依赖：无框架或极轻框架（原生 HTML/CSS/JS 即可，保持简单）。
- 边界：前端不碰任何密钥落盘逻辑，只把输入交给后端。

### 5.3 csswitch_proxy.py（加固后）
- 职责不变：Anthropic 入站，按 provider 透传（deepseek）或翻译（qwen）到上游。
- 加固见第 7 节 P1/P2 各条。
- 新增：启动时接受 `--auth-token <secret>`（或读环境变量），对入站请求校验 path secret（见 7.2）。

### 5.4 make-virtual-oauth.mjs（加固后）
- 职责不变：往沙箱 auth_dir 写一套本地自造的虚拟 OAuth（远期过期，不触发联网刷新 _refreshToken）。此处「不联网」仅指这套令牌本身不会引发刷新联网，不等于 Science 整体零联网。
- 加固：护栏改用 `fs.realpathSync` 跟随符号链接后再校验（见 7.1）。

### 5.5 launch/stop 脚本（加固后）
- `launch-virtual-sandbox.sh`：端口比较归一化为整数，数据目录用 realpath 比（见 7.7）。
- `stop-science-sandbox.sh`：按真实退出码报告，去掉吞错（见 7.6）。

## 6. 一键越过登录数据流

前端点「一键越过登录」后，后端顺序执行：

1. 检查 `config.json` 里有没有当前 provider 的 key，没有就报错让用户先填。
2. 检查代理是否在跑，没跑就 `start_proxy()`（生成一次性 path secret，注入代理与稍后的 `ANTHROPIC_BASE_URL`）。
3. 探活代理 `/health`，通过再继续。
4. 起沙箱：调 `launch-virtual-sandbox.sh --port <沙箱端口> --proxy-url http://127.0.0.1:<代理端口>/<secret>`。脚本内部写虚拟 OAuth 并 `Science serve`。
5. 轮询沙箱 `/health` 直到就绪或超时。
6. 取 UI URL，交系统浏览器打开（浏览器里已是登录态）。
7. 前端三盏灯转绿。

失败任一步就停下、亮红灯、把该步的 stderr 尾巴显示在面板上，绝不吞错。

## 7. Phase 0：现有代码加固（8 项逐条）

先做这一节，再盖 GUI。每条给：问题、修法、验证。

### 7.1 [P1] 符号链接绕过护栏（make-virtual-oauth.mjs:41）
- 问题：护栏只做字符串 `path.resolve`，随后删除目标目录所有 `.enc`。若 `.sandbox/auth` 是指向真实 `~/.claude-science` 的符号链接，字符串检查看不穿，凭证会写进链接目标，可能破坏真实登录。更进一步：即便目录护栏收紧，`encryption.key`、`active-org.json`、`<uuid>.enc` 这些叶子文件本身仍可能是指向真实凭证的符号链接，`writeFileSync` 会跟随链接覆盖目标。
- 修法：
  1. 目录：对 authDir 与其父目录先 `fs.realpathSync`（父目录存在时逐层解析，目录不存在则解析最近的已存在祖先再拼接），用解析后的真实路径再做「不等于真实目录」「在 .sandbox 下」两道校验。
  2. 叶子文件：每个写入目标（encryption.key、active-org.json、每个 .enc）与每个待删 .enc，写/删前先 `fs.lstatSync`，`isSymbolicLink()` 为真一律拒绝退出，绝不跟随。
  3. 写入用「临时文件 + 原子 rename」：写到同目录下 `.tmp-<rand>`（`O_CREAT|O_EXCL`，mode 0600），`fs.renameSync` 覆盖目标，写后 `fs.chmodSync` 复核权限，目录确保 0700。
- 验证：三条独立用例。(a) auth-dir 是指向别处的符号链接 → 拒绝、目标零改动；(b) encryption.key 预置为指向 /tmp 某文件的符号链接 → 拒绝、该文件零改动；(c) 正常沙箱目录 → 写入成功、都是普通文件（非链接）、权限 0600。

### 7.2 [P1] 代理鉴权（csswitch_proxy.py:282）
- 问题：代理不校验来源，本机进程可借代理持有的第三方 key 发请求。只监听回环不够。
- 威胁模型（明确边界，不过度声称）：path secret 能挡的是「网页 CSRF / 跨源请求」「其它 app 的误调用」「拿不到本进程参数与环境的低权限客户端」。它挡不住「同用户、能读到进程参数或环境变量的恶意本机进程」，因为 secret 会出现在启动脚本参数与 Science 的环境变量里，同用户进程通常能观察到。并且同一个同用户恶意进程本就能直接读 `~/.csswitch/config.json` 里的 key（0600 对同用户可读），所以「防同用户恶意软件」这个目标对 key 文件本身也不成立，属于本方案与 CC Switch 式明文存储共有的固有残余风险，文档如实标注、不假装解决。
- 修法：启动时生成一次性 secret。`ANTHROPIC_BASE_URL` 带 path 前缀 `http://127.0.0.1:<port>/<secret>`，Science 在其后拼 `/v1/...`；代理校验路径前缀，不符返回 403 且不接触上游。
  - 主方案：path secret（不依赖 Science 转发自定义头）。
  - 备用方案：若 SDK 对 base_url 带 path 拼接不友好，改自定义头注入（`ANTHROPIC_CUSTOM_HEADERS` 注入 `X-CSSwitch-Token`）。实现阶段先验主方案，不通再退备用。
  - secret 卫生：不写进代理日志、不回显进错误响应、URL 在面板显示时打码。
- 验证：带正确 secret 通过；缺/错 secret 得 403 且上游零调用；grep 代理日志与错误体断言不含 secret 明文。

### 7.3 [P1] 流中断写坏响应（csswitch_proxy.py:337）
- 问题：已发 200 加 chunked 后，读上游出错会掉进统一异常处理再发一份 502 JSON，客户端收到非法 chunked 流。已知 SSL EOF 抖动正会触发。
- 修法：`_handle_anthropic` 里加 `headers_sent` 标志。一旦进入流式并发出响应头，读上游出错时不再发新的 JSON 响应体：尽力发一个 Anthropic SSE `event: error` 帧，然后写终止块 `0\r\n\r\n` 干净收尾并记日志。未发头之前的错误仍走 502 JSON。
- 验证：mock 上游发一半就断，断言客户端收到的是合法 chunked 结尾或 SSE error 帧，不是拼进流里的 JSON。

### 7.4 [P1] Qwen 翻译丢强制工具（csswitch_proxy.py:213）
- 问题：`anthropic_to_openai` 不转换 `tool_choice`，标题、verdict 等强制工具请求退化成自动选择，可能返回普通文本。同时丢了 `stop_sequences`、`top_p`。
- 修法：翻译层补齐，但 `any` 不做通用映射（DashScope OpenAI 兼容模式当前文档给的通用取值是 `auto`、`none`、指定函数；`required` 是否支持依模型与模式而定，思考模式另有限制）。按能力分档：
  - `{"type":"tool","name":X}` → `{"type":"function","function":{"name":X}}`（任何情况都能精确指定，最稳）。
  - `{"type":"any"}` 且只有一个工具 → 直接指定那个函数（等效强制，不依赖 `required`）。
  - `{"type":"any"}` 且多个工具 → 先尝试 `"required"`，上游报不支持就明确报错（不静默退化），或对该请求改走原生 Anthropic 通道（若该 provider 有）。
  - `{"type":"auto"}` → `"auto"`；`{"type":"none"}` → `"none"`。
  - `stop_sequences` → `stop`；`top_p` → `top_p`。
- 待验证：DashScope 各模型对 `required` 的真实支持，实现阶段用真实或 mock 端点实测确认，再定多工具 any 的落点。
- 验证：`tool`、单工具 `any` 两种强制都断言译出 body 精确指定了函数；多工具 `any` 在 mock 不支持 `required` 时断言明确报错而非退化；断言 stop、top_p 透传。

### 7.5 [P2] max_tokens 硬截 8192（csswitch_proxy.py:35）
- 问题：DeepSeek V4 输出上限远高于 8192，这个旧上限会截断长任务或工具调用。且 Qwen Max/Plus/Turbo 各自上限可能不同，按 provider 一刀切也不对。
- 修法：cap 改成【按解析后的目标模型】查表，不按 provider 一刀切。每个模型登记其 cap，并显式标注该值是「官方硬上限」还是「保守的安全默认值」（拿不到官方数就用后者并注明）。上游未给 max_tokens 时不强加。
- 验证：对 deepseek 目标模型与三个 qwen 目标模型分别断言 cap 取自该模型条目；带一个大于旧 8192 的 max_tokens 断言不被截到 8192。

### 7.6 [P2] 停止脚本假成功（stop-science-sandbox.sh:11）
- 问题：`|| true` 吞掉 stop 命令的失败，随后固定输出「沙箱已停」。
- 修法：捕获 stop 命令退出码，成功才报「已停」，失败报实际错误与退出码并非零退出。保留「data-dir 不存在则无需停止」的早退。
- 验证：对一个并未在跑的沙箱调用，断言输出与退出码如实反映，不谎报成功。

### 7.7 [P2] 端口字符串绕过（launch-virtual-sandbox.sh:37）
- 问题：只拒绝精确字符串 `8765`，`08765` 会被放行传给 Science。旧启动脚本同病。
- 修法：端口先归一化为十进制整数再比较（`$(( 10#$PORT ))`），等于 8765 即拒绝。数据目录护栏用 realpath 比真实目录。两个启动脚本都改。
- 验证：传 `08765` 断言被拒；传符号链接指向真实目录的 data-dir 断言被拒。

### 7.8 [P2] 测试测错对象（proxy_e2e_test.py:28）
- 问题：测试固定启动已被取代的 `qwen_proxy.py`；DeepSeek、主 Qwen 路径、强制工具、流中断都没回归；`unittest discover` 实际发现 0 个测试（命名不匹配默认 `test*.py`）。
- 修法：重写 `test/`，改打 `csswitch_proxy.py`，用一个本地 mock 上游（假 DeepSeek / 假 DashScope）覆盖：
  1. deepseek 透传：模型改名、thinking 归一化（auto → adaptive）、强制工具时 thinking 置 disabled。
  2. qwen 翻译：tool_choice 映射、tool_use 往返、stop/top_p 透传。
  3. 流中断：mock 中途断流，断言干净收尾（对应 7.3）。
  4. 鉴权：缺 secret 得 403、上游零调用（对应 7.2）。
  文件命名改成 `test_*.py` 让默认发现能找到，或在 runner 里显式 `-p '*_test.py'`。
- 验证：`python3 -m unittest discover test` 报告发现的用例数大于 0 且全绿。

## 8. 错误处理

- 后端每个 Tauri command 返回 `Result`，前端把 err 显示在面板，不静默。
- 子进程 stderr 收进 `~/.csswitch/logs/`，面板显示尾巴。
- 探活超时给明确文案（代理没起、沙箱没就绪、上游不通分别不同提示）。
- 缺 python3 / node / Science 二进制时，一键按钮直接给「缺少依赖 X」而不是崩溃。
- 铁律断言失败（端口 8765、真实目录）时硬停并红字提示。

## 9. 测试策略

- 代理层：第 7.8 的 mock 上游回归，覆盖 deepseek / qwen / 强制工具 / 流中断 / 鉴权。默认发现能找到、全绿。
- 伪造器：符号链接护栏用例（7.1），加已有的解密自校验。
- 脚本：端口归一化与 realpath 护栏用例（7.7）、停止脚本如实报告用例（7.6）。
- app：手动冒烟。填 key、保存、启动代理、一键越登录、浏览器打开登录态、停止，各一遍。整链联调只在用户明确同意时做，仍守铁律第 2、3 条。

## 10. GitHub 上传准备

- README.md：中文为主。含项目定位、架构图、铁律摘要、安装与用法、免责声明。
- LICENSE：MIT。
- 免责声明：个人与研究用途；推理请求不经过 Anthropic；使用本地自造的虚拟登录、零真实凭证；与 Anthropic 无从属或背书关系；不提供任何担保；使用者自负风险。措辞避免「零 Anthropic 接触」这类绝对断言（Science 启动阶段仍会尝试硬编码 profile/account 接口，失败无害）。
- 密钥扫描：用 `gitleaks` 这类扫描器配一份明确 allowlist（文档示例、测试假 token、源码占位常量都进 allowlist），不用裸 grep（裸 grep 必然命中文档与测试假串，验收不可执行）。分三处各扫一遍：工作树、待提交暂存区、Git 历史。确认 `encryption.key`、`.oauth-tokens`、`.env` 从不入库。已知 `.sandbox/` 已被忽略；`findings/*.log` 经初步排查无真实密钥，仍以 gitleaks 复核为准。
- `.gitignore` 补充：Rust `target/`、`node_modules/`、Tauri 与 py2app 的 `build/` `dist/`、`~/.csswitch` 不在仓库内无需忽略但 README 说明。
- 敏感内容处置：按 12.1 已定的「公开全量」执行。伪造器与逆向细节入库，README 写完整用法，附显著免责声明。公开推送前过一遍 12.2 的法律/条款 go-no-go 检查点（最后可反悔的闸）。

## 11. 分期实施顺序

0. 前置：公开边界已定为「公开全量 + 免责声明」（12.1），伪造器进包，无需再拆。
1. Phase 0：加固 8 项 + 重写测试，全绿。这是地基。
2. Phase 1a：Tauri 骨架 + 进程管家（起停代理、探活、config.json 读写、第 5.1 节生命周期规则）。
3. Phase 1b：前端面板 + 一键越登录全流程 + 状态灯。
4. Phase 1c：手动冒烟联调（用户同意后）。
5. Phase 2：GitHub 准备（README、LICENSE、gitleaks 扫描、忽略规则、按 12.1 边界落实、公开前过 12.2 go-no-go）。git init 与首次提交放在这一步，仓库公开/私有与上传时机由用户按 12.1 决定。

## 12. 风险与待确认

### 12.1 [已决定：B 公开全量 + 免责声明] 公开边界与「一键越过登录」的冲突

评审指出一处真实矛盾：若公开仓库排除 `make-virtual-oauth.mjs`，而 app 的一键越登录流程又必须打包并调用它，那么从公开源码根本构建不出 spec 描述的产品。这与「一键越过登录」这个产品目标直接冲突，须在进 Phase 0 前定死边界。当时四个可选：

- A. 私有 GitHub 仓库（全量，含伪造器）。风险最低，但仓库不公开。
- B. 【已选】公开全量 + 显著免责声明。任何人克隆即得完整一键登录；用户在知悉风险后自行承担 DMCA/条款/关系风险。
- C. 公开「代理 + app 框架」，伪造器作为私有 overlay 构建时注入。
- D. 公开版只做代理，不含一键登录整块。

决定影响：伪造器与逆向细节进公开仓库；Phase 1 正常打包伪造器；第 10 节 README 写完整用法 + 免责声明；公开推送前过 12.2 的 go-no-go。

### 12.2 法律与条款风险（潜在风险，非法律结论）

以下是潜在风险提示，不是法律意见，具体适用与豁免需专业法律判断。公开前设一个独立的「法律/条款 go-no-go 检查点」，通过才发布。

- 无争议部分：翻译代理与桌面 app 本身干净，公开无碍，是项目真正有用、可复用的部分。
- 敏感部分：`make-virtual-oauth.mjs` 与 CLAUDE.md 里对 Science OAuth 令牌加密格式的逆向（HKDF 参数、AAD、AES-GCM 结构、encryption.key 结构）。这是对 Anthropic 商业产品内部机制的逆向，且构成一套绕过其登录门的可用方法。
- 潜在风险（非结论）：
  1. DMCA 反规避（美国版权法 §1201 涉及规避访问控制及相关工具传播）：带明确「绕过登录」说明的公开仓库可能成为下架请求对象。是否真正适用、有无豁免，需法律判断。
  2. 服务条款：绕过 Science 登录门可能违反 Anthropic 条款，公开教程可能被视为诱导他人违反。
  3. 被打补丁：Anthropic 可能改加密方案使工具失效，危害低，只是维护成本。
  4. 个人关系风险：以本人身份公开一个绕过 Anthropic 产品的工具，可能给自己的 Anthropic 账号招来负面关注。
- 不构成的风险：不伤害第三方；推理走用户自付的第三方，不偷 Anthropic 算力；不泄露用户自己的密钥；无恶意代码。这是法律、条款、关系层面的风险，不是对他人的安全风险。

### 12.3 其余待确认（实现阶段实测）

- Qwen 多工具 `any` 的落点，依赖 DashScope 对 `required` 的实测支持（见 7.4）。
- path 前缀 secret 与 Science base_url 拼接的兼容性（见 7.2），先验主方案，不通退备用。
- DeepSeek 官方模型 id 核对：`model_map` 里的 `deepseek-v4-pro`、`deepseek-v4-flash` 必须与 DeepSeek 官方目录（api.deepseek.com）当前真实模型名对齐，否则原生端点拒收。实现阶段拉一次官方模型列表核对，对不上就改映射。上游走的是 DeepSeek 第一方官方 API（`https://api.deepseek.com/anthropic/v1/messages`，`x-api-key` + `DEEPSEEK_API_KEY`），非第三方转卖。

## 13. 从竞品 claude-science-api-bridge 借鉴的待办

对比开源竞品 `Jyx0208/claude-science-api-bridge`（同类：Science 接第三方，但直接改真实目录、无鉴权写端点、含 443/hosts/证书拦截，均为反面教材）后，抽出可借鉴的形态层面改进，挂到后续阶段：

- 配置结构（Phase 1）：config 支持多 provider（deepseek/openai/custom 或更多）+ per-provider `model_map` + `model_pattern`，取代当前写死在 `PROVIDERS` 里的映射。Phase 0 的 `model_caps` 到时并入这套结构。
- 面板（Phase 1）：key 打码回显、写接口拒收打码占位符（本 spec 评审已要求，竞品做法可印证细节）。
- 运维脚本（Phase 2）：`doctor`（只读诊断）/ `self-test` / `verify-proxy` 三件套，方便安装后自检。
- 明确不借鉴（安全底线）：绝不动真实 `~/.claude-science`；管理写端点必须鉴权；不做 443/hosts/证书拦截（已判定 base_url 无条件生效，推理无需拦截）。
