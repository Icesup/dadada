from __future__ import annotations

from typing import Protocol


class SimulationAdapter(Protocol):
    """预留的仿真验证接口；第一版不实现真实在线 GNPy 调用。"""

    def validate_action(self, task: dict) -> dict:
        """接收处置验证任务，返回外部仿真引擎执行结果。"""

        ...
