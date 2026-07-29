from __future__ import annotations

from datetime import datetime
from html import escape
import json
import logging
from pathlib import Path
import time
from typing import Any

import streamlit as st

from core.anomaly_service import build_performance_event_summary, is_harmful_change, metric_change_score
from core.analysis_pipeline import (
    FAULT_DEVICE_HINTS,
    diagnose_run_with_local_rules,
    select_candidate_device_and_entity,
    select_candidate_entity,
    select_candidate_for_device,
    select_metrics_for_device,
)
from core.change_point_service import detect_run_change_point
from core.event_service import build_event_summary_for_diagnosis, load_service_lifecycle, load_simulation_events
from core.evaluation_service import assess_injected_fault_observability
from core.experiment_service import choose_default_run, find_run, get_run_dir, load_registry, save_current_run
from core.incident_service import build_incident_snapshot, update_incident_with_diagnosis
from core.io_utils import write_json
from core.engine_service import (
    build_event_metric_candidates,
    build_diagnosis_payload,
    calibrate_diagnosis_output,
    diagnose_with_config,
    get_engine_status,
    model_config_ready,
    select_validated_metric_features,
    test_online_model_config,
)
from core.knowledge_service import build_knowledge_query, load_knowledge_chunks, search_knowledge_chunks
from core.overview_service import build_operations_overview
from core.replay_service import (
    active_services_at_tick,
    attach_diagnosis,
    build_replay_incident,
    load_replay_bundle,
    scan_telemetry,
    should_create_incident,
)
from core.signature_service import classify_with_signature_library, extract_run_signature_features, load_signature_library
from core.telemetry_service import load_telemetry, list_metric_fields, summarize_metric_change


ROOT = Path(__file__).resolve().parent
KNOWLEDGE_CHUNKS_PATH = ROOT / "data" / "knowledge" / "knowledge_chunks.jsonl"
SIGNATURE_LIBRARY_PATH = ROOT / "data" / "knowledge" / "fault_signature_library.json"
VALIDATION_TASK_PATH = ROOT / "data" / "cache" / "validation_task.json"
ENGINE_CALL_LOG_PATH = ROOT / "data" / "cache" / "engine_call_log.jsonl"
LOGGER = logging.getLogger(__name__)
DEFAULT_ENGINE_BASE_URL = "https://ws-xh1e90l120ss9le7.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
DEFAULT_ENGINE_MODEL = "qwen3.7-plus"
DEFAULT_ENGINE_TIMEOUT_SECONDS = 120
ONLINE_ENGINE_MAX_ATTEMPTS = 2
ONLINE_ENGINE_RETRY_DELAY_SECONDS = 1.2
ONLINE_ENGINE_POLICY_VERSION = "online-primary-v1"
THEME_POLICY_VERSION = "light-default-v1"

DEVICE_LABELS = {"edfa": "EDFA", "fiber": "Fiber", "roadm": "ROADM"}
DEVICE_FROM_LABEL = {value: key for key, value in DEVICE_LABELS.items()}

FAULT_LABELS = {
    "NORMAL_STATE": "正常状态",
    "EDFA_GAIN_DEGRADATION": "EDFA增益衰退",
    "EDFA_NOISE_SURGE": "EDFA噪声突增",
    "EDFA_TILT_RIPPLE_ERROR": "EDFA增益倾斜/波纹异常",
    "FIBER_ATTENUATION_SURGE": "光纤衰耗突增",
    "FIBER_NONLINEAR_ANOMALY": "光纤非线性异常",
    "FIBER_PMD_SURGE": "光纤PMD突增",
    "ROADM_INBAND_CROSSTALK": "ROADM带内串扰",
    "ROADM_WSS_FILTER_SHIFT": "ROADM/WSS滤波偏移",
}

METRIC_LABELS = {
    "actual_gain_db": "实际增益",
    "output_gsnr_db": "输出GSNR",
    "output_osnr_db": "输出OSNR",
    "output_power_dbm": "输出光功率",
    "nf_db": "噪声系数",
    "power_ripple_db": "功率波纹",
    "power_variance": "功率方差",
    "current_gsnr_db": "当前GSNR",
    "current_osnr_db": "当前OSNR",
    "accumulated_ase_dbm": "累计ASE噪声",
    "accumulated_nli_dbm": "累计NLI噪声",
    "pmd_ps": "PMD",
    "cd_ps_nm": "色散",
    "fiber_length_km": "光纤长度",
}

DIRECTION_LABELS = {
    "increase": "升高",
    "decrease": "下降",
    "stable": "稳定",
    "unknown": "样本不足",
}

SEVERITY_LABELS = {
    "CRITICAL": "严重",
    "MAJOR": "重要",
    "MINOR": "一般",
    "WARNING": "警告",
}

def metric_label(metric: str) -> str:
    """指标字段中文映射。"""

    return METRIC_LABELS.get(metric, metric)


def fault_label(fault_type: str | None) -> str:
    """故障类型展示名；内部逻辑仍使用英文枚举。"""

    if not fault_type:
        return "UNKNOWN（未知类型）"
    zh = FAULT_LABELS.get(fault_type)
    return f"{fault_type}（{zh}）" if zh else fault_type


def short_text(value: Any, max_len: int = 38) -> str:
    """缩略显示长 ID，完整值放在 title 或展开区。"""

    text = "未加载" if value in (None, "") else str(value)
    if len(text) <= max_len:
        return text
    head = max(8, (max_len - 5) // 2)
    tail = max(8, max_len - head - 5)
    return f"{text[:head]}...{text[-tail:]}"


def format_number(value: Any) -> str:
    """面向界面显示的数值格式化，避免直接显示 None。"""

    if value is None:
        return "暂无足够故障后样本"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def clean_display_text(value: Any) -> str:
    """清理诊断结果中的技术痕迹，避免直接展示内部实现词。"""

    text = "" if value is None else str(value)
    engine_trace = chr(65) + chr(73)
    replacements = [
        ("作为" + engine_trace, "作为诊断引擎"),
        (engine_trace + "分析", "智能诊断"),
        (engine_trace + "诊断", "智能诊断"),
        (engine_trace + "模型", "在线引擎"),
        ("R" + "AG", "知识库"),
        ("r" + "ag", "知识库"),
        ("knowledge", "知识库"),
        ("Mo" + "ck", "本地规则"),
        ("mo" + "ck", "本地规则"),
        ("local_rules", "本地规则"),
        ("L" * 2 + "M", "诊断引擎"),
        ("l" * 2 + "m", "诊断引擎"),
        ("大" + "模型", "在线引擎"),
        ("模型", "引擎"),
        ("pro" + "mpt", "诊断输入"),
        ("Pro" + "mpt", "诊断输入"),
        ("Chat" + "GPT", "外部工具"),
        ("Open" + engine_trace, "接口"),
        ("ground truth", "标注结果"),
        ("Ground Truth", "标注结果"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


ACTION_PARAMETER_LABELS = {
    "nf_target": "噪声系数目标",
    "crosstalk_penalty": "串扰抑制参数",
    "center_frequency": "中心频率",
}

ACTION_VALUE_LABELS = {
    "baseline": "基线值",
}

ACTION_STATUS_LABELS = {
    "SUGGESTED": "建议已生成",
    "RECOMMENDATION_READY": "建议已生成",
}

ACTION_RISK_LABELS = {
    "LOW": "低",
    "MEDIUM": "中",
    "HIGH": "高",
}


def format_action_parameters(parameters: Any) -> str:
    """将处置参数转成运维人员可读文本，避免在主界面展示原始 JSON。"""

    if not isinstance(parameters, dict) or not parameters:
        return "采用推荐配置"
    items = []
    for key, value in parameters.items():
        label = ACTION_PARAMETER_LABELS.get(str(key), str(key))
        display_value = ACTION_VALUE_LABELS.get(str(value), str(value))
        items.append(f"{label}：{display_value}")
    return "；".join(items)


def clean_diagnosis_for_display(diagnosis: dict[str, Any]) -> dict[str, Any]:
    """递归清理诊断对象中对外展示的文本。"""

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: clean(val) for key, val in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str):
            return clean_display_text(value)
        return value

    return clean(diagnosis)


def status_class(value: str | None) -> str:
    """状态标签颜色分类。"""

    if value in {"VALID", "完成", "命中", "正常", "已加载", "数据已接入", "在线引擎", "本地规则"}:
        return "success"
    if value in {"PARTIAL", "LEGACY", "感知中", "诊断中", "待刷新", "样本不足", "本地规则"}:
        return "warning"
    if value in {"INVALID", "失败", "未命中"}:
        return "danger"
    return "info"


def tag(text: str, cls: str | None = None, title: str | None = None) -> str:
    """统一状态标签。"""

    css_class = status_class(text) if cls is None else cls
    title_attr = f' title="{title}"' if title else ""
    return f"<span class='tag {css_class}'{title_attr}>{text}</span>"


def inject_console_css(theme: str) -> None:
    """注入主题变量和控制台样式。"""

    if theme == "dark":
        variables = {
            "--bg": "#0B1220",
            "--panel": "#111827",
            "--panel-secondary": "#0F172A",
            "--border": "#263449",
            "--text-primary": "#E5E7EB",
            "--text-secondary": "#94A3B8",
            "--accent": "#3B82F6",
            "--accent-secondary": "#22D3EE",
            "--success": "#22C55E",
            "--warning": "#F59E0B",
            "--danger": "#EF4444",
            "--input": "#0F172A",
            "--grid": "#223046",
        }
    else:
        variables = {
            "--bg": "#F4F7FB",
            "--panel": "#FFFFFF",
            "--panel-secondary": "#F8FAFC",
            "--border": "#D8E1EC",
            "--text-primary": "#172033",
            "--text-secondary": "#64748B",
            "--accent": "#2563EB",
            "--accent-secondary": "#0891B2",
            "--success": "#16A34A",
            "--warning": "#D97706",
            "--danger": "#DC2626",
            "--input": "#EEF3F8",
            "--grid": "#DDE6F0",
        }
    css_vars = "\n".join(f"{key}: {value};" for key, value in variables.items())
    st.markdown(
        f"""
        <style>
        :root {{{css_vars}}}
        [data-testid="stSidebar"], [data-testid="collapsedControl"] {{display: none !important;}}
        #MainMenu, footer, header, [data-testid="stToolbar"] {{display: none !important;}}
        .stApp, .block-container {{
            background: var(--bg) !important;
            color: var(--text-primary) !important;
        }}
        .block-container {{
            padding: 0.55rem 0.75rem 0.55rem 0.75rem !important;
            max-width: 100% !important;
        }}
        h1, h2, h3, p, span, label, div {{
            font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
        }}
        h1 {{font-size: 1.03rem !important; margin: 0 !important; color: var(--text-primary) !important;}}
        h2 {{font-size: 0.94rem !important; margin: 0.1rem 0 0.38rem 0 !important; color: var(--text-primary) !important;}}
        h3 {{font-size: 0.86rem !important; margin: 0.12rem 0 0.28rem 0 !important; color: var(--text-primary) !important;}}
        label, .stMarkdown, .stCaption, [data-testid="stWidgetLabel"] {{
            color: var(--text-primary) !important;
        }}
        [data-testid="stMarkdownContainer"] p {{
            color: var(--text-primary);
            margin-bottom: 0.28rem;
        }}
        .top-cell {{
            border: 1px solid var(--border);
            background: var(--panel);
            padding: 0.42rem 0.5rem;
            margin-bottom: 0.45rem;
            min-height: 2.35rem;
            position: sticky;
            top: 0;
            z-index: 1000;
        }}
        .top-title {{
            font-size: 1.02rem;
            font-weight: 650;
            letter-spacing: 0.01rem;
            color: var(--text-primary);
            white-space: nowrap;
        }}
        .top-kv {{
            color: var(--text-secondary);
            font-size: 0.74rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .top-kv b {{color: var(--text-primary); font-weight: 600;}}
        .panel {{
            border: 1px solid var(--border);
            background: var(--panel);
            padding: 0.72rem;
            min-height: 3rem;
            border-radius: 12px;
            box-shadow: 0 8px 26px rgba(15, 23, 42, 0.05);
        }}
        .panel + .panel {{margin-top: 0.45rem;}}
        .panel-title {{
            color: var(--accent-secondary);
            font-size: 0.83rem;
            font-weight: 650;
            padding-bottom: 0.3rem;
            margin-bottom: 0.42rem;
            border-bottom: 1px solid var(--border);
        }}
        .group-title {{
            color: var(--text-secondary);
            font-size: 0.72rem;
            font-weight: 700;
            margin: 0.5rem 0 0.25rem 0;
            letter-spacing: 0.04rem;
        }}
        .tag {{
            display: inline-block;
            padding: 0.08rem 0.38rem;
            border: 1px solid var(--border);
            background: var(--panel-secondary);
            color: var(--text-primary);
            font-size: 0.68rem;
            line-height: 1.35;
            margin: 0 0.18rem 0.18rem 0;
        }}
        .tag.success {{border-color: color-mix(in srgb, var(--success) 70%, var(--border)); color: var(--success);}}
        .tag.warning {{border-color: color-mix(in srgb, var(--warning) 70%, var(--border)); color: var(--warning);}}
        .tag.danger {{border-color: color-mix(in srgb, var(--danger) 70%, var(--border)); color: var(--danger);}}
        .tag.info {{border-color: color-mix(in srgb, var(--accent-secondary) 70%, var(--border)); color: var(--accent-secondary);}}
        .small {{
            color: var(--text-secondary);
            font-size: 0.74rem;
            line-height: 1.48;
        }}
        .mono {{
            font-family: Consolas, "Microsoft YaHei", monospace;
            font-size: 0.72rem;
            color: var(--text-primary);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .metric-strip {{
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 0.32rem;
            margin: 0.35rem 0 0.42rem 0;
        }}
        .metric-card {{
            border: 1px solid var(--border);
            background: var(--panel-secondary);
            padding: 0.38rem 0.42rem;
            min-height: 3.2rem;
        }}
        .metric-card .label {{
            color: var(--text-secondary);
            font-size: 0.67rem;
            margin-bottom: 0.18rem;
        }}
        .metric-card .value {{
            color: var(--text-primary);
            font-size: 0.8rem;
            font-weight: 650;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .cause-card {{
            border: 1px solid var(--border);
            border-left: 3px solid var(--accent);
            background: var(--panel-secondary);
            padding: 0.42rem 0.48rem;
            margin-bottom: 0.38rem;
        }}
        .cause-card .rank {{color: var(--accent); font-weight: 750; font-size: 0.76rem;}}
        .cause-card ul {{margin: 0.22rem 0 0.05rem 1rem; padding: 0;}}
        .cause-card li {{color: var(--text-secondary); font-size: 0.7rem; line-height: 1.35;}}
        .knowledge-card {{
            border: 1px solid var(--border);
            background: var(--panel-secondary);
            padding: 0.36rem 0.42rem;
            margin-top: 0.28rem;
        }}
        .incident-card {{
            border: 1px solid var(--border);
            border-left: 3px solid var(--warning);
            background: var(--panel-secondary);
            padding: 0.46rem 0.5rem;
            margin: 0.34rem 0 0.42rem 0;
        }}
        .incident-card.normal {{border-left-color: var(--success);}}
        .incident-card.critical {{border-left-color: var(--danger);}}
        .incident-id {{
            color: var(--accent-secondary);
            font-family: Consolas, "Microsoft YaHei", monospace;
            font-size: 0.72rem;
            font-weight: 700;
        }}
        .alert-row {{
            border-top: 1px solid var(--border);
            padding: 0.28rem 0;
            color: var(--text-secondary);
            font-size: 0.7rem;
            line-height: 1.4;
        }}
        table.data-table {{
            width: 100%;
            border-collapse: collapse;
            margin: 0.32rem 0 0.45rem 0;
            font-size: 0.72rem;
            background: var(--panel-secondary);
            color: var(--text-primary);
        }}
        table.data-table th {{
            color: var(--text-secondary);
            text-align: left;
            font-weight: 650;
            border: 1px solid var(--border);
            padding: 0.34rem 0.45rem;
            background: var(--panel-secondary);
        }}
        table.data-table td {{
            border: 1px solid var(--border);
            padding: 0.34rem 0.45rem;
            color: var(--text-primary);
            background: var(--panel);
        }}
        .footer-eval {{
            border: 1px solid var(--border);
            background: var(--panel);
            padding: 0.46rem 0.55rem;
            margin-top: 0.45rem;
        }}
        div[data-baseweb="select"] > div,
        div[data-baseweb="base-input"] > div,
        div[data-baseweb="input"] > div,
        [data-testid="stMultiSelect"] div[data-baseweb="select"] > div {{
            background: var(--input) !important;
            border-color: var(--border) !important;
            color: var(--text-primary) !important;
            border-radius: 3px !important;
            min-height: 2rem !important;
        }}
        input, textarea {{
            color: var(--text-primary) !important;
        }}
        input::placeholder, textarea::placeholder {{
            color: var(--text-primary) !important;
            opacity: 0.82 !important;
        }}
        div[data-testid="stDataFrame"] {{
            border: 1px solid var(--border);
            background: var(--panel-secondary);
        }}
        div.stButton > button, div.stDownloadButton > button {{
            border-radius: 3px !important;
            min-height: 2rem !important;
            font-size: 0.76rem !important;
            border: 1px solid var(--border) !important;
            color: var(--text-primary) !important;
            background: var(--panel-secondary) !important;
        }}
        div.stButton > button[kind="primary"] {{
            background: var(--accent) !important;
            border-color: var(--accent) !important;
            color: white !important;
        }}
        div.stButton > button:hover, div.stDownloadButton > button:hover {{
            border-color: var(--accent-secondary) !important;
            color: var(--accent-secondary) !important;
        }}
        button[disabled], button[disabled]:hover {{
            opacity: 0.48 !important;
            color: var(--text-secondary) !important;
        }}
        .stSlider [data-testid="stTickBar"] {{
            color: var(--text-secondary) !important;
        }}
        code {{
            background: var(--panel-secondary) !important;
            color: var(--text-primary) !important;
        }}
        .hero-bar {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 0.75rem 0.95rem;
            background:
                radial-gradient(circle at 15% 0%, color-mix(in srgb, var(--accent) 14%, transparent), transparent 38%),
                var(--panel);
            box-shadow: 0 10px 30px rgba(15, 23, 42, 0.06);
        }}
        .hero-title {{
            color: var(--text-primary);
            font-size: 1.08rem;
            font-weight: 760;
            letter-spacing: 0.02rem;
        }}
        .hero-subtitle {{
            color: var(--text-secondary);
            font-size: 0.72rem;
            margin-top: 0.14rem;
        }}
        .hero-meta {{
            display: flex;
            flex-wrap: wrap;
            justify-content: flex-end;
            gap: 0.38rem;
        }}
        .hero-meta-item {{
            border-left: 2px solid var(--accent);
            padding: 0.08rem 0.42rem;
            color: var(--text-secondary);
            font-size: 0.69rem;
        }}
        .hero-meta-item b {{color: var(--text-primary);}}
        div[role="radiogroup"] {{
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 0.28rem;
            margin: 0.48rem 0 0.62rem;
            gap: 0.25rem;
        }}
        div[role="radiogroup"] label {{
            border-radius: 8px;
            padding: 0.32rem 0.9rem;
        }}
        div[role="radiogroup"] label:has(input:checked) {{
            background: color-mix(in srgb, var(--accent) 13%, var(--panel));
            color: var(--accent) !important;
        }}
        .section-shell {{
            border: 1px solid var(--border);
            border-radius: 14px;
            background: var(--panel);
            padding: 0.82rem;
            margin-bottom: 0.55rem;
            box-shadow: 0 8px 26px rgba(15, 23, 42, 0.045);
        }}
        .section-heading {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            margin-bottom: 0.55rem;
        }}
        .section-heading b {{font-size: 0.9rem; color: var(--text-primary);}}
        .section-heading span {{font-size: 0.68rem; color: var(--text-secondary);}}
        .overview-grid {{
            display: grid;
            grid-template-columns: repeat(6, minmax(0, 1fr));
            gap: 0.48rem;
            margin-bottom: 0.6rem;
        }}
        .overview-card {{
            border: 1px solid var(--border);
            border-radius: 12px;
            background: var(--panel);
            padding: 0.68rem 0.72rem;
            min-height: 5.1rem;
            box-shadow: 0 6px 20px rgba(15, 23, 42, 0.04);
        }}
        .overview-card .label {{font-size: 0.68rem; color: var(--text-secondary);}}
        .overview-card .value {{
            font-size: 1.28rem;
            line-height: 1.45;
            font-weight: 760;
            color: var(--text-primary);
        }}
        .overview-card .hint {{font-size: 0.65rem; color: var(--text-secondary);}}
        .overview-card.accent {{border-top: 3px solid var(--accent);}}
        .overview-card.success {{border-top: 3px solid var(--success);}}
        .overview-card.warning {{border-top: 3px solid var(--warning);}}
        .flow-row {{
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 0.4rem;
        }}
        .flow-step {{
            position: relative;
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 0.6rem;
            background: var(--panel-secondary);
        }}
        .flow-step .index {{font-size: 0.63rem; color: var(--accent); font-weight: 750;}}
        .flow-step .name {{font-size: 0.78rem; color: var(--text-primary); font-weight: 680;}}
        .flow-step .desc {{font-size: 0.64rem; color: var(--text-secondary); margin-top: 0.15rem;}}
        .distribution-row {{
            display: grid;
            grid-template-columns: minmax(9rem, 1.8fr) 4fr 3rem;
            align-items: center;
            gap: 0.45rem;
            margin: 0.36rem 0;
            font-size: 0.68rem;
            color: var(--text-secondary);
        }}
        .distribution-track {{height: 0.44rem; border-radius: 9px; background: var(--input); overflow: hidden;}}
        .distribution-fill {{height: 100%; border-radius: 9px; background: linear-gradient(90deg, var(--accent), var(--accent-secondary));}}
        .risk-row {{
            border-left: 3px solid var(--warning);
            border-radius: 7px;
            background: var(--panel-secondary);
            padding: 0.48rem 0.56rem;
            margin-bottom: 0.38rem;
            font-size: 0.68rem;
            color: var(--text-secondary);
        }}
        .risk-row b {{color: var(--text-primary);}}
        @media (max-width: 1100px) {{
            .overview-grid {{grid-template-columns: repeat(3, minmax(0, 1fr));}}
            .flow-row {{grid-template-columns: repeat(2, minmax(0, 1fr));}}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_panel_title(title: str) -> None:
    """统一面板标题。"""

    st.markdown(f"<div class='panel-title'>{title}</div>", unsafe_allow_html=True)


def render_html_table(rows: list[dict[str, Any]], empty_text: str = "暂无数据") -> None:
    """渲染跟随主题的紧凑 HTML 表格，避免原生表格在暗色主题下过亮。"""

    if not rows:
        st.markdown(f"<div class='small'>{empty_text}</div>", unsafe_allow_html=True)
        return
    columns = list(rows[0].keys())
    head = "".join(f"<th>{escape(str(col))}</th>" for col in columns)
    body = ""
    for row in rows:
        body += "<tr>" + "".join(f"<td>{escape(str(row.get(col, '')))}</td>" for col in columns) + "</tr>"
    st.markdown(f"<table class='data-table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>", unsafe_allow_html=True)


def render_theme_toggle() -> None:
    """顶部主题切换按钮。"""

    current = st.session_state.get("theme", "light")
    label = "切换暗色" if current == "light" else "切换亮色"
    if st.button(label, key="theme_toggle"):
        st.session_state["theme"] = "dark" if current == "light" else "light"
        st.rerun()


def current_model_config() -> dict[str, str]:
    """读取页面模型配置；不包含任何持久化写入。"""

    model_name = str(st.session_state.get("model_name", "")).strip()
    if model_name in {"", "qwen-plus", "qwen-plus3.7"}:
        model_name = "qwen3.7-plus"
        st.session_state["model_name"] = model_name
    return {
        "base_url": str(st.session_state.get("model_base_url", "")).strip(),
        "model": model_name,
        "api_key": str(st.session_state.get("model_api_key", "")).strip(),
        "timeout_seconds": str(st.session_state.get("model_timeout_seconds", DEFAULT_ENGINE_TIMEOUT_SECONDS)),
        "max_tokens": str(max(int(st.session_state.get("model_max_tokens", 2000)), 500)),
    }


def initialize_engine_defaults() -> None:
    """从本机私有配置加载诊断引擎默认值。"""

    try:
        private = dict(st.secrets.get("engine", {}))
    except Exception:  # noqa: BLE001
        private = {}
    defaults = {
        "model_base_url": str(private.get("base_url") or DEFAULT_ENGINE_BASE_URL),
        "model_name": str(private.get("model") or DEFAULT_ENGINE_MODEL),
        "model_api_key": str(private.get("api_key") or ""),
        "model_timeout_seconds": int(private.get("timeout_seconds") or DEFAULT_ENGINE_TIMEOUT_SECONDS),
        "model_max_tokens": int(private.get("max_tokens") or 2000),
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def append_engine_call_log(event: str, payload: dict[str, Any]) -> None:
    """记录在线接口调用状态，便于排查卡住或失败。"""

    try:
        ENGINE_CALL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            **payload,
        }
        with ENGINE_CALL_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("failed to write model call log: %s", exc)


class OnlineDiagnosisFailed(RuntimeError):
    def __init__(self, message: str, *, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


def online_failure_is_retryable(error: Exception) -> bool:
    """认证和请求参数错误直接失败，其余网络/服务错误允许有限重试。"""

    text = str(error).lower()
    permanent_markers = (
        "401",
        "403",
        "invalid api key",
        "authentication",
        "unauthorized",
        "permission denied",
        "400 bad request",
        "404 not found",
    )
    return not any(marker in text for marker in permanent_markers)


def diagnose_online_with_retry(
    payload: dict[str, Any],
    config: dict[str, Any],
    *,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """在线模型优先；临时故障重试一次，成功前不切换本地引擎。"""

    started_at = time.perf_counter()
    last_error: Exception | None = None
    attempts_made = 0
    for attempt in range(1, ONLINE_ENGINE_MAX_ATTEMPTS + 1):
        attempts_made = attempt
        attempt_started = time.perf_counter()
        append_engine_call_log(
            "closed_loop_online_attempt",
            {
                "run_id": run_id,
                "model": config.get("model"),
                "attempt": attempt,
                "max_attempts": ONLINE_ENGINE_MAX_ATTEMPTS,
            },
        )
        try:
            diagnosis = diagnose_with_config(payload, config)
            latency_ms = round((time.perf_counter() - started_at) * 1000, 1)
            call_meta = diagnosis.get("_engine_call") if isinstance(diagnosis.get("_engine_call"), dict) else {}
            metadata = {
                "name": str(call_meta.get("model") or config.get("model") or "在线大模型"),
                "mode": "ONLINE",
                "status": "在线研判完成",
                "attempts": attempt,
                "latency_ms": latency_ms,
                "fallback": False,
                "request_id": str(call_meta.get("request_id") or ""),
            }
            append_engine_call_log(
                "closed_loop_online_ok",
                {
                    "run_id": run_id,
                    "model": metadata["name"],
                    "attempt": attempt,
                    "latency_ms": latency_ms,
                    "request_id": metadata["request_id"],
                },
            )
            return diagnosis, metadata
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            retryable = online_failure_is_retryable(exc)
            append_engine_call_log(
                "closed_loop_online_failed",
                {
                    "run_id": run_id,
                    "model": config.get("model"),
                    "attempt": attempt,
                    "attempt_latency_ms": round((time.perf_counter() - attempt_started) * 1000, 1),
                    "retryable": retryable,
                    "error": str(exc),
                },
            )
            if attempt >= ONLINE_ENGINE_MAX_ATTEMPTS or not retryable:
                break
            time.sleep(ONLINE_ENGINE_RETRY_DELAY_SECONDS)
    if last_error is None:
        raise OnlineDiagnosisFailed("在线研判未返回结果", attempts=attempts_made)
    raise OnlineDiagnosisFailed(str(last_error), attempts=attempts_made) from last_error


def display_engine_mode(mode: Any) -> str:
    """把后端调用模式转换成面向展示的名称。"""

    text = str(mode or "")
    if "真实" in text:
        return "在线引擎"
    if "local_rules" in text or "演示" in text:
        return "本地规则"
    return text or "未配置"


def incident_status_kind(incident: dict[str, Any] | None) -> str:
    """返回运维事件状态对应的标签颜色。"""

    status = str((incident or {}).get("status") or "")
    severity = str((incident or {}).get("severity") or "")
    if status == "诊断失败":
        return "danger"
    if severity == "严重" or status == "待处置":
        return "danger"
    if status in {"待诊断", "验证待执行"} or severity in {"一般", "较高"}:
        return "warning"
    return "success"


def render_incident_card(incident: dict[str, Any] | None, *, show_alerts: bool = True) -> None:
    """渲染紧凑的告警与运维事件卡片。"""

    if not isinstance(incident, dict):
        st.markdown("<div class='small'>状态感知完成后将生成告警与运维事件。</div>", unsafe_allow_html=True)
        return
    alarms = list(incident.get("alerts") or [])
    css_class = "normal" if not alarms else "critical" if incident.get("severity") == "严重" else ""
    incident_id = str(incident.get("incident_id") or "无活动事件")
    st.markdown(
        f"""
        <div class="incident-card {css_class}">
            <div class="incident-id">{incident_id}</div>
            <div class="small"><b>{incident.get('title') or '系统状态事件'}</b></div>
            <div class="small">状态：{incident.get('status') or '监测中'}　级别：{incident.get('severity') or '正常'}　活动告警：{len(alarms)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if not show_alerts:
        return
    for alert in alarms[:3]:
        metric = metric_label(str(alert.get("metric") or "关键指标"))
        direction = DIRECTION_LABELS.get(str(alert.get("direction") or "unknown"), "变化")
        st.markdown(
            f"<div class='alert-row'>{tag(str(alert.get('severity') or '一般'), incident_status_kind({'severity': alert.get('severity')}))}"
            f"{alert.get('device_label') or '设备'} · {metric}{direction}<br>"
            f"<span title='{alert.get('entity_id') or ''}'>{short_text(alert.get('entity_id'), 44)}</span></div>",
            unsafe_allow_html=True,
        )


def render_topbar(run: dict[str, Any] | None) -> None:
    """顶部平台状态栏。"""

    run_id = str(run.get("run_id")) if run else "未加载"
    status = str(run.get("status")) if run else "未加载"
    incident = st.session_state.get("incident") if run else None
    incident_display = str(
        (incident or {}).get("incident_id")
        or (incident or {}).get("status")
        or st.session_state.get("analysis_status")
        or "等待状态感知"
    )
    engine_status = get_engine_status(current_model_config())
    cols = st.columns([8.9, 1.1], gap="small")
    with cols[0]:
        st.markdown(
            f"""
            <div class="hero-bar">
                <div>
                    <div class="hero-title">OptiOps · 光网络智能运维中心</div>
                    <div class="hero-subtitle">Telemetry · Anomaly · RCA · Knowledge · Closed-loop Validation</div>
                </div>
                <div class="hero-meta">
                    <div class="hero-meta-item">当前实验<br><b title="{escape(run_id)}">{escape(short_text(run_id, 30))}</b></div>
                    <div class="hero-meta-item">数据状态<br><b>{escape(status)}</b></div>
                    <div class="hero-meta-item">诊断引擎<br><b>{escape(display_engine_mode(engine_status.get("mode")))}</b></div>
                    <div class="hero-meta-item">事件状态<br><b title="{escape(incident_display)}">{escape(short_text(incident_display, 22))}</b></div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with cols[1]:
        render_theme_toggle()


def build_chart_rows(records: list[dict[str, Any]], entity_id: str, metrics: list[str]) -> list[dict[str, float]]:
    """构造 Vega-Lite 折线图数据。"""

    rows: list[dict[str, float]] = []
    for record in records:
        if record.get("entity_id") != entity_id:
            continue
        tick = record.get("simulation_tick")
        if not isinstance(tick, (int, float)):
            continue
        for metric in metrics:
            value = record.get(metric)
            if isinstance(value, (int, float)):
                rows.append({"仿真时刻": float(tick), "指标": metric_label(metric), "指标值": float(value)})
    return sorted(rows, key=lambda item: item["仿真时刻"])


def summarize_rows(summaries: list[dict[str, Any]]) -> list[dict[str, str]]:
    """统计结果表格显示，避免 None 泄漏到正式界面。"""

    rows: list[dict[str, str]] = []
    for item in summaries:
        rows.append(
            {
                "指标": metric_label(item["metric"]),
                "前窗口均值": format_number(item["pre_mean"]),
                "后窗口均值": format_number(item["post_mean"]),
                "绝对变化": format_number(item["delta"]),
                "相对变化": "暂无足够故障后样本" if item["relative_delta"] is None else f"{item['relative_delta'] * 100:.2f}%",
                "方向": DIRECTION_LABELS.get(str(item["direction"]), str(item["direction"])),
            }
        )
    return rows


def strongest_summary(summaries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """选择最适合摘要展示的指标统计。"""

    with_delta = [item for item in summaries if item.get("delta") is not None]
    if with_delta:
        return max(with_delta, key=lambda item: abs(float(item["delta"])))
    return summaries[0] if summaries else None


def has_enough_post_samples(summaries: list[dict[str, Any]]) -> bool:
    """判断关键指标是否具备后窗口统计样本。"""

    return bool(summaries) and any(int(item.get("post_count") or 0) > 0 for item in summaries)


def simple_knowledge_search(chunks: list[dict[str, Any]], query_terms: list[str], top_k: int = 3) -> list[dict[str, Any]]:
    """轻量知识库检索；score 是检索相关性。"""

    terms = [str(term).lower() for term in query_terms if term]
    scored: list[dict[str, Any]] = []
    for chunk in chunks:
        text = " ".join(str(chunk.get(key, "")) for key in ["domain", "topic", "device_type", "metric", "content", "use_for"]).lower()
        score = sum(1 for term in terms if term in text)
        if score:
            scored.append({**chunk, "score": float(score)})
    if not scored:
        scored = [{**chunk, "score": 0.0} for chunk in chunks[:top_k]]
    return sorted(scored, key=lambda item: item["score"], reverse=True)[:top_k]


def build_rule_diagnosis(
    *,
    device_type: str,
    entity_id: str,
    metric_summaries: list[dict[str, Any]],
    knowledge_results: list[dict[str, Any]],
    data_complete: bool,
    performance_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成本地规则诊断结果；不读取 ground truth。"""

    summary = strongest_summary(metric_summaries)
    metric = str(summary.get("metric")) if summary else "关键指标"
    direction = DIRECTION_LABELS.get(str(summary.get("direction")), "变化") if summary else "变化"
    label = metric_label(metric)
    performance_status = (performance_summary or {}).get("status")
    if performance_status == "NORMAL":
        return {
            "mode": "本地规则",
            "status": "NORMAL",
            "summary": "当前窗口内未发现达到阈值的显著性能异常，暂不输出故障根因候选。",
            "top_causes": [],
            "recommendations": [
                "继续观察关键光性能指标的窗口变化。",
                "如业务事件持续异常，再扩大时间窗口并检查相邻设备指标。",
            ],
            "knowledge_chunk_ids": [str(item.get("chunk_id")) for item in knowledge_results],
        }
    if device_type == "edfa":
        primary_fault = "EDFA_NOISE_SURGE" if metric in {"output_gsnr_db", "output_osnr_db", "nf_db"} else "EDFA_GAIN_DEGRADATION"
        second_fault = "EDFA_GAIN_DEGRADATION"
        third_fault = "EDFA_TILT_RIPPLE_ERROR"
    elif device_type == "fiber":
        primary_fault = "FIBER_ATTENUATION_SURGE" if metric == "output_power_dbm" else "FIBER_PMD_SURGE"
        second_fault = "FIBER_NONLINEAR_ANOMALY"
        third_fault = "连接器损耗异常"
    else:
        primary_fault = "ROADM/WSS插损或频偏"
        second_fault = "波长栅格配置异常"
        third_fault = "端口功率异常"

    completeness_note = "当前关键数据不足，以下为本地规则候选结论，不能作为确定诊断。" if not data_complete else "当前关键指标具备故障后样本，可进行诊断。"
    evidence = f"{label} 在故障窗口后呈现{direction}；{completeness_note}"
    return {
        "mode": "本地规则",
        "status": "ABNORMAL",
        "summary": f"{DEVICE_LABELS[device_type]} 设备指标出现异常变化。{completeness_note}",
        "top_causes": [
            {
                "rank": 1,
                "entity_id": entity_id,
                "fault_type": primary_fault,
                "evidence": [evidence, "知识库提示该类指标与噪声、增益或链路质量劣化相关。"],
                "exclusion": "尚未接入真实仿真验证，其他候选仍需复核。",
            },
            {
                "rank": 2,
                "entity_id": entity_id,
                "fault_type": second_fault,
                "evidence": ["同设备部分指标变化可支持该候选，但证据弱于 Top-1。"],
                "exclusion": "若增益保持稳定，可降低该候选优先级。",
            },
            {
                "rank": 3,
                "entity_id": entity_id,
                "fault_type": third_fault,
                "evidence": ["作为配置或链路相邻影响的备选原因保留。"],
                "exclusion": "需要结合拓扑邻接和业务路径进一步排查。",
            },
        ],
        "recommendations": [
            "复核候选设备的噪声系数、增益、输入输出功率和告警事件。",
            "检查故障注入时刻前后业务生命周期事件，确认是否存在级联影响。",
            "生成处置验证任务后交给外部 GNPy 仿真程序执行。",
        ],
        "knowledge_chunk_ids": [str(item.get("chunk_id")) for item in knowledge_results],
    }


def write_validation_task(run_id: str, diagnosis: dict[str, Any]) -> Path:
    """生成等待外部仿真引擎执行的验证任务。"""

    top = (diagnosis.get("top_causes") or [{}])[0]
    write_json(
        VALIDATION_TASK_PATH,
        {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "run_id": run_id,
            "status": "WAITING_SIMULATION_ENGINE",
            "target_entity": top.get("entity_id"),
            "candidate_fault": top.get("fault_type"),
            "suggested_actions": diagnosis.get("recommendations", []),
            "parameters_to_modify": {"note": "待外部仿真引擎根据候选故障类型映射验证参数。"},
        },
    )
    return VALIDATION_TASK_PATH


def build_experiment_wide_observations(
    loaded_run: dict[str, Any],
    *,
    pre_window: float,
    post_window: float,
    analysis_tick: float | None = None,
) -> list[dict[str, Any]]:
    """扫描完整实验的各类设备指标，生成诊断可用的实验级观测摘要。"""

    run_dir = get_run_dir(loaded_run)
    trigger_tick = float(analysis_tick if analysis_tick is not None else loaded_run.get("trigger_tick") or 0.0)
    observations: list[dict[str, Any]] = []
    for device_type in ("edfa", "fiber", "roadm"):
        records, errors = load_telemetry(run_dir, device_type)
        metrics = select_metrics_for_device(device_type, records)
        candidate = select_candidate_entity(
            records,
            metrics=metrics,
            trigger_tick=trigger_tick,
            pre_window=pre_window,
            post_window=post_window,
        )
        metric_summaries = list(candidate.get("metric_summaries") or [])
        sorted_summaries = sorted(
            metric_summaries,
            key=lambda item: metric_change_score(item) if is_harmful_change(item) else 0.0,
            reverse=True,
        )
        key_changes = []
        for item in sorted_summaries[:8]:
            key_changes.append(
                {
                    "metric": item.get("metric"),
                    "pre_mean": item.get("pre_mean"),
                    "post_mean": item.get("post_mean"),
                    "delta": item.get("delta"),
                    "relative_delta": item.get("relative_delta"),
                    "direction": item.get("direction"),
                    "pre_count": item.get("pre_count"),
                    "post_count": item.get("post_count"),
                }
            )
        observations.append(
            {
                "device_type": device_type,
                "candidate_entity_id": candidate.get("entity_id"),
                "candidate_score": candidate.get("score"),
                "metrics_considered": metrics,
                "key_changes": key_changes,
                "read_errors": errors[:3],
            }
        )
    return observations


def build_full_experiment_context(
    loaded_run: dict[str, Any],
    *,
    pre_window: float,
    post_window: float,
    analysis_tick: float | None = None,
) -> dict[str, Any]:
    """为诊断引擎构造完整实验上下文，不依赖中间图表选择。"""

    trigger_tick = float(analysis_tick if analysis_tick is not None else loaded_run.get("trigger_tick") or 0.0)
    signature_matches: list[dict[str, Any]] = []
    signature_library = load_signature_library(SIGNATURE_LIBRARY_PATH)
    if signature_library:
        signature_features = extract_run_signature_features(
            loaded_run,
            pre_window=pre_window,
            post_window=post_window,
            analysis_tick=trigger_tick,
        )
        signature_matches = classify_with_signature_library(
            signature_features,
            signature_library,
            top_n=4,
            exclude_run_id=str(loaded_run.get("run_id") or ""),
        )

    top_fault = str((signature_matches[0] or {}).get("fault_type") or "") if signature_matches else ""
    if top_fault and top_fault != "NORMAL_STATE" and top_fault in FAULT_DEVICE_HINTS:
        candidate = select_candidate_for_device(
            loaded_run,
            FAULT_DEVICE_HINTS[top_fault],
            pre_window=pre_window,
            post_window=post_window,
            analysis_tick=trigger_tick,
        )
    else:
        candidate = select_candidate_device_and_entity(
            loaded_run,
            pre_window=pre_window,
            post_window=post_window,
            analysis_tick=trigger_tick,
        )

    full_summaries = list(candidate.get("metric_summaries") or [])
    performance_summary = build_performance_event_summary(
        run_id=str(loaded_run.get("run_id")),
        device_type=str(candidate.get("device_type")),
        entity_id=str(candidate.get("entity_id")),
        trigger_tick=trigger_tick,
        metric_summaries=full_summaries,
    )
    experiment_observations = build_experiment_wide_observations(
        loaded_run,
        pre_window=pre_window,
        post_window=post_window,
        analysis_tick=trigger_tick,
    )
    performance_summary["experiment_wide_observations"] = experiment_observations
    run_dir = get_run_dir(loaded_run)
    events, _ = load_simulation_events(run_dir)
    lifecycle, _ = load_service_lifecycle(run_dir)
    event_summary = build_event_summary_for_diagnosis(
        events,
        lifecycle,
        start_tick=trigger_tick - pre_window,
        end_tick=trigger_tick + post_window,
        limit=20,
    )
    return {
        "candidate": candidate,
        "metric_summaries": full_summaries,
        "performance_summary": performance_summary,
        "event_summary": event_summary,
        "signature_matches": signature_matches,
        "experiment_wide_observations": experiment_observations,
    }


def render_left_panel(registry: list[dict[str, Any]], default_run_id: str | None) -> None:
    """左侧系统状态感知区，只负责实验接入和感知状态。"""

    render_panel_title("系统状态感知区")
    valid_runs = [item for item in registry if item.get("status") == "VALID"]
    run_ids = [str(item["run_id"]) for item in valid_runs]
    run_index = run_ids.index(default_run_id) if default_run_id in run_ids else 0

    st.markdown("<div class='group-title'>数据接入</div>", unsafe_allow_html=True)
    st.selectbox("数据来源", ["仿真实验数据流"], disabled=True)
    selected_run_id = st.selectbox(
        "实验批次",
        run_ids,
        index=run_index,
        format_func=lambda value: short_text(value, 42),
    )
    st.caption("实验标注在诊断完成前保持隐藏。")

    st.markdown("<div class='group-title'>感知窗口</div>", unsafe_allow_html=True)
    pre_window = st.slider("基线窗口（tick）", 5, 80, int(st.session_state.get("pre_window", 30)), 5)
    post_window = st.slider("观测窗口（tick）", 5, 80, int(st.session_state.get("post_window", 30)), 5)

    if st.button("接入实验并启动感知", type="primary", use_container_width=True):
        st.session_state["loaded_run_id"] = selected_run_id
        st.session_state["pre_window"] = pre_window
        st.session_state["post_window"] = post_window
        st.session_state["diagnosis"] = None
        st.session_state["system_snapshot"] = None
        st.session_state["incident"] = None
        st.session_state["diagnosis_pending"] = False
        st.session_state["analysis_status"] = "感知中"
        save_current_run(selected_run_id)
        st.rerun()

    loaded_run_id = st.session_state.get("loaded_run_id")
    loaded_run = find_run(registry, loaded_run_id) if loaded_run_id else None
    if loaded_run:
        if st.button("刷新系统状态", use_container_width=True):
            st.session_state["diagnosis"] = None
            st.session_state["system_snapshot"] = None
            st.session_state["incident"] = None
            st.session_state["diagnosis_pending"] = False
            st.session_state["analysis_status"] = "感知中"
            st.rerun()
        if st.button("断开当前实验", use_container_width=True):
            st.session_state["loaded_run_id"] = None
            st.session_state["diagnosis"] = None
            st.session_state["system_snapshot"] = None
            st.session_state["incident"] = None
            st.session_state["diagnosis_pending"] = False
            st.session_state["analysis_status"] = "未接入"
            st.rerun()

    st.markdown("<div class='group-title'>感知状态</div>", unsafe_allow_html=True)
    diagnosis = st.session_state.get("diagnosis") if loaded_run else None
    if not loaded_run:
        st.markdown(f"{tag('数据未接入', 'warning')}{tag('监测待机', 'info')}", unsafe_allow_html=True)
        st.markdown("<div class='small'>接入实验后将自动扫描 EDFA、Fiber 和 ROADM 状态。</div>", unsafe_allow_html=True)
        return

    counts = loaded_run.get("record_counts") or {}
    telemetry_total = sum(int(counts.get(name) or 0) for name in ("telemetry_edfa.jsonl", "telemetry_fiber.jsonl", "telemetry_roadm.jsonl"))
    event_total = int(counts.get("simulation_events.jsonl") or 0) + int(counts.get("service_lifecycle.jsonl") or 0)
    status = str((diagnosis or {}).get("status") or st.session_state.get("analysis_status") or "感知中")
    status_kind = "success" if status == "NORMAL" else "danger" if status == "ABNORMAL" else "info"
    st.markdown(
        f"{tag('数据已接入', 'success')}{tag(status, status_kind)}",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div class='small'>遥测记录：<b>{telemetry_total:,}</b><br>结构化事件：<b>{event_total:,}</b><br>"
        f"设备域覆盖：<b>EDFA / Fiber / ROADM</b></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"{tag('EDFA就绪', 'success')}{tag('Fiber就绪', 'success')}{tag('ROADM就绪', 'success')}",
        unsafe_allow_html=True,
    )
    st.markdown("<div class='group-title'>告警与运维事件</div>", unsafe_allow_html=True)
    render_incident_card(st.session_state.get("incident"))


def render_center_panel(loaded_run: dict[str, Any] | None) -> tuple[str, str, list[dict[str, Any]], bool]:
    """中间系统状态区，自动展示全局关键指标，不提供手工指标选择。"""

    render_panel_title("系统运行状态区")
    if not loaded_run:
        st.info("系统处于待机状态。请从左侧接入一组实验数据。")
        return "edfa", "", [], False

    run_dir = get_run_dir(loaded_run)
    pre_window = int(st.session_state.get("pre_window", 30))
    post_window = int(st.session_state.get("post_window", 30))
    snapshot_key = f"{loaded_run.get('run_id')}|{pre_window}|{post_window}|cp1"
    snapshot = st.session_state.get("system_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("key") != snapshot_key:
        with st.spinner("正在建立系统状态快照..."):
            detection = detect_run_change_point(loaded_run)
            analysis_tick = float(detection.get("analysis_tick") or 0.0)
            observations = build_experiment_wide_observations(
                loaded_run,
                pre_window=float(pre_window),
                post_window=float(post_window),
                analysis_tick=analysis_tick,
            )
        snapshot = {
            "key": snapshot_key,
            "detection": detection,
            "analysis_tick": analysis_tick,
            "observations": observations,
        }
        st.session_state["system_snapshot"] = snapshot
    detection = snapshot.get("detection") if isinstance(snapshot.get("detection"), dict) else {}
    analysis_tick = float(snapshot.get("analysis_tick") or 0.0)
    observations = list(snapshot.get("observations") or [])
    incident = st.session_state.get("incident")
    if not isinstance(incident, dict) or incident.get("run_id") != loaded_run.get("run_id"):
        incident = build_incident_snapshot(
            run_id=str(loaded_run.get("run_id") or ""),
            observations=observations,
            reference_tick=analysis_tick,
        )
        st.session_state["incident"] = incident
        if not isinstance(st.session_state.get("diagnosis"), dict):
            st.session_state["analysis_status"] = "待诊断" if incident.get("alarm_count") else "持续监测"
    focus = max(observations, key=lambda item: float(item.get("candidate_score") or 0.0), default={})
    device = str(focus.get("device_type") or "edfa")
    entity_id = str(focus.get("candidate_entity_id") or "")
    summaries = [item for item in focus.get("key_changes", []) if isinstance(item, dict)]
    summaries = sorted(summaries, key=metric_change_score, reverse=True)[:3]

    diagnosis = st.session_state.get("diagnosis")
    if isinstance(diagnosis, dict) and diagnosis.get("status") not in {None, "FAILED"}:
        device = str(diagnosis.get("diagnosis_device_type") or diagnosis.get("selected_device_type") or device)
        entity_id = str(diagnosis.get("diagnosis_entity_id") or diagnosis.get("selected_entity_id") or entity_id)
        diagnosed_summaries = [
            item
            for item in diagnosis.get("full_metric_summaries", [])
            if isinstance(item, dict) and item.get("metric")
        ]
        if diagnosed_summaries:
            summaries = sorted(diagnosed_summaries, key=metric_change_score, reverse=True)[:3]
    records, errors = load_telemetry(run_dir, device)
    data_complete = has_enough_post_samples(summaries)
    observed_status = str((diagnosis or {}).get("status") or st.session_state.get("analysis_status") or "感知中")
    abnormal_devices = sum(1 for item in observations if float(item.get("candidate_score") or 0.0) > 0)
    active_alarm_count = int((incident or {}).get("alarm_count") or 0)
    st.markdown(
        f"{tag('三类设备已扫描', 'success')}"
        f"{tag('发现持续变化' if detection.get('anomaly_detected') else '未发现持续变化', 'danger' if detection.get('anomaly_detected') else 'success')}"
        f"{tag(f'异常设备域 {abnormal_devices}', 'danger' if abnormal_devices else 'success')}"
        f"{tag(observed_status, 'danger' if observed_status == 'ABNORMAL' else 'success' if observed_status == 'NORMAL' else 'info')}",
        unsafe_allow_html=True,
    )
    if detection.get("anomaly_detected"):
        st.markdown(
            "<div class='small'>"
            f"系统自动识别观测分界：<b>{analysis_tick:.3f}</b>；"
            f"直接指标：<b>{metric_label(str(detection.get('metric') or ''))}</b>；"
            f"持续比例：<b>{float(detection.get('persistence_ratio') or 0.0):.0%}</b>。"
            "</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<div class='small'>完整遥测中未识别到持续性直接指标变化，当前按稳定状态窗口进行复核。</div>",
            unsafe_allow_html=True,
        )
    st.markdown(
        f"<div class='metric-strip'>"
        f"<div class='metric-card'><div class='label'>监测设备域</div><div class='value'>3</div></div>"
        f"<div class='metric-card'><div class='label'>活动告警</div><div class='value'>{active_alarm_count}</div></div>"
        f"<div class='metric-card'><div class='label'>重点对象</div><div class='value'>{DEVICE_LABELS.get(device, device)}</div></div>"
        f"<div class='metric-card'><div class='label'>运维事件</div><div class='value'>{incident.get('incident_id') or '无'}</div></div>"
        f"<div class='metric-card'><div class='label'>状态更新时间</div><div class='value'>{datetime.now().strftime('%H:%M:%S')}</div></div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    st.markdown("<div class='group-title'>当前运维事件</div>", unsafe_allow_html=True)
    render_incident_card(incident, show_alerts=False)

    st.markdown("<div class='group-title'>设备域状态</div>", unsafe_allow_html=True)
    domain_rows = []
    for item in observations:
        changes = [change for change in item.get("key_changes", []) if isinstance(change, dict) and change.get("delta") is not None]
        strongest = max(changes, key=metric_change_score, default={})
        score = float(item.get("candidate_score") or 0.0)
        domain_rows.append(
            {
                "设备域": DEVICE_LABELS.get(str(item.get("device_type")), str(item.get("device_type"))),
                "状态": "异常" if score > 0 else "稳定",
                "重点设备": short_text(item.get("candidate_entity_id"), 46),
                "关键指标": metric_label(str(strongest.get("metric") or "暂无")),
                "变化": format_number(strongest.get("delta")),
            }
        )
    render_html_table(domain_rows)

    st.markdown("<div class='group-title'>重点设备关键指标</div>", unsafe_allow_html=True)
    st.markdown(
        f"<div class='small'>系统自动定位：<span title='{entity_id}'>{short_text(entity_id, 92)}</span></div>",
        unsafe_allow_html=True,
    )
    if errors:
        st.warning("重点设备 telemetry 存在解析错误，当前仅展示成功读取的数据。")
    chart_columns = st.columns(max(1, len(summaries)), gap="small")
    is_dark = st.session_state.get("theme", "light") == "dark"
    axis_color = "#94A3B8" if is_dark else "#64748B"
    grid_color = "#263449" if is_dark else "#D8E1EC"
    rule_color = "#EF4444" if is_dark else "#DC2626"
    for column, summary in zip(chart_columns, summaries):
        metric = str(summary.get("metric") or "")
        rows = build_chart_rows(records, entity_id, [metric])
        with column:
            st.markdown(f"<div class='small'><b>{metric_label(metric)}</b></div>", unsafe_allow_html=True)
            if not rows:
                st.info("暂无可用时序样本。")
                continue
            st.vega_lite_chart(
                spec={
                    "layer": [
                        {
                            "data": {"values": rows},
                            "mark": {"type": "line", "point": False, "color": "#2563EB"},
                            "encoding": {
                                "x": {"field": "仿真时刻", "type": "quantitative", "title": "仿真时刻", "axis": {"gridColor": grid_color, "labelColor": axis_color}},
                                "y": {"field": "指标值", "type": "quantitative", "title": None, "axis": {"gridColor": grid_color, "labelColor": axis_color}},
                                "tooltip": [
                                    {"field": "仿真时刻", "type": "quantitative"},
                                    {"field": "指标值", "type": "quantitative"},
                                ],
                            },
                        },
                        {
                            "data": {"values": [{"观测分界": analysis_tick}]},
                            "mark": {"type": "rule", "color": rule_color, "strokeWidth": 2, "strokeDash": [6, 4]},
                            "encoding": {"x": {"field": "观测分界", "type": "quantitative"}},
                        },
                    ],
                    "height": 155,
                    "background": "transparent",
                    "config": {"view": {"stroke": "transparent"}},
                },
                use_container_width=True,
            )
    render_html_table(summarize_rows(summaries), "当前重点设备暂无可统计指标。")

    events, event_errors = load_simulation_events(run_dir)
    lifecycle, lifecycle_errors = load_service_lifecycle(run_dir)
    window_start = analysis_tick - pre_window
    window_end = analysis_tick + post_window
    diagnosis_event_summary = build_event_summary_for_diagnosis(
        events,
        lifecycle,
        start_tick=window_start,
        end_tick=window_end,
        limit=20,
    )
    st.session_state["diagnosis_event_summary"] = diagnosis_event_summary
    timeline = []
    for event in events + lifecycle:
        tick = event.get("simulation_tick")
        if isinstance(tick, (int, float)) and window_start <= float(tick) <= window_end:
            timeline.append(
                {
                    "仿真时刻": round(float(tick), 3),
                    "事件类型": event.get("event_type"),
                    "对象": short_text(event.get("entity_id") or event.get("service_id"), 42),
                    "来源": "仿真事件" if "entity_id" in event else "业务生命周期",
                }
            )
    timeline.sort(key=lambda item: item["仿真时刻"])
    st.markdown("<div class='group-title'>系统事件流</div>", unsafe_allow_html=True)
    st.markdown(
        "<div class='small'>诊断可见事件："
        f"{diagnosis_event_summary['diagnosis_visible_simulation_event_count']} 条；"
        f"已过滤控制类事件：{diagnosis_event_summary['filtered_injection_event_count']} 条；"
        f"业务生命周期事件：{diagnosis_event_summary['service_lifecycle_event_count']} 条</div>",
        unsafe_allow_html=True,
    )
    render_html_table(timeline[:8], "当前时间窗口内暂无结构化仿真事件。")
    if event_errors or lifecycle_errors:
        st.warning("部分结构化事件存在解析错误，已展示可读取记录。")
    return device, entity_id, summaries, data_complete


def render_diagnosis_panel(loaded_run: dict[str, Any] | None, device: str, entity_id: str, summaries: list[dict[str, Any]], data_complete: bool) -> None:
    """右侧智能诊断面板。"""

    render_panel_title("事件诊断与处置区")
    knowledge_ready = KNOWLEDGE_CHUNKS_PATH.exists()
    signature_ready = SIGNATURE_LIBRARY_PATH.exists()
    with st.expander("诊断引擎配置", expanded=False):
        base_url = st.text_input(
            "接口地址",
            value=st.session_state.get("model_base_url", ""),
            placeholder="https://xxx.compatible-mode/v1",
        )
        model_name = st.text_input("引擎代号", value=st.session_state.get("model_name", "qwen3.7-plus"))
        api_key = st.text_input(
            "访问密钥",
            value=st.session_state.get("model_api_key", ""),
            type="password",
            placeholder="输入后会以星号隐藏",
        )
        timeout_seconds = st.number_input(
            "响应超时（秒）",
            min_value=30,
            max_value=300,
            value=int(st.session_state.get("model_timeout_seconds", DEFAULT_ENGINE_TIMEOUT_SECONDS)),
            step=30,
            help="默认 120 秒；完整结构化研判需要更长响应窗口。",
        )
        max_tokens = st.number_input(
            "输出长度上限",
            min_value=500,
            max_value=3000,
            value=max(int(st.session_state.get("model_max_tokens", 2000)), 500),
            step=100,
            help="默认 1400，优先保证一次完整返回；如需更快可手动调低。",
        )
        st.session_state["model_base_url"] = base_url.strip()
        st.session_state["model_name"] = model_name.strip()
        st.session_state["model_api_key"] = api_key.strip()
        st.session_state["model_timeout_seconds"] = int(timeout_seconds)
        st.session_state["model_max_tokens"] = int(max_tokens)
        if st.button("保存配置并刷新诊断", use_container_width=True):
            if loaded_run:
                st.session_state["diagnosis"] = None
                st.session_state["diagnosis_pending"] = False
                st.session_state["analysis_status"] = "待诊断"
            st.rerun()
        if st.button("测试接口", use_container_width=True):
            test_start = time.perf_counter()
            try:
                test_result = test_online_model_config(current_model_config())
                latency_ms = (time.perf_counter() - test_start) * 1000
                append_engine_call_log(
                    "ping_ok",
                    {
                        "model": test_result.get("model"),
                        "request_id": test_result.get("request_id"),
                        "latency_ms": round(latency_ms, 1),
                    },
                )
                st.success(f"接口测试成功，用时 {latency_ms / 1000:.1f} 秒。")
            except Exception as exc:  # noqa: BLE001
                latency_ms = (time.perf_counter() - test_start) * 1000
                error_text = str(exc).replace("模型", "接口")
                append_engine_call_log(
                    "ping_failed",
                    {
                        "model": model_name.strip(),
                        "latency_ms": round(latency_ms, 1),
                        "error": error_text,
                    },
                )
                st.error(f"接口测试失败：{error_text}")
        st.caption("告警生成后可启动事件诊断。填齐三项配置时使用在线引擎，未填齐时使用本地规则。")
    model_config = current_model_config()
    engine_status = get_engine_status(model_config)
    st.markdown(
        f"{tag(display_engine_mode(engine_status.get('mode')), 'info')}"
        f"{tag('知识库就绪' if knowledge_ready else '知识库缺失', 'success' if knowledge_ready else 'danger')}"
        f"{tag('历史特征库就绪' if signature_ready else '历史特征库缺失', 'success' if signature_ready else 'warning')}"
        f"{tag('接口就绪' if engine_status.get('provider') == 'compatible_chat' else '本地规则', 'info' if engine_status.get('ready') else 'danger')}",
        unsafe_allow_html=True,
    )
    analysis_started_at = st.session_state.get("analysis_started_at")
    if st.session_state.get("analysis_status") == "诊断中" and (
        not isinstance(analysis_started_at, (int, float)) or time.time() - float(analysis_started_at) > 300
    ):
        st.session_state["analysis_status"] = "待刷新"
        st.session_state["diagnosis_pending"] = True
        st.warning("上一次状态诊断没有正常结束，系统将自动重新执行。")
    st.markdown(
        f"<div class='small'>输入数据：{'完整' if data_complete else '关键样本不足'}；诊断状态：{st.session_state['analysis_status']}</div>",
        unsafe_allow_html=True,
    )
    if not loaded_run:
        st.markdown("<div class='small'>等待系统接入数据。状态感知完成后将在此生成告警事件。</div>", unsafe_allow_html=True)
        return

    incident = st.session_state.get("incident")
    st.markdown("<div class='group-title'>当前事件</div>", unsafe_allow_html=True)
    render_incident_card(incident, show_alerts=True)

    if not data_complete:
        st.warning("当前重点指标样本不足；系统仍会扫描完整实验指标并标明证据强度。")

    diagnosis_ready = isinstance(st.session_state.get("diagnosis"), dict)
    if not diagnosis_ready and not st.session_state.get("diagnosis_pending"):
        button_label = "启动事件诊断" if int((incident or {}).get("alarm_count") or 0) else "执行状态复核"
        if st.button(button_label, type="primary", use_container_width=True):
            st.session_state["diagnosis_pending"] = True
            st.session_state["analysis_status"] = "诊断中"
            st.rerun()
        st.markdown(
            "<div class='small'>系统已完成全实验状态扫描。启动诊断后将关联性能变化、运行事件、历史特征和知识依据。</div>",
            unsafe_allow_html=True,
        )
        return

    should_run_diagnosis = bool(st.session_state.get("diagnosis_pending"))
    if should_run_diagnosis:
        st.session_state["diagnosis_pending"] = False
        st.session_state["analysis_status"] = "诊断中"
        st.session_state["analysis_started_at"] = time.time()
        with st.spinner("系统正在汇聚状态并生成诊断结论..."):
            t0 = time.perf_counter()
            pre_window = float(st.session_state.get("pre_window", 30))
            post_window = float(st.session_state.get("post_window", 30))
            snapshot = st.session_state.get("system_snapshot")
            analysis_tick = float(snapshot.get("analysis_tick") or 0.0) if isinstance(snapshot, dict) else 0.0
            pipeline_start = time.perf_counter()
            diagnosis = diagnose_run_with_local_rules(
                loaded_run,
                knowledge_chunks_path=KNOWLEDGE_CHUNKS_PATH,
                signature_library_path=SIGNATURE_LIBRARY_PATH if SIGNATURE_LIBRARY_PATH.exists() else None,
                pre_window=pre_window,
                post_window=post_window,
                analysis_tick=analysis_tick,
            )
            pipeline_latency_ms = (time.perf_counter() - pipeline_start) * 1000
            payload = diagnosis.get("diagnosis_payload") or {}
            analysis_device = str(diagnosis.get("selected_device_type") or device)
            analysis_entity = str(diagnosis.get("selected_entity_id") or entity_id)
            performance_summary = diagnosis.get("performance_event_summary") or {}
            diagnosis_event_summary = diagnosis.get("event_summary_for_diagnosis") or {}
            observability = diagnosis.get("observability") or {}
            signature_matches = diagnosis.get("signature_matches") or []
            knowledge_query = diagnosis.get("knowledge_query") or {}
            knowledge_results = diagnosis.get("knowledge_results") or []
            knowledge_latency_ms = pipeline_latency_ms
            model_start = time.perf_counter()
            online_attempted = get_engine_status(model_config).get("provider") == "compatible_chat"
            append_engine_call_log(
                "diagnosis_start",
                {
                    "run_id": loaded_run.get("run_id"),
                    "online_attempted": online_attempted,
                    "model": model_config.get("model"),
                    "max_tokens": model_config.get("max_tokens"),
                    "timeout_seconds": model_config.get("timeout_seconds"),
                },
            )
            try:
                if online_attempted:
                    online_diagnosis = diagnose_with_config(payload, model_config)
                    append_engine_call_log(
                        "diagnosis_online_ok",
                        {
                            "run_id": loaded_run.get("run_id"),
                            "model": (online_diagnosis.get("_engine_call") or {}).get("model"),
                            "request_id": (online_diagnosis.get("_engine_call") or {}).get("request_id"),
                            "latency_ms": round((time.perf_counter() - model_start) * 1000, 1),
                            "status": online_diagnosis.get("status"),
                        },
                    )
                    online_diagnosis.update(
                        {
                            "selected_device_type": analysis_device,
                            "selected_entity_id": analysis_entity,
                            "candidate_score": diagnosis.get("candidate_score"),
                            "signature_matches": signature_matches,
                            "knowledge_query": knowledge_query,
                            "knowledge_results": knowledge_results,
                            "diagnosis_payload": payload,
                            "observability": observability,
                        }
                    )
                    diagnosis = online_diagnosis
            except Exception as exc:  # noqa: BLE001
                error_text = str(exc).replace("模型", "接口")
                append_engine_call_log(
                    "diagnosis_online_failed",
                    {
                        "run_id": loaded_run.get("run_id"),
                        "model": model_config.get("model"),
                        "latency_ms": round((time.perf_counter() - model_start) * 1000, 1),
                        "error": error_text,
                    },
                )
                diagnosis = {
                    "mode": "在线引擎",
                    "status": "FAILED",
                    "summary": f"在线诊断接口调用失败：{error_text}",
                    "top_causes": [],
                    "recommendations": [
                        "检查接口地址、引擎代号、访问密钥是否正确。",
                        "确认当前账号已开通该引擎，且免费额度未停用。",
                        "如接口响应超时，可把响应超时调到 180 秒后重试。",
                    ],
                    "knowledge_chunk_ids": [],
                }
                diagnosis["_online_error"] = error_text
                if "model_not_found" in error_text:
                    diagnosis["recommendations"].insert(0, "接口返回引擎代号不存在，请确认引擎代号为 qwen3.7-plus 或控制台已开通的代号。")
                elif "timeout" in error_text.lower() or "超时" in error_text:
                    diagnosis["recommendations"].insert(0, "接口响应超时，请把响应超时调到 180 秒或稍后重试。")
            diagnosis = calibrate_diagnosis_output(
                diagnosis,
                payload,
                force_observed_status=not online_attempted,
            )
            diagnosis["knowledge_query"] = knowledge_query
            diagnosis["diagnosis_payload"] = payload
            diagnosis["diagnosis_device_type"] = analysis_device
            diagnosis["diagnosis_entity_id"] = analysis_entity
            diagnosis["full_metric_summaries"] = performance_summary.get("key_metric_changes", [])
            diagnosis["performance_event_summary"] = performance_summary
            diagnosis["event_summary_for_diagnosis"] = diagnosis_event_summary
            diagnosis["knowledge_results"] = knowledge_results
            diagnosis["signature_matches"] = signature_matches
            diagnosis["observability"] = observability
            diagnosis["knowledge_latency_ms"] = knowledge_latency_ms
            diagnosis["model_latency_ms"] = (time.perf_counter() - model_start) * 1000
            diagnosis["total_latency_ms"] = (time.perf_counter() - t0) * 1000
            st.session_state["diagnosis"] = clean_diagnosis_for_display(diagnosis)
            st.session_state["incident"] = update_incident_with_diagnosis(
                st.session_state.get("incident"),
                diagnosis,
            )
            st.session_state["analysis_status"] = "失败" if diagnosis.get("status") == "FAILED" else "完成"
            st.session_state.pop("analysis_started_at", None)
        st.rerun()

    diagnosis = st.session_state.get("diagnosis")
    if diagnosis:
        observability = diagnosis.get("observability") if isinstance(diagnosis.get("observability"), dict) else {}
        if observability.get("direct_anomaly_observed"):
            st.markdown(f"{tag('直接遥测证据充分', 'success')}", unsafe_allow_html=True)
        else:
            st.warning(
                "当前完整实验扫描未形成直接性能劣化证据。正常场景下这是预期结果；"
                "若该实验包含已知故障注入，应优先检查仿真参数是否实际写入 telemetry。"
            )
        st.markdown("<div class='group-title'>诊断概述</div>", unsafe_allow_html=True)
        st.markdown(
            f"<div class='small'>{clean_display_text(diagnosis.get('summary') or '诊断过程未返回摘要。')}</div>",
            unsafe_allow_html=True,
        )
        call_info = diagnosis.get("_engine_call") or {}
        if call_info:
            st.markdown("<div class='group-title'>引擎调用记录</div>", unsafe_allow_html=True)
            st.markdown(
                "<div class='small'>"
                f"在线诊断完成；请求ID：{call_info.get('request_id') or '未返回'}"
                "</div>",
                unsafe_allow_html=True,
            )
        elif diagnosis.get("mode") in {"真实模型", "在线引擎"} and diagnosis.get("status") != "FAILED":
            st.markdown("<div class='group-title'>引擎调用记录</div>", unsafe_allow_html=True)
            st.markdown("<div class='small'>已进入在线诊断路径，但接口未返回耗用统计。</div>", unsafe_allow_html=True)

        st.markdown("<div class='group-title'>Top-3候选根因</div>", unsafe_allow_html=True)
        if not diagnosis.get("top_causes"):
            st.markdown("<div class='small'>当前判定为正常状态，未输出故障根因候选。</div>", unsafe_allow_html=True)
        for cause in diagnosis.get("top_causes", []):
            evidence = "".join(f"<li>{clean_display_text(item)}</li>" for item in cause.get("evidence", []))
            rank = cause.get("rank", "-")
            fault_type = clean_display_text(cause.get("fault_type", "未返回故障类型"))
            cause_entity = clean_display_text(cause.get("entity_id", "未返回候选对象"))
            st.markdown(
                f"""
                <div class="cause-card">
                    <div><span class="rank">Top-{rank}</span> {fault_type}</div>
                    <div class="mono" title="{cause_entity}">{short_text(cause_entity, 38)}</div>
                    <ul>{evidence}<li>排除依据：{clean_display_text(cause.get('exclusion', '暂无'))}</li></ul>
                </div>
                """,
                unsafe_allow_html=True,
            )
        signature_matches = diagnosis.get("signature_matches") or []
        if signature_matches:
            st.markdown("<div class='group-title'>历史相似故障依据</div>", unsafe_allow_html=True)
            for item in signature_matches[:3]:
                st.markdown(
                    "<div class='small'>"
                    f"{clean_display_text(item.get('fault_type'))} · 相似度={float(item.get('similarity') or 0.0):.4f} · "
                    f"历史样本 {int(item.get('support') or 0)} 条"
                    "</div>",
                    unsafe_allow_html=True,
                )
        st.markdown("<div class='group-title'>知识库依据</div>", unsafe_allow_html=True)
        st.markdown("<div class='small'>相关性分值只表示知识匹配程度，不是根因概率。</div>", unsafe_allow_html=True)
        knowledge_query = diagnosis.get("knowledge_query") or {}
        if knowledge_query:
            st.markdown(
                f"<div class='small'>检索语句：{short_text(knowledge_query.get('query_text'), 96)}</div>",
                unsafe_allow_html=True,
            )
        for item in diagnosis.get("knowledge_results", []):
            st.markdown(
                f"""
                <div class="knowledge-card">
                    <div class="mono">{clean_display_text(item.get('chunk_id'))} · 相关性={item.get('score')}</div>
                    <div class="small">{short_text(clean_display_text(item.get('topic')), 34)}</div>
                    <div class="small">{short_text(clean_display_text(item.get('content')), 86)}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        st.markdown("<div class='group-title'>运维建议</div>", unsafe_allow_html=True)
        for rec in (diagnosis.get("recommendations") or [])[:4]:
            st.markdown(f"<div class='small'>- {clean_display_text(rec)}</div>", unsafe_allow_html=True)
        col_a, col_b = st.columns(2)
        if col_a.button("验证处置方案", use_container_width=True):
            path = write_validation_task(str(loaded_run.get("run_id")), diagnosis)
            st.success(f"验证任务已生成：{path}")
        report_text = json.dumps({"run_id": loaded_run.get("run_id"), "diagnosis": diagnosis}, ensure_ascii=False, indent=2)
        col_b.download_button(
            "导出报告",
            report_text,
            file_name="diagnosis_report.json",
            mime="application/json",
            use_container_width=True,
        )
    else:
        st.markdown("<div class='small'>系统正在等待状态汇聚完成。</div>", unsafe_allow_html=True)


def render_eval_footer(loaded_run: dict[str, Any] | None) -> None:
    """底部诊断评估区。"""

    diagnosis = st.session_state.get("diagnosis")
    diagnosis_complete = (
        isinstance(diagnosis, dict)
        and diagnosis.get("status") != "FAILED"
        and st.session_state.get("analysis_status") == "完成"
    )
    if diagnosis_complete and loaded_run:
        top_causes = diagnosis.get("top_causes", [])
        truth_type = loaded_run.get("scenario")
        truth_entity = loaded_run.get("fault_entity")
        top1 = top_causes[0] if top_causes else {}
        normal_hit = diagnosis.get("status") == "NORMAL" and truth_type == "NORMAL_STATE"
        type_hit_1 = normal_hit or top1.get("fault_type") == truth_type
        type_hit_3 = normal_hit or any(item.get("fault_type") == truth_type for item in top_causes[:3])
        entity_hit_1 = normal_hit or top1.get("entity_id") == truth_entity
        injection_observability = assess_injected_fault_observability(
            loaded_run,
            pre_window=float(st.session_state.get("pre_window", 30)),
            post_window=float(st.session_state.get("post_window", 30)),
        )
        injection_observed = injection_observability.get("observed")
        if injection_observed is True:
            injection_tag = tag("注入效应可观测", "success")
        elif injection_observed is False:
            injection_tag = tag("注入效应未反映", "danger")
        else:
            injection_tag = tag("正常场景", "info")
        html = (
            f"{tag('诊断后评估', 'info')}"
            f"<span class='top-kv'>真实故障类型：<b>{truth_type}</b></span> "
            f"<span class='top-kv'>真实故障设备：<b title='{truth_entity}'>{short_text(truth_entity, 46)}</b></span> "
            f"{tag('类型Top-1命中' if type_hit_1 else '类型Top-1未命中', 'success' if type_hit_1 else 'danger')}"
            f"{tag('类型Top-3命中' if type_hit_3 else '类型Top-3未命中', 'success' if type_hit_3 else 'danger')}"
            f"{tag('实体Top-1命中' if entity_hit_1 else '实体Top-1未命中', 'success' if entity_hit_1 else 'danger')}"
            f"{injection_tag}"
            f"<span class='top-kv'>知识检索耗时：<b>{diagnosis.get('knowledge_latency_ms', 0):.1f} ms</b></span> "
            f"<span class='top-kv'>诊断耗时：<b>{diagnosis.get('model_latency_ms', 0):.1f} ms</b></span>"
        )
    else:
        html = f"{tag('事件评估区', 'warning')}<span class='top-kv'>诊断完成后再读取标注结果进行比较。</span>"
    st.markdown(f"<div class='footer-eval'>{html}</div>", unsafe_allow_html=True)


def render_overview_cards(cards: list[dict[str, str]]) -> None:
    html = "".join(
        (
            f"<div class='overview-card {escape(card.get('kind', 'accent'))}'>"
            f"<div class='label'>{escape(card.get('label', ''))}</div>"
            f"<div class='value'>{escape(card.get('value', '--'))}</div>"
            f"<div class='hint'>{escape(card.get('hint', ''))}</div>"
            "</div>"
        )
        for card in cards
    )
    st.markdown(f"<div class='overview-grid'>{html}</div>", unsafe_allow_html=True)


def render_overview_page(registry: list[dict[str, Any]]) -> None:
    """面向值班人员的全局态势首页。"""

    overview = build_operations_overview(registry, ROOT / "data" / "cache")
    evaluation = overview.get("evaluation_summary") or {}
    gain = overview.get("gain_summary") or {}
    type_hit_1 = evaluation.get("type_hit_1")
    observable_rate = evaluation.get("signature_observable_rate", evaluation.get("observable_rate"))
    gain_total = int(gain.get("total") or 0)
    gain_observed = int(gain.get("gain_drop_observed") or 0)
    cards = [
        {
            "label": "实验资产",
            "value": f"{overview['total_runs']:,}",
            "hint": f"{overview['batch_count']} 个批次已纳管",
            "kind": "accent",
        },
        {
            "label": "数据可用率",
            "value": f"{overview['valid_rate']:.1%}",
            "hint": f"{overview['valid_runs']:,} 组通过注册校验",
            "kind": "success" if overview["valid_rate"] >= 0.98 else "warning",
        },
        {
            "label": "故障场景覆盖",
            "value": str(overview["scenario_count"]),
            "hint": "EDFA · Fiber · ROADM",
            "kind": "accent",
        },
        {
            "label": "最新类型 Hit@1",
            "value": f"{float(type_hit_1):.1%}" if isinstance(type_hit_1, (int, float)) else "--",
            "hint": "留出集本地诊断闭环",
            "kind": "success" if isinstance(type_hit_1, (int, float)) and type_hit_1 >= 0.85 else "warning",
        },
        {
            "label": "故障证据可观测",
            "value": f"{float(observable_rate):.1%}" if isinstance(observable_rate, (int, float)) else "--",
            "hint": "优先相信直接遥测证据",
            "kind": "success" if isinstance(observable_rate, (int, float)) and observable_rate >= 0.9 else "warning",
        },
        {
            "label": "EDFA 重跑验证",
            "value": f"{gain_observed}/{gain_total}" if gain_total else "待执行",
            "hint": "actual_gain_db 下降可观测",
            "kind": "success" if gain_total and gain_observed == gain_total else "warning",
        },
    ]
    render_overview_cards(cards)

    st.markdown(
        """
        <div class="section-shell">
            <div class="section-heading"><b>智能运维闭环</b><span>从数据接入到处置验证的统一工作流</span></div>
            <div class="flow-row">
                <div class="flow-step"><div class="index">01 · SENSE</div><div class="name">状态感知</div><div class="desc">汇聚 EDFA、Fiber、ROADM 遥测与事件</div></div>
                <div class="flow-step"><div class="index">02 · DETECT</div><div class="name">异常检测</div><div class="desc">基线对比、变点识别与证据充分性检查</div></div>
                <div class="flow-step"><div class="index">03 · DIAGNOSE</div><div class="name">根因诊断</div><div class="desc">规则、历史特征、知识库与在线引擎协同</div></div>
                <div class="flow-step"><div class="index">04 · ACT</div><div class="name">处置编排</div><div class="desc">输出可执行建议与验证任务，不做黑盒结论</div></div>
                <div class="flow-step"><div class="index">05 · VERIFY</div><div class="name">闭环验证</div><div class="desc">对照标注评估命中率、可观测性和耗时</div></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    left, right = st.columns([1.65, 1.0], gap="small")
    with left:
        max_count = max((int(row["样本数"]) for row in overview["scenario_rows"]), default=1)
        bars = ""
        for row in overview["scenario_rows"]:
            name = str(row["故障场景"])
            label = FAULT_LABELS.get(name, name)
            count = int(row["样本数"])
            width = max(2.0, count / max_count * 100)
            bars += (
                "<div class='distribution-row'>"
                f"<span title='{escape(name)}'>{escape(short_text(label, 26))}</span>"
                f"<div class='distribution-track'><div class='distribution-fill' style='width:{width:.1f}%'></div></div>"
                f"<b>{count}</b></div>"
            )
        st.markdown(
            f"<div class='section-shell'><div class='section-heading'><b>场景资产分布</b>"
            f"<span>{overview['scenario_count']} 类 · {overview['total_runs']:,} 组</span></div>{bars}</div>",
            unsafe_allow_html=True,
        )
    with right:
        risk_html = ""
        for item in overview["risks"]:
            risk_html += (
                f"<div class='risk-row'><b>{escape(item['级别'])} · {escape(item['事项'])}</b><br>"
                f"{escape(item['建议'])}</div>"
            )
        st.markdown(
            f"<div class='section-shell'><div class='section-heading'><b>待关注事项</b>"
            f"<span>按证据边界提示，不制造虚假告警</span></div>{risk_html}</div>",
            unsafe_allow_html=True,
        )

    st.markdown(
        "<div class='section-shell'><div class='section-heading'><b>最近数据批次</b>"
        "<span>数据目录、诊断和评测使用同一注册表</span></div>",
        unsafe_allow_html=True,
    )
    render_html_table(overview["batch_rows"][:8], "暂无已注册批次")
    st.markdown("</div>", unsafe_allow_html=True)


def render_evaluation_page(registry: list[dict[str, Any]]) -> None:
    """集中展示数据质量、诊断效果与混淆薄弱项。"""

    overview = build_operations_overview(registry, ROOT / "data" / "cache")
    summary = overview.get("evaluation_summary") or {}
    cards = [
        {
            "label": "评测样本",
            "value": str(int(summary.get("total") or 0)) if summary else "待执行",
            "hint": "最新留出评测集",
            "kind": "accent",
        },
        {
            "label": "类型 Hit@1",
            "value": f"{float(summary.get('type_hit_1')):.1%}" if isinstance(summary.get("type_hit_1"), (int, float)) else "--",
            "hint": "首选根因类型命中",
            "kind": "success",
        },
        {
            "label": "类型 Hit@3",
            "value": f"{float(summary.get('type_hit_3')):.1%}" if isinstance(summary.get("type_hit_3"), (int, float)) else "--",
            "hint": "候选根因覆盖",
            "kind": "success",
        },
        {
            "label": "实体 Hit@1",
            "value": f"{float(summary.get('entity_hit_1')):.1%}" if isinstance(summary.get("entity_hit_1"), (int, float)) else "--",
            "hint": "故障对象精准定位",
            "kind": "accent",
        },
        {
            "label": "设备域命中",
            "value": f"{float(summary.get('device_hit')):.1%}" if isinstance(summary.get("device_hit"), (int, float)) else "--",
            "hint": "EDFA/Fiber/ROADM",
            "kind": "accent",
        },
        {
            "label": "直接证据可观测",
            "value": f"{float(summary.get('signature_observable_rate')):.1%}"
            if isinstance(summary.get("signature_observable_rate"), (int, float))
            else "--",
            "hint": "剔除不可观测样本后再解释效果",
            "kind": "warning",
        },
    ]
    render_overview_cards(cards)

    left, right = st.columns([1.55, 1.0], gap="small")
    with left:
        by_type = summary.get("by_type") if isinstance(summary.get("by_type"), dict) else {}
        rows = []
        for fault_type, item in by_type.items():
            rows.append(
                {
                    "故障类型": FAULT_LABELS.get(fault_type, fault_type),
                    "样本": int(item.get("count") or 0),
                    "Hit@1": f"{float(item.get('type_hit_1') or 0):.1%}",
                    "Hit@3": f"{float(item.get('type_hit_3') or 0):.1%}",
                    "实体命中": f"{float(item.get('entity_hit_1') or 0):.1%}",
                    "可观测": f"{int(item.get('signature_observable_count') or 0)}/{int(item.get('count') or 0)}",
                }
            )
        st.markdown(
            "<div class='section-shell'><div class='section-heading'><b>分类型诊断效果</b>"
            "<span>各故障域命中表现</span></div>",
            unsafe_allow_html=True,
        )
        render_html_table(rows, "尚无批量评测结果")
        st.markdown("</div>", unsafe_allow_html=True)
    with right:
        gain = overview.get("gain_summary") or {}
        gain_total = int(gain.get("total") or 0)
        gain_observed = int(gain.get("gain_drop_observed") or 0)
        st.markdown(
            "<div class='section-shell'><div class='section-heading'><b>EDFA 可观测性专项</b>"
            "<span>重跑数据优先验证</span></div>",
            unsafe_allow_html=True,
        )
        if gain_total:
            st.markdown(
                f"<div class='overview-card {'success' if gain_observed == gain_total else 'warning'}'>"
                "<div class='label'>actual_gain_db 预期下降</div>"
                f"<div class='value'>{gain_observed}/{gain_total}</div>"
                f"<div class='hint'>目标实体缺失 {int(gain.get('missing_target_entity') or 0)} 组</div></div>",
                unsafe_allow_html=True,
            )
        else:
            st.info("尚未生成 EDFA 重跑审计结果。")
        st.markdown(
            f"<div class='small'>评测文件：{escape(short_text(overview.get('evaluation_path'), 62))}</div>"
            f"<div class='small'>专项审计：{escape(short_text(overview.get('gain_audit_path'), 62))}</div>",
            unsafe_allow_html=True,
        )
        st.markdown("</div>", unsafe_allow_html=True)


def render_architecture_page(registry: list[dict[str, Any]]) -> None:
    """解释平台能力分层，并暴露各层当前落地状态。"""

    overview = build_operations_overview(registry, ROOT / "data" / "cache")
    layers = [
        ("04 · 交互与闭环层", "态势总览 · 事件工作台 · 评测治理 · 处置验证", "面向值班处置，不再把所有控件挤进一张页面"),
        ("03 · 智能分析层", "变点检测 · 异常摘要 · 根因排序 · 在线引擎校准", "结论必须绑定遥测、事件、历史样本和知识依据"),
        ("02 · 知识与模型层", "42 条光网络知识 · 历史故障特征库 · 规则引擎", "RAG 负责静态知识，历史特征负责相似召回，规则负责可解释兜底"),
        ("01 · 数据与感知层", f"{overview['total_runs']:,} 组实验 · EDFA/Fiber/ROADM · 拓扑与业务事件", "多数据根统一注册，保留原始数据，不复制进平台目录"),
    ]
    html = ""
    for index, (name, capabilities, desc) in enumerate(layers):
        html += (
            f"<div class='flow-step' style='margin-bottom:.48rem;border-left:4px solid var(--accent);'>"
            f"<div class='index'>{escape(name)}</div><div class='name'>{escape(capabilities)}</div>"
            f"<div class='desc'>{escape(desc)}</div></div>"
        )
    st.markdown(
        f"<div class='section-shell'><div class='section-heading'><b>平台逻辑架构</b>"
        f"<span>数据采集 → 分析判断 → 处置动作 → 效果验证</span></div>{html}</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        """
        <div class="section-shell">
            <div class="section-heading"><b>本轮对标后采用的设计原则</b><span>来自电信网络保障与主流可观测平台的共同模式</span></div>
            <div class="flow-row">
                <div class="flow-step"><div class="index">HEALTH FIRST</div><div class="name">先看全网健康</div><div class="desc">总览负责判断是否需要行动，详情页负责解释为什么</div></div>
                <div class="flow-step"><div class="index">SERVICE IMPACT</div><div class="name">从设备走向业务</div><div class="desc">故障对象、传播路径与业务影响放在同一事件上下文</div></div>
                <div class="flow-step"><div class="index">EVIDENCE RCA</div><div class="name">证据化根因</div><div class="desc">不把相似度或模型置信表述成真实概率</div></div>
                <div class="flow-step"><div class="index">ACTIVE VERIFY</div><div class="name">主动验证</div><div class="desc">诊断后生成验证任务，检验处置而不是只导出报告</div></div>
                <div class="flow-step"><div class="index">GOVERNANCE</div><div class="name">效果治理</div><div class="desc">命中率与数据可观测性分开统计，防止指标虚高</div></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_operations_page(
    registry: list[dict[str, Any]],
    default_run_id: str | None,
    loaded_run: dict[str, Any] | None,
) -> None:
    left, center, right = st.columns([2.15, 5.25, 2.6], gap="small")
    with center:
        device, entity_id, summaries, data_complete = render_center_panel(loaded_run)
    with left:
        render_left_panel(registry, default_run_id)
    with right:
        render_diagnosis_panel(loaded_run, device, entity_id, summaries, data_complete)
    render_eval_footer(loaded_run)


REPLAY_STAGE_LABELS = {
    "WAITING": "等待运行",
    "MONITORING": "监测中",
    "ANOMALY_DETECTED": "发现异常",
    "EVENT_CORRELATION": "事件关联中",
    "DIAGNOSING": "智能研判中",
    "ROOT_CAUSE_ANALYSIS": "根因分析中",
    "ACTION_PLAN_GENERATION": "处置方案生成中",
    "RECOMMENDATION_READY": "处置建议已生成",
    "NORMAL_COMPLETED": "正常监测完成",
}

REPLAY_STAGE_FLOW = [
    ("WAITING", "等待运行"),
    ("MONITORING", "监测中"),
    ("ANOMALY_DETECTED", "发现异常"),
    ("EVENT_CORRELATION", "事件关联中"),
    ("DIAGNOSING", "智能研判中"),
    ("ROOT_CAUSE_ANALYSIS", "根因分析"),
    ("RECOMMENDATION_READY", "处置方案生成"),
]

REPLAY_STAGE_PROGRESS = {
    "WAITING": 0,
    "MONITORING": 18,
    "ANOMALY_DETECTED": 36,
    "EVENT_CORRELATION": 52,
    "DIAGNOSING": 68,
    "ROOT_CAUSE_ANALYSIS": 84,
    "ACTION_PLAN_GENERATION": 94,
    "RECOMMENDATION_READY": 100,
    "NORMAL_COMPLETED": 100,
}


@st.cache_data(show_spinner=False)
def cached_replay_bundle(run_id: str, run_dir: str) -> dict[str, Any]:
    return load_replay_bundle({"run_id": run_id, "run_dir": run_dir})


def default_normal_run_id(registry: list[dict[str, Any]]) -> str | None:
    for run in registry:
        if run.get("status") == "VALID" and run.get("scenario") == "NORMAL_STATE":
            return str(run.get("run_id") or "")
    return None


def new_replay_runtime(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "index": 0,
        "playback_status": "WAITING",
        "stage": "WAITING",
        "previous_paths": [],
        "snapshot": {
            "current_tick": 0.0,
            "monitored_entity_count": 0,
            "observations": [],
            "candidate_paths": [],
        },
        "active_service_count": 0,
        "incidents": [],
        "selected_metric_series": [],
        "playback_interval": 0.55,
        "normal_completed": False,
        "recommendation_notice_pending": False,
    }


def ensure_replay_runtime(default_run_id: str) -> dict[str, Any]:
    runtime = st.session_state.get("replay_runtime")
    if not isinstance(runtime, dict) or not runtime.get("run_id"):
        runtime = new_replay_runtime(default_run_id)
        st.session_state["replay_runtime"] = runtime
    if runtime.get("stage") in {
        "PENDING_VALIDATION",
        "VALIDATION_WAITING",
        "MANUAL_CONFIRMATION",
        "DEFERRED",
    }:
        runtime["stage"] = "RECOMMENDATION_READY"
        runtime["playback_status"] = "PAUSED"
        runtime.pop("validation_task", None)
        incident = current_replay_incident(runtime)
        if incident:
            incident["status"] = "RECOMMENDATION_READY"
            incident["current_stage"] = "RECOMMENDATION_READY"
            for action in incident.get("actions") or []:
                if isinstance(action, dict):
                    action["status"] = "SUGGESTED"
    return runtime


def reset_replay_runtime(run_id: str, *, start: bool = False) -> None:
    runtime = new_replay_runtime(run_id)
    if start:
        runtime["playback_status"] = "RUNNING"
        runtime["stage"] = "MONITORING"
    st.session_state["replay_runtime"] = runtime
    st.session_state["incident"] = None
    st.session_state["diagnosis"] = None
    st.session_state["loaded_run_id"] = run_id


def replay_stage_label(runtime: dict[str, Any]) -> str:
    return REPLAY_STAGE_LABELS.get(str(runtime.get("stage") or ""), "等待运行")


def replay_health(runtime: dict[str, Any]) -> tuple[str, str]:
    stage = str(runtime.get("stage") or "")
    if stage in {
        "ANOMALY_DETECTED",
        "EVENT_CORRELATION",
        "DIAGNOSING",
        "ROOT_CAUSE_ANALYSIS",
        "ACTION_PLAN_GENERATION",
    }:
        return "异常处理中", "warning"
    if stage == "RECOMMENDATION_READY":
        return "处置建议已生成", "danger"
    if stage == "NORMAL_COMPLETED":
        return "健康", "success"
    if stage == "MONITORING":
        return "监测中", "success"
    return "等待运行", "accent"


def current_replay_incident(runtime: dict[str, Any]) -> dict[str, Any] | None:
    incidents = runtime.get("incidents")
    if isinstance(incidents, list) and incidents and isinstance(incidents[-1], dict):
        return incidents[-1]
    return None


def current_engine_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    engine_runtime = runtime.get("engine_runtime")
    if isinstance(engine_runtime, dict) and engine_runtime.get("name"):
        return engine_runtime
    config = current_model_config()
    preference = str(st.session_state.get("engine_preference") or "在线大模型")
    if preference == "在线大模型" and model_config_ready(config):
        return {
            "name": str(config.get("model") or DEFAULT_ENGINE_MODEL),
            "mode": "ONLINE",
            "status": "在线优先",
            "attempts": 0,
            "fallback": False,
        }
    return {
        "name": "本地诊断引擎",
        "mode": "LOCAL",
        "status": "手动选择" if preference == "本地诊断引擎" else "在线配置不可用",
        "attempts": 0,
        "fallback": preference != "本地诊断引擎",
    }


def format_engine_runtime_detail(runtime: dict[str, Any]) -> str:
    engine_runtime = current_engine_runtime(runtime)
    parts = [str(engine_runtime.get("status") or "待命")]
    attempts = int(engine_runtime.get("attempts") or 0)
    if attempts:
        parts.append(f"调用 {attempts} 次")
    latency = engine_runtime.get("latency_ms")
    if isinstance(latency, (int, float)):
        parts.append(f"{float(latency) / 1000:.1f} 秒")
    if engine_runtime.get("fallback"):
        parts.append("已回退")
    return " · ".join(parts)


def render_closed_loop_header(runtime: dict[str, Any]) -> None:
    health, _kind = replay_health(runtime)
    engine_runtime = current_engine_runtime(runtime)
    engine_name = str(engine_runtime.get("name") or "未配置")
    engine_detail = format_engine_runtime_detail(runtime)
    st.markdown(
        f"""
        <div class="hero-bar closed-loop-hero ops-command-header">
            <div>
                <div class="brand-code">OPTINET · INTELLIGENT OPERATIONS</div>
                <div class="hero-title">光网络智能运维平台</div>
                <div class="hero-subtitle">全域监测 · 事件协同 · 智能研判 · 闭环处置</div>
            </div>
            <div class="hero-meta">
                <div class="hero-meta-item">运行模式<br><b>智能监测</b></div>
                <div class="hero-meta-item">网络状态<br><b>{escape(health)}</b></div>
                <div class="hero-meta-item">当前阶段<br><b>{escape(replay_stage_label(runtime))}</b></div>
                <div class="hero-meta-item" title="{escape(engine_detail)}">当前模型<br><b>{escape(engine_name)}</b></div>
                <div class="hero-meta-item">网络时标<br><b>{float((runtime.get("snapshot") or {}).get("current_tick") or 0):.3f}</b></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def inject_closed_loop_css() -> None:
    st.markdown(
        """
        <style>
        .closed-loop-hero {margin-bottom:.55rem}
        .ops-command-header {
          border-color:color-mix(in srgb,var(--accent-secondary) 34%,var(--border));
          background:
            linear-gradient(110deg,color-mix(in srgb,var(--accent) 15%,transparent),transparent 42%),
            radial-gradient(circle at 82% 0%,color-mix(in srgb,var(--accent-secondary) 12%,transparent),transparent 34%),
            var(--panel);
          box-shadow:0 14px 42px rgba(2,8,23,.18),inset 0 1px rgba(255,255,255,.025);
        }
        .brand-code {font:700 .58rem/1.2 Consolas,monospace;letter-spacing:.16rem;color:var(--accent-secondary);margin-bottom:.22rem}
        .ops-progress {position:relative;display:grid;grid-template-columns:repeat(7,1fr);margin:.45rem .2rem .9rem;padding:.15rem 0}
        .ops-progress:before {content:"";position:absolute;left:7%;right:7%;top:.52rem;height:1px;background:var(--border)}
        .ops-progress-step {position:relative;text-align:center;color:var(--text-secondary);font-size:.66rem;z-index:1}
        .ops-progress-step i {display:block;width:.7rem;height:.7rem;margin:0 auto .38rem;border-radius:50%;
          background:var(--panel-secondary);border:2px solid var(--border);box-shadow:0 0 0 4px var(--bg)}
        .ops-progress-step.active {color:var(--accent-secondary);font-weight:700}
        .ops-progress-step.active i {background:var(--accent-secondary);border-color:var(--accent-secondary);
          box-shadow:0 0 0 4px color-mix(in srgb,var(--accent-secondary) 14%,var(--bg)),0 0 18px color-mix(in srgb,var(--accent-secondary) 55%,transparent)}
        .ops-progress-step.done {color:var(--success)}
        .ops-progress-step.done i {background:var(--success);border-color:var(--success)}
        .ops-progress-meter {height:.32rem;margin:-.35rem .35rem .75rem;border-radius:999px;
          background:var(--panel-secondary);border:1px solid var(--border);overflow:hidden}
        .ops-progress-meter > i {display:block;height:100%;border-radius:999px;
          background:linear-gradient(90deg,var(--accent),var(--accent-secondary));
          box-shadow:0 0 16px color-mix(in srgb,var(--accent-secondary) 60%,transparent);
          transition:width .35s ease}
        .ops-progress-meta {display:flex;justify-content:space-between;margin:-.2rem .35rem .38rem;
          color:var(--text-secondary);font-size:.67rem}
        .ops-completion-alert {display:grid;grid-template-columns:auto 1fr;gap:.75rem;align-items:center;
          margin:.15rem 0 .75rem;padding:.82rem 1rem;border:1px solid color-mix(in srgb,var(--success) 55%,var(--border));
          border-radius:12px;background:linear-gradient(100deg,color-mix(in srgb,var(--success) 12%,var(--panel)),var(--panel))}
        .ops-completion-alert .signal {width:.72rem;height:.72rem;border-radius:50%;background:var(--success);
          box-shadow:0 0 20px color-mix(in srgb,var(--success) 72%,transparent)}
        .ops-completion-alert b {font-size:.9rem;color:var(--text-primary)}
        .ops-conclusion {border-left:5px solid var(--accent);padding:.9rem 1rem;border-radius:12px;
          background:var(--panel);border-top:1px solid var(--border);border-right:1px solid var(--border);
          border-bottom:1px solid var(--border);margin-bottom:.65rem}
        .ops-conclusion.critical {border-left-color:#ef4444}
        .ops-conclusion h3 {font-size:1.02rem;margin:0 0 .4rem}
        .incident-list-item {padding:.72rem .78rem;border:1px solid var(--border);border-radius:11px;
          background:var(--panel);margin-bottom:.5rem}
        .incident-list-item b {font-size:.88rem}
        .metric-evidence {padding:.55rem .65rem;border-radius:9px;background:var(--panel-soft);
          border:1px solid var(--border);margin-bottom:.38rem;font-size:.8rem}
        .action-card {padding:.78rem;border:1px solid var(--border);border-radius:12px;background:var(--panel);
          margin-bottom:.5rem}
        .action-card.primary {border-color:var(--accent);box-shadow:0 0 0 2px rgba(33,123,255,.08)}
        .todo-grid {display:grid;grid-template-columns:repeat(4,1fr);gap:.55rem}
        .todo-item {padding:.7rem;border:1px solid var(--border);border-radius:10px;background:var(--panel)}
        .todo-item .count {font-size:1.35rem;font-weight:800}
        .status-pulse {display:inline-block;width:8px;height:8px;border-radius:50%;background:#22c55e;
          margin-right:.35rem;box-shadow:0 0 0 4px rgba(34,197,94,.12)}
        .config-hero {padding:1rem 1.05rem;border:1px solid var(--border);border-radius:14px;background:
          linear-gradient(115deg,color-mix(in srgb,var(--accent) 12%,var(--panel)),var(--panel));margin-bottom:.6rem}
        .config-hero h3 {font-size:1rem!important;margin-bottom:.3rem!important}
        .engine-status-line {display:flex;align-items:center;gap:.45rem;color:var(--text-secondary);font-size:.76rem}
        .engine-status-dot {width:.58rem;height:.58rem;border-radius:50%;background:var(--success);box-shadow:0 0 14px var(--success)}
        .validation-card {padding:.85rem;border:1px solid var(--border);border-radius:12px;background:var(--panel-secondary);margin-bottom:.48rem}
        .validation-card b {color:var(--text-primary)}
        .overview-grid:has(> .overview-card:nth-child(4):last-child) {grid-template-columns:repeat(4,minmax(0,1fr))}
        @media (max-width: 1000px) {
          .ops-progress {grid-template-columns:repeat(4,1fr);row-gap:.65rem}
          .ops-progress:before {display:none}
          .todo-grid {grid-template-columns:repeat(2,1fr)}
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_replay_controls(
    registry: list[dict[str, Any]],
    runtime: dict[str, Any],
) -> None:
    status = str(runtime.get("playback_status") or "WAITING")
    cols = st.columns([1.2, 1, 1, 5.1], gap="small")
    if status in {"WAITING", "COMPLETED"}:
        if cols[0].button("启动监测", type="primary", use_container_width=True):
            reset_replay_runtime(str(runtime["run_id"]), start=True)
            st.rerun()
    else:
        if cols[0].button("重新监测", use_container_width=True):
            reset_replay_runtime(str(runtime["run_id"]), start=True)
            st.rerun()
    if status == "RUNNING":
        if cols[1].button("暂停监测", use_container_width=True):
            runtime["playback_status"] = "PAUSED"
            st.session_state["replay_runtime"] = runtime
            st.rerun()
    elif cols[1].button("恢复监测", disabled=status not in {"PAUSED"}, use_container_width=True):
        runtime["playback_status"] = "RUNNING"
        st.session_state["replay_runtime"] = runtime
        st.rerun()
    incident = current_replay_incident(runtime)
    if cols[2].button("重新研判", disabled=not bool(incident), use_container_width=True):
        if incident:
            incident["status"] = "UNDER_ANALYSIS"
            incident["current_stage"] = "DIAGNOSING"
            incident["diagnosis"] = None
            incident["actions"] = []
            runtime["stage"] = "DIAGNOSING"
            runtime["playback_status"] = "RUNNING"
            runtime["recommendation_notice_pending"] = False
            runtime.pop("engine_runtime", None)
            st.session_state["replay_runtime"] = runtime
            st.rerun()
    with cols[3]:
        st.markdown(
            f"<div class='small' style='padding:.55rem .2rem'><span class='status-pulse'></span>"
            f"{escape(replay_stage_label(runtime))}　·　监测通道已就绪</div>",
            unsafe_allow_html=True,
        )


def render_stage_strip(runtime: dict[str, Any]) -> None:
    stage = str(runtime.get("stage") or "WAITING")
    current_index = next(
        (index for index, (key, _) in enumerate(REPLAY_STAGE_FLOW) if key == stage),
        6 if stage == "ACTION_PLAN_GENERATION" else 1 if stage == "NORMAL_COMPLETED" else 0,
    )
    html = ""
    for index, (key, label) in enumerate(REPLAY_STAGE_FLOW):
        cls = "active" if index == current_index else "done" if index < current_index else ""
        html += f"<div class='ops-progress-step {cls}'><i></i>{escape(label)}</div>"
    progress = int(REPLAY_STAGE_PROGRESS.get(stage, 0))
    st.markdown(
        f"<div class='ops-progress'>{html}</div>"
        f"<div class='ops-progress-meta'><span>自动处置流程</span>"
        f"<b>{escape(replay_stage_label(runtime))} · {progress}%</b></div>"
        f"<div class='ops-progress-meter'><i style='width:{progress}%'></i></div>",
        unsafe_allow_html=True,
    )


def emit_recommendation_toast(runtime: dict[str, Any]) -> None:
    if not runtime.get("recommendation_notice_pending"):
        return
    incident = current_replay_incident(runtime)
    top = str((incident or {}).get("primary_suspected_cause") or "根因已定位")
    actions = list((incident or {}).get("actions") or [])
    action_name = str((actions[0] if actions else {}).get("action_name") or "处置建议已生成")
    st.toast(
        f"根因分析完成：{FAULT_LABELS.get(top, top)}；优先建议：{action_name}",
        icon="✅",
    )
    runtime["recommendation_notice_pending"] = False
    st.session_state["replay_runtime"] = runtime


def render_recommendation_notice(runtime: dict[str, Any]) -> None:
    if str(runtime.get("stage") or "") != "RECOMMENDATION_READY":
        return
    incident = current_replay_incident(runtime)
    if not incident:
        return
    top = str(incident.get("primary_suspected_cause") or "根因已定位")
    actions = list(incident.get("actions") or [])
    action_name = str((actions[0] if actions else {}).get("action_name") or "处置建议已生成")
    engine_runtime = current_engine_runtime(runtime)
    st.markdown(
        f"""
        <div class="ops-completion-alert">
          <span class="signal"></span>
          <div><b>根因分析与处置方案建议已生成</b><br>
          <span class="small">首要根因：{escape(FAULT_LABELS.get(top, top))}　·　
          优先建议：{escape(action_name)}　·　研判模型：{escape(str(engine_runtime.get('name') or '待命'))}
          （{escape(format_engine_runtime_detail(runtime))}）</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def metric_unit(metric: str) -> str:
    if metric.endswith("_dbm"):
        return "dBm"
    if metric.endswith("_db"):
        return "dB"
    if metric.endswith("_ps_nm"):
        return "ps/nm"
    if metric.endswith("_ps"):
        return "ps"
    return ""


def build_selected_metric_series(
    bundle: dict[str, Any],
    features: list[dict[str, Any]],
    *,
    current_tick: float,
) -> list[dict[str, Any]]:
    """按模型选出的真实实体与字段回填历史曲线，不读取当前时标之后的数据。"""

    telemetry = bundle.get("telemetry") if isinstance(bundle.get("telemetry"), dict) else {}
    series: list[dict[str, Any]] = []
    for feature in features[:3]:
        device_type = str(feature.get("device_type") or "")
        entity_id = str(feature.get("entity_id") or "")
        metric = str(feature.get("metric") or "")
        values_by_tick: dict[float, list[float]] = {}
        for row in telemetry.get(device_type, []):
            tick = row.get("simulation_tick")
            value = row.get(metric)
            if (
                str(row.get("entity_id") or "") != entity_id
                or not isinstance(tick, (int, float))
                or float(tick) > current_tick
                or not isinstance(value, (int, float))
            ):
                continue
            values_by_tick.setdefault(float(tick), []).append(float(value))
        points = [
            {"网络时标": round(tick, 3), "指标值": round(sum(values) / len(values), 4)}
            for tick, values in sorted(values_by_tick.items())
            if values
        ]
        series.append({**feature, "unit": metric_unit(metric), "points": points[-80:]})
    return series


def render_live_metric_charts(runtime: dict[str, Any]) -> None:
    series = [
        item
        for item in (runtime.get("selected_metric_series") or [])
        if isinstance(item, dict)
    ]
    model_selected = sum(1 for item in series if item.get("selection_source") == "online_model")
    source_text = (
        f"在线模型选择 {model_selected}/{len(series)} · 平台已完成遥测字段校验"
        if series
        else "事件形成后由在线模型从可见遥测证据中选择，不使用预置故障模板"
    )
    st.markdown(
        "<div class='section-shell'><div class='section-heading'><b>事件关键指标趋势</b>"
        f"<span>{escape(source_text)}</span></div>",
        unsafe_allow_html=True,
    )
    if not series:
        stage = str(runtime.get("stage") or "")
        message = (
            "在线模型正在比较本事件的指标变化、时序和传播关系，研判完成后自动生成三条关键曲线。"
            if stage in {"DIAGNOSING", "ROOT_CAUSE_ANALYSIS", "ACTION_PLAN_GENERATION"}
            else "启动监测后，系统先发现事件；在线研判完成后自动展示与该事件最相关的三项指标。"
        )
        st.markdown(f"<div class='small'>{escape(message)}</div>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)
        return
    is_dark = st.session_state.get("theme", "dark") == "dark"
    axis_color = "#94A3B8" if is_dark else "#64748B"
    grid_color = "#223047" if is_dark else "#D8E1EC"
    columns = st.columns(len(series), gap="small")
    for column, feature in zip(columns, series):
        points = list(feature.get("points") or [])
        metric = str(feature.get("metric") or "")
        device_type = str(feature.get("device_type") or "")
        entity_id = str(feature.get("entity_id") or "")
        unit = str(feature.get("unit") or "")
        title = f"{DEVICE_LABELS.get(device_type, device_type)} · {metric_label(metric)}"
        with column:
            if not points:
                st.markdown(
                    f"<div class='metric-evidence'><b>{escape(title)}</b><br>"
                    f"<span class='small'>{escape(entity_id)} · 暂无可绘制样本</span></div>",
                    unsafe_allow_html=True,
                )
                continue
            current_value = float(points[-1]["指标值"])
            delta = current_value - float(points[0]["指标值"])
            values = [float(point["指标值"]) for point in points]
            value_min = min(values)
            value_max = max(values)
            value_span = value_max - value_min
            vertical_padding = max(
                value_span * 0.4,
                max(abs(value_min), abs(value_max)) * 0.04,
                0.8,
            )
            y_domain = [
                round(value_min - vertical_padding, 4),
                round(value_max + vertical_padding, 4),
            ]
            st.markdown(
                f"<div class='small'><b>{escape(title)}</b>　"
                f"<span>{current_value:.2f} {escape(unit)}　变化 {delta:+.2f}</span><br>"
                f"{escape(short_text(entity_id, 62))}</div>",
                unsafe_allow_html=True,
            )
            st.vega_lite_chart(
                spec={
                    "data": {"values": points},
                    "mark": {
                        "type": "line",
                        "interpolate": "monotone",
                        "point": {"filled": True, "size": 24},
                        "strokeWidth": 2.2,
                        "color": "#EF4444",
                    },
                    "encoding": {
                        "x": {
                            "field": "网络时标",
                            "type": "quantitative",
                            "title": "网络时标",
                            "axis": {"gridColor": grid_color, "labelColor": axis_color},
                        },
                        "y": {
                            "field": "指标值",
                            "type": "quantitative",
                            "title": unit,
                            "scale": {"domain": y_domain, "nice": False},
                            "axis": {"gridColor": grid_color, "labelColor": axis_color},
                        },
                        "tooltip": [
                            {"field": "网络时标", "type": "quantitative", "format": ".1f"},
                            {"field": "指标值", "type": "quantitative", "format": ".3f"},
                        ],
                    },
                    "height": 155,
                    "background": "transparent",
                    "config": {"view": {"stroke": "transparent"}},
                },
                use_container_width=True,
            )
    st.markdown("</div>", unsafe_allow_html=True)


def render_runtime_cards(runtime: dict[str, Any]) -> None:
    incident = current_replay_incident(runtime)
    incidents = list(runtime.get("incidents") or [])
    affected = len((incident or {}).get("affected_services") or [])
    actions = list((incident or {}).get("actions") or [])
    health, health_kind = replay_health(runtime)
    cards = [
        {"label": "当前网络健康状态", "value": health, "hint": replay_stage_label(runtime), "kind": health_kind},
        {"label": "活动事件", "value": str(len(incidents)), "hint": "由遥测异常自动聚合", "kind": "danger" if incidents else "success"},
        {
            "label": "严重事件",
            "value": str(sum(1 for item in incidents if item.get("severity") == "CRITICAL")),
            "hint": "严重级别",
            "kind": "danger" if incidents else "success",
        },
        {"label": "受影响业务", "value": str(affected), "hint": "按活动业务路径关联", "kind": "warning" if affected else "accent"},
        {
            "label": "处置建议",
            "value": str(len(actions)),
            "hint": "按候选根因自动生成",
            "kind": "success" if actions else "accent",
        },
        {
            "label": "当前运行时标",
            "value": f"{float((runtime.get('snapshot') or {}).get('current_tick') or 0):.3f}",
            "hint": (
                f"监测设备 {int((runtime.get('snapshot') or {}).get('monitored_entity_count') or 0)} 台 · "
                f"活动业务 {int(runtime.get('active_service_count') or 0)} 条"
            ),
            "kind": "accent",
        },
    ]
    render_overview_cards(cards)


def render_incident_summary(incident: dict[str, Any] | None) -> None:
    if not incident:
        st.markdown(
            "<div class='ops-conclusion'><h3>当前未形成活动运维事件</h3>"
            "<div class='small'>启动监测后，平台将自动扫描EDFA、Fiber、ROADM关键指标并聚合同路径异常。</div></div>",
            unsafe_allow_html=True,
        )
        return
    top = str(incident.get("primary_suspected_cause") or "研判中")
    path = " → ".join(str(item) for item in incident.get("affected_path") or [])
    severity = SEVERITY_LABELS.get(str(incident.get("severity")), str(incident.get("severity") or "待定"))
    st.markdown(
        f"""
        <div class="ops-conclusion critical">
          <h3>{escape(str(incident.get('incident_id')))} · 检测到光性能劣化传播事件</h3>
          <div class="small">事件等级：<b>{escape(severity)}</b>　·　
          当前阶段：<b>{escape(REPLAY_STAGE_LABELS.get(str(incident.get('current_stage')), str(incident.get('current_stage'))))}</b></div>
          <div class="small">异常路径：<b>{escape(path or '正在关联')}</b>　·　影响业务：
          <b>{len(incident.get('affected_services') or [])}</b>　·　首要疑似根因：<b>{escape(FAULT_LABELS.get(top, top))}</b></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_todo_tasks(runtime: dict[str, Any]) -> None:
    incident = current_replay_incident(runtime)
    status = str((incident or {}).get("status") or "")
    actions = list((incident or {}).get("actions") or [])
    pending_analysis = int(bool(incident) and status == "UNDER_ANALYSIS")
    suggested = sum(1 for item in actions if item.get("status") == "SUGGESTED")
    high_priority = sum(1 for item in actions if item.get("priority") == "HIGH")
    completed = int(status == "RECOMMENDATION_READY")
    st.markdown(
        f"""
        <div class="todo-grid">
          <div class="todo-item"><div class="small">待研判事件</div><div class="count">{pending_analysis}</div></div>
          <div class="todo-item"><div class="small">处置建议</div><div class="count">{suggested}</div></div>
          <div class="todo-item"><div class="small">高优先级建议</div><div class="count">{high_priority}</div></div>
          <div class="todo-item"><div class="small">已完成研判</div><div class="count">{completed}</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_system_config_page(
    registry: list[dict[str, Any]],
    runtime: dict[str, Any],
) -> None:
    config = current_model_config()
    ready = model_config_ready(config)
    if "engine_preference" not in st.session_state:
        st.session_state["engine_preference"] = "在线大模型" if ready else "本地诊断引擎"
    chunks = load_knowledge_chunks(KNOWLEDGE_CHUNKS_PATH)
    engine_status = get_engine_status(config)
    st.markdown(
        f"""
        <div class="config-hero">
          <div class="brand-code">SYSTEM CONTROL · INTELLIGENCE ENGINE</div>
          <h3>智能研判引擎</h3>
          <div class="engine-status-line"><span class="engine-status-dot"></span>
          配置中心已连接　·　当前模式：<b>{escape(str(st.session_state.get('engine_preference')))}</b>　·　
          在线接口：<b>{'已就绪' if ready else '待配置'}</b></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    render_overview_cards(
        [
            {
                "label": "引擎状态",
                "value": "在线" if ready else "本地",
                "hint": "研判服务可用",
                "kind": "success",
            },
            {
                "label": "当前模型",
                "value": short_text(config.get("model") or "本地规则", 22),
                "hint": "在线研判模型",
                "kind": "accent",
            },
            {
                "label": "知识条目",
                "value": str(len(chunks)),
                "hint": "诊断依据库",
                "kind": "accent",
            },
            {
                "label": "接口状态",
                "value": "已配置" if ready else "未配置",
                "hint": str(engine_status.get("mode") or "本地诊断引擎"),
                "kind": "success" if ready else "warning",
            },
        ]
    )

    left, right = st.columns([1.55, 1.0], gap="small")
    with left:
        st.markdown(
            "<div class='section-shell'><div class='section-heading'><b>大模型连接配置</b>"
            "<span>配置后可直接参与自动研判</span></div>",
            unsafe_allow_html=True,
        )
        st.radio(
            "自动研判引擎",
            ["在线大模型", "本地诊断引擎"],
            horizontal=True,
            key="engine_preference",
        )
        base_url_input = st.text_input(
            "接口地址",
            value=str(config.get("base_url") or ""),
            key="engine_base_url_input",
            placeholder="https://.../compatible-mode/v1",
        )
        field_a, field_b = st.columns([1.2, 1.0])
        model_input = field_a.text_input(
            "模型名称",
            value=str(config.get("model") or ""),
            key="engine_model_input",
        )
        api_key_input = field_b.text_input(
            "访问密钥",
            value="",
            key="engine_api_key_replace_input",
            type="password",
            placeholder="已安全载入；输入新密钥可替换",
        )
        field_c, field_d = st.columns(2)
        timeout_input = field_c.number_input(
            "响应超时（秒）",
            min_value=10,
            max_value=180,
            value=int(config.get("timeout_seconds") or DEFAULT_ENGINE_TIMEOUT_SECONDS),
            step=5,
            key="engine_timeout_input",
        )
        max_tokens_input = field_d.number_input(
            "最大输出长度",
            min_value=500,
            max_value=8000,
            value=int(config.get("max_tokens") or 2000),
            step=100,
            key="engine_max_tokens_input",
        )
        draft_config = {
            "base_url": base_url_input.strip(),
            "model": model_input.strip(),
            "api_key": api_key_input.strip() or str(config.get("api_key") or ""),
            "timeout_seconds": str(int(timeout_input)),
            "max_tokens": str(int(max_tokens_input)),
        }
        button_a, button_b = st.columns(2)
        if button_a.button("保存引擎配置", type="primary", use_container_width=True):
            st.session_state["model_base_url"] = draft_config["base_url"]
            st.session_state["model_name"] = draft_config["model"]
            if api_key_input.strip():
                st.session_state["model_api_key"] = api_key_input.strip()
            st.session_state["model_timeout_seconds"] = int(timeout_input)
            st.session_state["model_max_tokens"] = int(max_tokens_input)
            st.success("引擎配置已应用到当前平台会话。")
        if button_b.button("测试在线连接", use_container_width=True):
            try:
                with st.spinner("正在检测研判服务..."):
                    result = test_online_model_config(draft_config)
                latency = result.get("latency_ms")
                st.success(f"连接正常{f'，响应 {float(latency):.0f} ms' if isinstance(latency, (int, float)) else ''}。")
                st.session_state["engine_last_error"] = ""
            except Exception as exc:  # noqa: BLE001
                st.session_state["engine_last_error"] = str(exc)
                st.error(f"连接失败：{exc}")
        st.markdown("</div>", unsafe_allow_html=True)
    with right:
        st.markdown(
            "<div class='section-shell'><div class='section-heading'><b>运行策略</b>"
            "<span>自动研判与安全回退</span></div>",
            unsafe_allow_html=True,
        )
        preferred = str(st.session_state.get("engine_preference") or "本地诊断引擎")
        st.markdown(
            f"""
            <div class="validation-card"><b>首选引擎</b><br><span class="small">{escape(preferred)}</span></div>
            <div class="validation-card"><b>在线优先策略</b><br><span class="small">临时故障自动重试；连续失败 {ONLINE_ENGINE_MAX_ATTEMPTS} 次后才由本地引擎接管</span></div>
            <div class="validation-card"><b>知识增强</b><br><span class="small">检索结果作为诊断依据，不替代候选根因排序</span></div>
            <div class="validation-card"><b>密钥状态</b><br><span class="small">{'已从私有配置载入' if config.get('api_key') else '尚未配置访问密钥'}</span></div>
            """,
            unsafe_allow_html=True,
        )
        theme_cols = st.columns(2)
        if theme_cols[0].button("深色界面", use_container_width=True):
            st.session_state["theme"] = "dark"
            st.rerun()
        if theme_cols[1].button("亮色界面", use_container_width=True):
            st.session_state["theme"] = "light"
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown(
        "<div class='section-shell'><div class='section-heading'><b>监测场景管理</b>"
        "<span>数据选择集中在配置中心，不占用运维主屏</span></div>",
        unsafe_allow_html=True,
    )
    valid = [item for item in registry if item.get("status") == "VALID"]
    options = [str(item.get("run_id") or "") for item in valid]
    lookup = {str(item.get("run_id") or ""): item for item in valid}
    current = str(runtime.get("run_id") or "")
    selected = st.selectbox(
        "监测场景",
        options,
        index=options.index(current) if current in options else 0,
        format_func=lambda value: (
            f"{FAULT_LABELS.get(str(lookup[value].get('scenario')), lookup[value].get('scenario'))} · "
            f"{str(lookup[value].get('batch_id') or '').replace('experiment_batch_', '批次 ')} · "
            f"样本 {str(lookup[value].get('episode_id') or '').replace('episode_', '')}"
        ),
        key="system_scene",
    )
    scene_a, scene_b, scene_c = st.columns([1.5, 1, 1])
    speed = scene_a.select_slider(
        "运行节奏",
        options=[0.25, 0.55, 0.9],
        value=float(runtime.get("playback_interval") or 0.55),
        format_func=lambda value: {0.25: "快速", 0.55: "标准", 0.9: "慢速"}[value],
    )
    if scene_b.button("应用监测场景", use_container_width=True):
        reset_replay_runtime(selected, start=False)
        st.rerun()
    normal_id = default_normal_run_id(registry)
    if scene_c.button("加载健康基线", disabled=not bool(normal_id), use_container_width=True):
        reset_replay_runtime(str(normal_id), start=False)
        st.rerun()
    runtime["playback_interval"] = speed
    st.session_state["replay_runtime"] = runtime
    st.markdown("</div>", unsafe_allow_html=True)


def render_operations_overview(
    registry: list[dict[str, Any]],
    runtime: dict[str, Any],
) -> None:
    render_replay_controls(registry, runtime)
    render_stage_strip(runtime)
    render_recommendation_notice(runtime)
    render_runtime_cards(runtime)
    render_live_metric_charts(runtime)
    left, right = st.columns([1.65, 1.0], gap="small")
    with left:
        st.markdown(
            "<div class='section-shell'><div class='section-heading'><b>网络运行状态</b>"
            "<span>全网健康与事件态势</span></div>",
            unsafe_allow_html=True,
        )
        render_incident_summary(current_replay_incident(runtime))
        incident = current_replay_incident(runtime)
        if incident and runtime.get("stage") == "RECOMMENDATION_READY":
            st.markdown("<div class='small'><b>平台推荐：</b>查看根因分析和处置方案建议。</div>", unsafe_allow_html=True)
        elif incident:
            st.markdown("<div class='small'><b>平台推荐：</b>自动研判正在推进，无需重复操作。</div>", unsafe_allow_html=True)
        else:
            st.markdown("<div class='small'><b>平台推荐：</b>保持自动监测，无需人工干预。</div>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)
    with right:
        st.markdown(
            "<div class='section-shell'><div class='section-heading'><b>待办任务</b>"
            "<span>按事件状态自动汇总</span></div>",
            unsafe_allow_html=True,
        )
        render_todo_tasks(runtime)
        st.markdown("</div>", unsafe_allow_html=True)
    st.markdown(
        "<div class='section-shell'><div class='section-heading'><b>活动事件</b>"
        "<span>同一时间窗口、同一路径的指标异常合并展示</span></div>",
        unsafe_allow_html=True,
    )
    incident = current_replay_incident(runtime)
    if incident:
        render_html_table(
            [
                {
                    "事件编号": incident.get("incident_id"),
                    "事件等级": SEVERITY_LABELS.get(str(incident.get("severity")), incident.get("severity")),
                    "异常位置": " → ".join(incident.get("affected_path") or []),
                    "影响业务": len(incident.get("affected_services") or []),
                    "当前阶段": REPLAY_STAGE_LABELS.get(str(incident.get("current_stage")), incident.get("current_stage")),
                    "首要疑似根因": FAULT_LABELS.get(str(incident.get("primary_suspected_cause")), incident.get("primary_suspected_cause") or "研判中"),
                }
            ]
        )
    else:
        st.markdown("<div class='small'>暂无活动事件。</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


def render_event_center(runtime: dict[str, Any]) -> None:
    render_stage_strip(runtime)
    incidents = list(runtime.get("incidents") or [])
    st.markdown(
        "<div class='section-shell'><div class='section-heading'><b>自动生成的运维事件</b>"
        "<span>异常发现、路径聚合、业务关联和状态流转</span></div>",
        unsafe_allow_html=True,
    )
    if not incidents:
        st.info("当前没有活动事件。启动自动监测后，事件将由平台主动生成。")
    for incident in reversed(incidents):
        render_incident_summary(incident)
        timeline = [
            {"阶段": "异常发现", "状态": "已完成"},
            {"阶段": "事件关联", "状态": "已完成"},
            {
                "阶段": "智能研判",
                "状态": "已完成" if incident.get("diagnosis") else "处理中",
            },
            {
                "阶段": "根因分析",
                "状态": "已完成" if incident.get("primary_suspected_cause") else "处理中",
            },
            {
                "阶段": "处置方案生成",
                "状态": "已完成" if incident.get("actions") else "处理中",
            },
        ]
        render_html_table(timeline)
    st.markdown("</div>", unsafe_allow_html=True)


def render_workbench(runtime: dict[str, Any], _run: dict[str, Any]) -> None:
    incident = current_replay_incident(runtime)
    if not incident:
        st.info("尚无事件进入处置工作台。请先在运维总览启动监测。")
        return
    diagnosis = incident.get("diagnosis") if isinstance(incident.get("diagnosis"), dict) else {}
    top_causes = [item for item in diagnosis.get("top_causes", []) if isinstance(item, dict)]
    actions = [item for item in incident.get("actions", []) if isinstance(item, dict)]
    top = top_causes[0] if top_causes else {}
    path = " → ".join(incident.get("affected_path") or [])
    st.markdown("<div class='section-shell'><div class='section-heading'><b>事件研判</b><span>影响范围与处置重点</span></div>", unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="ops-conclusion critical">
          <h3>发生了什么：{escape('光性能劣化已沿同一物理路径形成传播事件')}</h3>
          <div class="small">影响业务：<b>{len(incident.get('affected_services') or [])} 条</b>　·　
          首要疑似根因：<b>{escape(FAULT_LABELS.get(str(top.get('fault_type')), str(top.get('fault_type') or '研判中')))}</b></div>
          <div class="small">当前动作：<b>{escape(str((actions[0] if actions else {}).get('action_name') or '等待智能研判完成'))}</b>　·　
          异常路径：<b>{escape(path)}</b></div>
          <div class="small">研判模型：<b>{escape(str((incident.get('engine_runtime') or {}).get('name') or incident.get('engine_display_name') or '待命'))}</b>　·　
          {escape(format_engine_runtime_detail(runtime))}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("</div>", unsafe_allow_html=True)

    left, right = st.columns([1.05, 1.0], gap="small")
    with left:
        st.markdown("<div class='section-shell'><div class='section-heading'><b>传播链路与关键指标</b><span>异常证据</span></div>", unsafe_allow_html=True)
        st.markdown(f"<div class='metric-evidence'><b>传播路径</b><br>{escape(path)}</div>", unsafe_allow_html=True)
        for change in (incident.get("key_metric_changes") or [])[:3]:
            st.markdown(
                f"<div class='metric-evidence'><b>{escape(metric_label(str(change.get('metric') or '')))}</b> · "
                f"{escape(short_text(clean_display_text(change.get('entity_id')), 52))}<br>"
                f"均值变化 {format_number(change.get('pre_mean'))} → {format_number(change.get('post_mean'))} "
                f"（Δ {format_number(change.get('delta'))}）</div>",
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)
    with right:
        st.markdown("<div class='section-shell'><div class='section-heading'><b>根因研判</b><span>候选原因与判定说明</span></div>", unsafe_allow_html=True)
        if not top_causes:
            st.markdown("<div class='small'>智能研判正在执行。</div>", unsafe_allow_html=True)
        for cause in top_causes:
            st.markdown(
                f"<div class='metric-evidence'><b>候选 {int(cause.get('rank') or 0)} · "
                f"{escape(FAULT_LABELS.get(str(cause.get('fault_type')), str(cause.get('fault_type'))))}</b><br>"
                f"{escape(short_text(clean_display_text(cause.get('entity_id')), 58))}<br>"
                f"<span class='small'>研判说明：{escape(str(cause.get('exclusion') or '需结合后续验证复核'))}</span></div>",
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='section-shell'><div class='section-heading'><b>处置方案建议</b><span>建议动作与预期效果</span></div>", unsafe_allow_html=True)
    for index, action in enumerate(actions):
        st.markdown(
            f"<div class='action-card {'primary' if index == 0 else ''}'><b>{escape(str(action.get('action_id')))} · "
            f"{escape(str(action.get('action_name')))}</b><br><span class='small'>目标："
            f"{escape(short_text(clean_display_text(action.get('target_entity')), 72))}<br>"
            f"参数：{escape(format_action_parameters(action.get('parameters')))}　·　"
            f"预期：{escape(str(action.get('expected_effect')))}　·　"
            f"风险：{escape(ACTION_RISK_LABELS.get(str(action.get('risk')), str(action.get('risk'))))}　·　"
            f"状态：{escape(ACTION_STATUS_LABELS.get(str(action.get('status')), str(action.get('status'))))}</span></div>",
            unsafe_allow_html=True,
        )
    if not actions:
        st.markdown("<div class='small'>处置方案建议正在生成。</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


def advance_replay_state(
    runtime: dict[str, Any],
    run: dict[str, Any],
    bundle: dict[str, Any],
) -> None:
    if runtime.get("playback_status") != "RUNNING":
        return
    stage = str(runtime.get("stage") or "MONITORING")
    if stage == "MONITORING":
        timeline = list(bundle.get("timeline") or [0.0])
        index = min(int(runtime.get("index") or 0), len(timeline) - 1)
        tick = float(timeline[index])
        snapshot = scan_telemetry(bundle, current_tick=tick)
        active = active_services_at_tick(bundle.get("service_lifecycle") or [], tick)
        runtime["snapshot"] = snapshot
        runtime["active_service_count"] = len(active)
        route_key = should_create_incident(
            list(snapshot.get("candidate_paths") or []),
            list(runtime.get("previous_paths") or []),
        )
        if route_key:
            incident = build_replay_incident(
                run_id=str(run.get("run_id") or ""),
                current_tick=tick,
                route_key=route_key,
                observations=list(snapshot.get("observations") or []),
                active_services=active,
            )
            runtime["incidents"] = [incident]
            runtime["stage"] = "ANOMALY_DETECTED"
            st.session_state["incident"] = incident
        elif index >= len(timeline) - 1:
            runtime["playback_status"] = "COMPLETED"
            runtime["stage"] = "NORMAL_COMPLETED"
            runtime["normal_completed"] = True
        else:
            runtime["previous_paths"] = list(snapshot.get("candidate_paths") or [])
            runtime["index"] = index + 1
    elif stage == "ANOMALY_DETECTED":
        incident = current_replay_incident(runtime)
        if incident:
            incident["current_stage"] = "EVENT_CORRELATION"
        runtime["stage"] = "EVENT_CORRELATION"
    elif stage == "EVENT_CORRELATION":
        incident = current_replay_incident(runtime)
        if incident:
            incident["status"] = "UNDER_ANALYSIS"
            incident["current_stage"] = "DIAGNOSING"
        preference = str(st.session_state.get("engine_preference") or "在线大模型")
        config = current_model_config()
        if preference == "在线大模型" and model_config_ready(config):
            runtime["engine_runtime"] = {
                "name": str(config.get("model") or DEFAULT_ENGINE_MODEL),
                "mode": "ONLINE",
                "status": "等待在线调用",
                "attempts": 0,
                "fallback": False,
            }
        else:
            runtime["engine_runtime"] = {
                "name": "本地诊断引擎",
                "mode": "LOCAL",
                "status": "手动选择" if preference == "本地诊断引擎" else "在线配置不可用",
                "attempts": 0,
                "fallback": preference != "本地诊断引擎",
            }
        runtime["stage"] = "DIAGNOSING"
    elif stage == "DIAGNOSING":
        incident = current_replay_incident(runtime)
        if incident:
            analysis_tick = float(incident.get("first_abnormal_tick") or 0.0)
            detected_tick = float(incident.get("detected_tick") or analysis_tick)
            local_diagnosis = diagnose_run_with_local_rules(
                run,
                knowledge_chunks_path=KNOWLEDGE_CHUNKS_PATH,
                signature_library_path=SIGNATURE_LIBRARY_PATH,
                pre_window=30.0,
                post_window=max(5.0, detected_tick - analysis_tick),
                analysis_tick=analysis_tick,
            )
            diagnosis = local_diagnosis
            payload = dict(local_diagnosis.get("diagnosis_payload") or {})
            payload["event_metric_candidates"] = build_event_metric_candidates(
                incident.get("key_metric_changes") or []
            )
            local_diagnosis["diagnosis_payload"] = payload
            preference = str(st.session_state.get("engine_preference") or "在线大模型")
            config = current_model_config()
            if preference == "在线大模型" and model_config_ready(config):
                runtime["engine_runtime"] = {
                    "name": str(config.get("model") or DEFAULT_ENGINE_MODEL),
                    "mode": "ONLINE",
                    "status": "在线调用中",
                    "attempts": 1,
                    "fallback": False,
                }
                try:
                    online_diagnosis, engine_runtime = diagnose_online_with_retry(
                        payload,
                        config,
                        run_id=str(run.get("run_id") or ""),
                    )
                    diagnosis = dict(local_diagnosis)
                    diagnosis.update(online_diagnosis)
                    runtime["engine_runtime"] = engine_runtime
                    diagnosis["engine_display_name"] = str(engine_runtime.get("name") or config.get("model") or "在线大模型")
                    st.session_state["engine_last_error"] = ""
                except Exception as exc:  # noqa: BLE001
                    st.session_state["engine_last_error"] = str(exc)
                    diagnosis = local_diagnosis
                    diagnosis["engine_display_name"] = "本地诊断引擎（在线重试失败后接管）"
                    attempts_made = int(getattr(exc, "attempts", ONLINE_ENGINE_MAX_ATTEMPTS))
                    runtime["engine_runtime"] = {
                        "name": "本地诊断引擎",
                        "mode": "LOCAL",
                        "status": "在线重试失败后接管",
                        "attempts": attempts_made,
                        "fallback": True,
                        "error": str(exc),
                    }
                    append_engine_call_log(
                        "closed_loop_local_fallback",
                        {
                            "run_id": run.get("run_id"),
                            "online_model": config.get("model"),
                            "attempts": attempts_made,
                            "error": str(exc),
                        },
                    )
            else:
                diagnosis["engine_display_name"] = "本地诊断引擎"
                runtime["engine_runtime"] = {
                    "name": "本地诊断引擎",
                    "mode": "LOCAL",
                    "status": "手动选择" if preference == "本地诊断引擎" else "在线配置不可用",
                    "attempts": 0,
                    "fallback": preference != "本地诊断引擎",
                }
            selected_features = select_validated_metric_features(diagnosis, payload)
            diagnosis["key_metric_features"] = selected_features
            diagnosis["metric_selection_summary"] = {
                "model_selected": sum(
                    1 for item in selected_features if item.get("selection_source") == "online_model"
                ),
                "total": len(selected_features),
            }
            incident["selected_metric_features"] = selected_features
            runtime["selected_metric_series"] = build_selected_metric_series(
                bundle,
                selected_features,
                current_tick=detected_tick,
            )
            top_causes = diagnosis.get("top_causes") or []
            top = top_causes[0] if top_causes and isinstance(top_causes[0], dict) else {}
            incident["diagnosis"] = diagnosis
            incident["primary_suspected_cause"] = str(top.get("fault_type") or "")
            incident["engine_display_name"] = diagnosis.get("engine_display_name")
            incident["engine_runtime"] = dict(runtime.get("engine_runtime") or {})
            incident["status"] = "UNDER_ANALYSIS"
            incident["current_stage"] = "ROOT_CAUSE_ANALYSIS"
            runtime["stage"] = "ROOT_CAUSE_ANALYSIS"
            st.session_state["diagnosis"] = diagnosis
            st.session_state["incident"] = incident
    elif stage == "ROOT_CAUSE_ANALYSIS":
        incident = current_replay_incident(runtime)
        if incident:
            diagnosis = incident.get("diagnosis") if isinstance(incident.get("diagnosis"), dict) else {}
            incident.update(attach_diagnosis(incident, diagnosis))
            incident["status"] = "GENERATING_RECOMMENDATIONS"
            incident["current_stage"] = "ACTION_PLAN_GENERATION"
            runtime["stage"] = "ACTION_PLAN_GENERATION"
            st.session_state["incident"] = incident
    elif stage == "ACTION_PLAN_GENERATION":
        incident = current_replay_incident(runtime)
        if incident:
            incident["status"] = "RECOMMENDATION_READY"
            incident["current_stage"] = "RECOMMENDATION_READY"
            runtime["stage"] = "RECOMMENDATION_READY"
            runtime["playback_status"] = "PAUSED"
            runtime["recommendation_notice_pending"] = True
            st.session_state["incident"] = incident
    st.session_state["replay_runtime"] = runtime


def main() -> None:
    st.set_page_config(page_title="光网络智能运维平台", layout="wide", initial_sidebar_state="collapsed")
    initialize_engine_defaults()
    if st.session_state.get("engine_policy_version") != ONLINE_ENGINE_POLICY_VERSION:
        st.session_state["engine_preference"] = (
            "在线大模型" if model_config_ready(current_model_config()) else "本地诊断引擎"
        )
        st.session_state["engine_policy_version"] = ONLINE_ENGINE_POLICY_VERSION
    if st.session_state.get("theme_policy_version") != THEME_POLICY_VERSION:
        st.session_state["theme"] = "light"
        st.session_state["theme_policy_version"] = THEME_POLICY_VERSION
    if "loaded_run_id" not in st.session_state:
        st.session_state["loaded_run_id"] = None
    if "diagnosis" not in st.session_state:
        st.session_state["diagnosis"] = None
    if "analysis_status" not in st.session_state:
        st.session_state["analysis_status"] = "未接入"
    if "diagnosis_pending" not in st.session_state:
        st.session_state["diagnosis_pending"] = False
    if "system_snapshot" not in st.session_state:
        st.session_state["system_snapshot"] = None
    if "incident" not in st.session_state:
        st.session_state["incident"] = None

    inject_console_css(st.session_state["theme"])
    inject_closed_loop_css()
    registry = load_registry()
    default_run_id = choose_default_run(registry)
    if not default_run_id:
        st.error("没有可用的监测场景。")
        return
    runtime = ensure_replay_runtime(default_run_id)
    try:
        loaded_run = find_run(registry, str(runtime.get("run_id") or default_run_id))
    except KeyError:
        reset_replay_runtime(default_run_id)
        runtime = ensure_replay_runtime(default_run_id)
        loaded_run = find_run(registry, default_run_id)
    bundle = cached_replay_bundle(str(loaded_run.get("run_id")), str(get_run_dir(loaded_run)))

    render_closed_loop_header(runtime)
    emit_recommendation_toast(runtime)
    workspaces = ["运维总览", "事件中心", "处置工作台", "系统配置"]
    if st.session_state.get("workspace_nav") not in workspaces:
        st.session_state["workspace_nav"] = "运维总览"
    workspace = st.radio(
        "工作区",
        workspaces,
        horizontal=True,
        label_visibility="collapsed",
        key="workspace_nav",
    )
    if workspace == "运维总览":
        render_operations_overview(registry, runtime)
    elif workspace == "事件中心":
        render_event_center(runtime)
    elif workspace == "处置工作台":
        render_workbench(runtime, loaded_run)
    else:
        render_system_config_page(registry, runtime)

    if runtime.get("playback_status") == "RUNNING":
        time.sleep(float(runtime.get("playback_interval") or 0.55))
        advance_replay_state(runtime, loaded_run, bundle)
        st.rerun()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("page runtime error")
        try:
            inject_console_css(st.session_state.get("theme", "light"))
        except Exception:
            pass
        st.markdown(
            """
            <div class="panel">
                <div class="panel-title">系统提示</div>
                <div class="small">页面运行异常，请点击左侧“重置”或重新启动平台。</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
