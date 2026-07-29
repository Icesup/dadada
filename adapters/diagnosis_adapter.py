from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any


class DiagnosisAdapter(ABC):
    """统一诊断引擎接口。"""

    @abstractmethod
    def diagnose(self, payload: dict[str, Any]) -> dict[str, Any]:
        """返回结构化诊断结果。"""


class CompatibleDiagnosisAdapter(DiagnosisAdapter):
    """兼容聊天补全协议的在线诊断适配器。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("ENGINE_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        self.base_url = (base_url or os.getenv("ENGINE_BASE_URL") or "").rstrip("/")
        self.model = model or os.getenv("ENGINE_MODEL") or os.getenv("QWEN_MODEL") or "qwen3.7-plus"
        self.timeout_seconds = timeout_seconds or float(os.getenv("ENGINE_TIMEOUT_SECONDS", "120"))
        self.max_tokens = max_tokens or int(os.getenv("ENGINE_MAX_TOKENS", "2000"))
        if not self.api_key:
            raise ValueError("缺少 ENGINE_API_KEY 或 DASHSCOPE_API_KEY")
        if not self.base_url:
            raise ValueError("缺少 ENGINE_BASE_URL")

    def diagnose(self, payload: dict[str, Any]) -> dict[str, Any]:
        """调用兼容聊天补全协议的诊断接口。"""

        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model,
            "messages": build_messages(payload),
            "temperature": 0.1,
            "max_tokens": self.max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_headers = dict(response.headers.items())
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"诊断接口 HTTP 调用失败: {exc.code}: {detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError(f"诊断接口调用超时：{self.timeout_seconds:.0f} 秒内未收到响应，请调大 timeout 或换用更快引擎") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"诊断接口调用失败: {exc}") from exc
        content = data.get("choices", [{}])[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"诊断接口返回内容为空: {data}")
        diagnosis = parse_engine_json(content)
        diagnosis["_engine_call"] = {
            "provider": "compatible_chat",
            "model": data.get("model") or self.model,
            "endpoint": url,
            "usage": data.get("usage") or {},
            "request_id": response_headers.get("x-request-id")
            or response_headers.get("X-Request-Id")
            or response_headers.get("request-id")
            or "",
        }
        return diagnosis

    def ping(self) -> dict[str, Any]:
        """轻量接口连通性检查。"""

        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "只输出合法 JSON。"},
                {"role": "user", "content": "输出 {\"status\":\"OK\"}"},
            ],
            "temperature": 0,
            "max_tokens": 50,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout_seconds, 30)) as response:
                response_headers = dict(response.headers.items())
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"诊断接口 HTTP 调用失败: {exc.code}: {detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError("诊断接口调用超时：30 秒内未收到响应") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"诊断接口调用失败: {exc}") from exc
        return {
            "model": data.get("model") or self.model,
            "usage": data.get("usage") or {},
            "request_id": response_headers.get("x-request-id")
            or response_headers.get("X-Request-Id")
            or response_headers.get("request-id")
            or "",
        }


def build_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """构造在线诊断输入，要求只输出 JSON。"""

    schema = {
        "status": "NORMAL 或 ABNORMAL",
        "summary": "状态概述",
        "top_causes": [
            {
                "rank": 1,
                "entity_id": "候选设备",
                "fault_type": "候选故障类型",
                "evidence": ["关键证据"],
                "exclusion": "排除或降低其他候选优先级的依据",
            }
        ],
        "key_metric_features": [
            {
                "rank": 1,
                "entity_id": "必须逐字复制 event_metric_candidates 中的设备实体",
                "metric": "必须逐字复制 event_metric_candidates 中的指标字段",
                "reason": "该指标为何是本事件最有诊断价值的可观测特征",
            }
        ],
        "recommendations": ["处理建议"],
        "knowledge_chunk_ids": ["使用的知识块ID"],
    }
    system = (
        "你是光网络智能运维仿真测试平台的诊断引擎，按工程排障报告口径输出结论。"
        "只能根据输入中的性能事件摘要、结构化仿真事件摘要、诊断规则提示、历史特征库相似项和知识库内容进行诊断。"
        "不要输出推理过程，不要编造不存在的数据，不要把知识库 score 或历史 similarity 当作根因概率。"
        "diagnostic_hints 是由当前窗口指标自动计算出的工程规则提示，不是 ground truth；当它提示 observed_status=ABNORMAL 且 top_candidates 非空时，不能输出 NORMAL。"
        "当 observed_status=INCONCLUSIVE 或 evidence_source=historical_similarity 时，表示只有历史相似证据而没有直接遥测劣化，不能仅凭相似度断言故障。"
        "如果历史相似度与当前窗口显著指标变化冲突，必须优先采用当前可观测指标变化；例如 pmd_ps 在光纤实体上显著升高，应优先考虑 FIBER_PMD_SURGE。"
        "PROVISIONED、RELEASED 等业务生命周期事件只能作为可观测事件，不代表正常结论；是否正常必须由指标变化幅度、持续性、时序和物理机理共同判断。"
        "summary 要像工程师写的结论：完整说明异常对象、关键变化和主要判断，不要写外部工具口吻或输入模板痕迹。"
        "evidence 必须对应输入中的指标变化、事件、历史相似项或知识库条目；每个候选保留足够证据，避免空泛表述。"
        "所有拓扑地点名和 entity_id 保持输入中的英文原文，不得翻译为中文。"
        "key_metric_features 必须从 event_metric_candidates 中独立选择最有诊断价值的 3 个可观测指标，并按重要性排序；"
        "entity_id 和 metric 必须逐字复制候选值，不得新增、改写或猜测。选择只能依据遥测变化幅度、方向、时序、跨设备传播和诊断相关性，"
        "不得使用 ground truth、故障注入类型、故障实体或注入参数，也不得为了迎合某个已知故障类型反向挑选指标。"
        "recommendations 写成可执行检查项，数量 2 到 4 条，既要可展示又不要写客套话。"
        "输出给页面展示时，统一使用“知识库、诊断引擎、本地规则、在线引擎”等工程化表述。"
        "如果 status 为 ABNORMAL，top_causes 输出 3 个候选根因，并按证据符合度排序；即使 Top-1 很明确，也要给出相近故障作为 Top-2 和 Top-3，并写清排除依据。"
        "如果证据不足或状态正常，status 输出 NORMAL，top_causes 输出空数组。"
        "必须只输出合法 JSON。"
    )
    user = {
        "task": "根据观测数据输出光网络故障诊断结果",
        "required_schema": schema,
        "input": payload,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def parse_engine_json(content: str) -> dict[str, Any]:
    """解析接口 JSON 输出。"""

    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(text[start : end + 1])
        else:
            raise
    if not isinstance(data, dict):
        raise ValueError("诊断接口输出 JSON 顶层必须是对象")
    data.setdefault("top_causes", [])
    data.setdefault("key_metric_features", [])
    data.setdefault("recommendations", [])
    data.setdefault("knowledge_chunk_ids", [])
    return data
