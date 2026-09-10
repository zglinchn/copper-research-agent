#!/bin/zsh
# 一键启动铜减量化模型智能体 Web 服务（自动加载 webapp/.env 密钥配置）
cd "$(dirname "$0")/.." || exit 1
exec python3 -m uvicorn webapp.server:app --host 127.0.0.1 --port 8000
