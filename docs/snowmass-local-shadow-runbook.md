# Snowmass 本地零付费 Shadow Runbook

本流程只用于 `shadow` 阶段。所有付费阶段仍使用 DeepSeek，并严格遵循
`shadow -> pilot5 -> pilot10 -> pilot25 -> batch50 -> remainder`。

MLX-LM 官方提供与 OpenAI Chat Completions 相近的本地 HTTP 服务，但官方明确说明
该服务不建议直接作为公网生产服务。因此这里只允许绑定 loopback，并由编排器核验模型
完整文件清单、监听进程的实际可执行文件以及启动命令中的模型目录。

官方资料：

- <https://github.com/ml-explore/mlx-lm>
- <https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md>

## 1. 首次 readiness 检查

```bash
python3 scripts/check_snowmass_local_shadow_readiness.py
```

退出码 `2` 表示仍有缺失条件。当前仓库不自动安装依赖或下载模型。

## 2. 安装和下载（仅在得到明确授权后）

使用当前项目 Python 创建带 `--system-site-packages` 的独立虚拟环境，再在其中安装官方
`mlx-lm` 包。这样既不污染项目 Python，又保留现有 BabelDOC/PyMuPDF 运行依赖。模型必须
下载到一个固定的本地目录，不能在 shadow 运行时临时从 Hugging Face 拉取。

安装完成后，用本仓库工具生成完整模型清单：

```bash
/absolute/path/to/venv/bin/python scripts/snowmass_local_attestation.py \
  --model-root /absolute/path/to/model \
  --model default_model \
  --output /absolute/path/to/model-manifests/qwen-shadow.json
```

清单必须位于模型目录之外；模型目录多出、缺少或改变任一文件都会使 shadow 启动失败。

## 3. 启动本地服务

使用虚拟环境中的 Python 启动 MLX-LM，且 `--model` 必须传入与清单一致的绝对模型目录：

```bash
/absolute/path/to/venv/bin/python -m mlx_lm.server \
  --model /absolute/path/to/model \
  --port 8080
```

服务只可监听本机。编排器会使用 `lsof` 确认 8080 端口只有一个监听 PID，并核对该 PID
映射的 Python 可执行文件及命令行中的模型目录。

## 4. 带证明的 readiness 检查

```bash
/absolute/path/to/venv/bin/python scripts/check_snowmass_local_shadow_readiness.py \
  --local-openai-base-url http://127.0.0.1:8080 \
  --local-model default_model \
  --local-model-manifest /absolute/path/to/model-manifests/qwen-shadow.json \
  --local-server-binary /absolute/path/to/venv/bin/python
```

只有输出中的 `ready` 为 `true` 才能继续。

## 5. Fresh shadow

使用全新的 output/control 目录，有限正预算和有限请求数。下面的 `120` 是 shadow 的硬请求
上限，不是并发数；根据 preflight 的精确投影可以进一步调低。

```bash
/absolute/path/to/venv/bin/python scripts/run_snowmass_batch_production.py \
  --stage shadow \
  --project-max-cost-rmb 1000 \
  --stage-max-cost-rmb 1 \
  --stage-max-api-calls 120 \
  --through-stage packaged \
  --output-root output/snowmass2021/babeldoc_local_shadow_v1 \
  --control-dir output/snowmass2021/production_control_local_shadow_v1 \
  --local-openai-base-url http://127.0.0.1:8080 \
  --local-model default_model \
  --local-model-manifest /absolute/path/to/model-manifests/qwen-shadow.json \
  --local-server-binary /absolute/path/to/venv/bin/python
```

晋级证据要求：完整 canonical shadow cohort、fresh packaged 结果、零付费 API 调用、至少一次
成功本地模型调用、无隔离论文、无人工复核项、无不确定请求，并且论文 artifact manifest 与
run snapshot 的 environment/pipeline/execution 三锁及 record ID 完全一致。
