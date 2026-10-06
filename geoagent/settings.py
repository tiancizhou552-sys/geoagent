# -*- coding: utf-8 -*-
"""全局配置：路径、密钥、运行时参数。所有模块统一从这里取配置。"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')

# ── 路径 ──────────────────────────────────────────────
DATA_DIR = ROOT / 'data'
RAW_DIR = DATA_DIR / 'raw'
CONFIG_DIR = ROOT / 'config'
LOG_DIR = ROOT / 'logs'
EVAL_DIR = ROOT / 'eval'
WEB_DIR = ROOT / 'web'

# ── 大模型（DeepSeek，OpenAI 兼容协议）────────────────
LLM_API_KEY = os.getenv('LLM_API_KEY', '')
LLM_MODEL_ID = os.getenv('LLM_MODEL_ID', 'deepseek-flash')
LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'https://api.deepseek.com')
LLM_TIMEOUT = float(os.getenv('LLM_TIMEOUT', '60'))

# ── Agent 运行时行为 ──────────────────────────────────
AGENT_MAX_STEPS = int(os.getenv('AGENT_MAX_STEPS', '8'))
AGENT_MAX_TOOL_RETRIES = int(os.getenv('AGENT_MAX_TOOL_RETRIES', '2'))
AGENT_ENGINE = os.getenv('AGENT_ENGINE', 'react')   # react | raw

# ── Web ──────────────────────────────────────────────
HOST = os.getenv('HOST', '127.0.0.1')
PORT = int(os.getenv('PORT', '5001'))

# ── 合规底图（腾讯地图 GL JS）─────────────────────────
# 合规红线：本项目不得使用 Google / Apple / Bing 海外版 / OpenStreetMap 直连瓦片 /
# Mapbox 等无资质底图。允许的只有腾讯地图、高德、百度、天地图。
# key 由用户在 lbs.qq.com 自行申请（免费），存在服务端 .env 里，不硬编码进前端 JS。
TMAP_KEY = os.getenv('TMAP_KEY', '')

# ── 空间常量 ──────────────────────────────────────────
CRS_STORAGE = 'EPSG:4326'    # 存储与对外交换：WGS84 经纬度
CRS_COMPUTE = 'EPSG:4547'    # 内部计算：CGCS2000 / 3° 高斯-克吕格 CM 114E
WALK_SPEED_M_PER_MIN = 72.0  # 约 1.2 m/s

# 工具参数的硬边界（同时写进 JSON Schema 与运行时校验）
RADIUS_MIN_M, RADIUS_MAX_M = 10.0, 5000.0
K_MIN, K_MAX = 1, 10
GEOFENCE_TOLERANCE_M = 200.0


def ensure_dirs():
    for d in (DATA_DIR, CONFIG_DIR, LOG_DIR, EVAL_DIR):
        d.mkdir(parents=True, exist_ok=True)
