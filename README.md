# EasyColab Web Console

把 [EasyColab](https://github.com/wy51ai/easycolab) 的桌面客户端改造成**局域网可访问的网页控制台**。

原项目是 macOS / Windows / Linux 的 Qt(PySide6) 原生应用，不做显卡计算——它只是"遥控器"，真正的算力在 Google Colab 的 GPU 实例上。本项目保留了这套逻辑，但把界面换成网页：不需要浏览器里套远程桌面（noVNC），不需要服务器有图形环境，也不需要在本机装 Qt。

## 特性

- **纯网页界面** — 局域网任意设备打开即用，复制粘贴走原生网页体验
- **无图形依赖** — 不装 Qt / PySide6 / Xvfb / noVNC，容器镜像仅约 490MB
- **手动授权码流程** — 服务器无需浏览器，生成 OAuth 链接后在你自己的电脑上授权，把 code 粘回来
- **完整还原原客户端面板** — 实例管理、CU 额度与闲置休眠、GPU/内存/磁盘指标、模型下载与显存加载、ComfyUI 画布、生成工作台、作品库、运行日志
- **凭证持久化** — `HOME` 指向挂载卷，容器重建后无需重新授权
- **产物本地留存** — 生成的视频与图片落到挂载卷，网页内可预览与下载

## 快速开始

```yaml
# docker-compose.yml
services:
  easycolab-web:
    build: .
    ports:
      - "8626:8626"
    volumes:
      - /path/to/easycolab:/opt/easycolab:ro   # 上游仓库，只读
      - ./data:/data                           # 凭证、配置、产物
    environment:
      HOME: /data
      EASYCOLAB_DATA_DIR: /data
      EASYCOLAB_REPO_DIR: /opt/easycolab
      EASYCOLAB_OUTPUT_DIR: /data/outputs
```

```bash
# 1. 克隆上游仓库（本项目不复制它的代码）
git clone --depth 1 https://github.com/wy51ai/easycolab.git

# 2. 按上面的 compose 配置好路径，然后
docker compose up -d --build
```

打开 `http://<你的服务器IP>:8626`。

## 首次使用

1. **绑定 Google 账号** — 点「绑定 / 切换 Google 账号」→ 生成授权链接 → 在自己电脑的浏览器打开授权 → 把浏览器给出的 code 粘回网页。要求该账号已订阅 Colab Pro / Pro+ / AI Ultra。
2. **选 GPU** — G4（PRO 6000 · 96G，推荐）/ H100 / A100 / L4 / T4。
3. **唤醒实例并部署** — 3~10 分钟，日志实时滚动，出现 `https://xxxx.gradio.live` 即完成。
4. **下载权重并加载到显存** — 首次下载约 130GB，视 Colab 到 HuggingFace 的带宽而定。
5. **生成** — 填提示词或选预设，点开始生成。产物自动落到 `data/outputs/`。

> **计费提醒**：Colab 按 CU 计费。用完请点「释放全部实例」，否则会持续消耗额度。「闲置自动释放」默认 30 分钟。

## 为什么锁定 google-colab-cli==0.7.5

上游 `colab_bridge.py` 依赖 `colab_cli.auth.TOKEN_CONFIG_PATH`。该符号在 **0.7.6 起被移除**（改用 `AuthProvider` + keyring），0.8.0 更是完全没有了。装最新版会直接 `ImportError`。逐版本核对 wheel 源码确认后，在 `requirements.txt` 里固定到 0.7.5。

## 架构

```
浏览器 (局域网)
     │
     ▼
easycolab-web 容器 :8626            ← FastAPI，本项目的代码
     │  ├─ /api/state, /api/wake ...        → 调用 colab_bridge.py CLI
     │  └─ /api/cloud/*                     → HTTP Basic 代理到 Colab 端 app.py
     ▼
colab_bridge.py (上游，只读挂载)
     │  唤醒实例 / 上传 app.py / 保活 / 释放
     ▼
Google Colab GPU 实例
     └─ app.py (Gradio + FastAPI，含 ComfyUI 与模型推理)
          ↕ gradio.live / trycloudflare 隧道
     └─ 生成的产物 ←────────────── 回传
```

代理层的 `auth=("easycolab", admin_password)` 与上游桌面客户端完全一致，管理密码由本项目自动生成并持久化，`/api/state` **不会**把密码字段返回给浏览器。

## 已知限制

- **公网隧道地址会变** — 每次重新唤醒，`*.gradio.live` 地址都是新的，需重新复制。
- **保活不是永久的** — 打开「保活守护」可防止无浏览器连接时被系统约 20 分钟强杀，但实例生命周期最终仍受 Colab 策略约束。
- **端口需可绑定** — 若宿主机 docker 地址池耗尽，compose 里的自定义 `subnet` 可解决（本项目已指定 `172.32.250.0/24`，按需改）。
- **未做多用户鉴权** — 面向内网信任环境设计，局域网内可直接访问。如需公网暴露，请自行在前面加反代与认证。

## 许可证

本项目代码基于 Apache License 2.0。上游 [EasyColab](https://github.com/wy51ai/easycolab) 亦为 Apache-2.0，其仓库不包含任何模型权重。

本项目与 Google LLC、Google Colab、MiniMax、阿里巴巴通义千问（Qwen）等官方机构无任何隶属、赞助或背书关系。使用第三方客户端连接 Colab 运行时可能受平台策略变更影响，账号风险由使用者自行评估承担。

## 致谢

- [wy51ai/easycolab](https://github.com/wy51ai/easycolab) — 本项目的上游，全部云端能力来自它
- [google-colab-cli](https://pypi.org/project/google-colab-cli/) — 无浏览器唤醒 Colab 实例的底层库
- [Gradio](https://www.gradio.app/) · [FastAPI](https://fastapi.tiangolo.com/)