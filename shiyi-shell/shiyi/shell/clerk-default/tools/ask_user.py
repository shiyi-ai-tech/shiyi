"""
AskUserTool - 向用户提问确认

增强版工具，功能：
- 生成结构化的问题
- 支持多种问题类型（confirm, choice, input）
- 返回用户响应占位符
"""

import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class AskUserTool:
    """向用户提问确认工具"""
    
    name = "ask_user"
    description = "向用户提问或请求确认，返回用户的响应"
    
    schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要向用户提问的问题"
            },
            "type": {
                "type": "string",
                "description": "问题类型: confirm(是/否), choice(选择), input(文本输入)",
                "enum": ["confirm", "choice", "input"],
                "default": "input"
            },
            "choices": {
                "type": "array",
                "description": "选项列表（用于 choice 类型）",
                "items": {
                    "type": "string"
                }
            },
            "default": {
                "type": "string",
                "description": "默认值（用户未输入时使用）"
            },
            "hint": {
                "type": "string",
                "description": "输入提示信息"
            }
        },
        "required": ["question"]
    }
    
    @staticmethod
    def execute(params: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
        """
        生成用户交互请求
        
        注意: 这个工具不会真正等待用户响应，而是生成一个结构化的
        交互请求，由调用方负责渲染和收集用户响应。
        
        在自主执行循环中，会将这个问题作为 LLM 响应的一部分，
        通知上层需要用户介入。
        
        Args:
            params: {
                "question": str,       # 问题
                "type": str,           # confirm/choice/input
                "choices": [str],      # 选项
                "default": str,       # 默认值
                "hint": str           # 提示
            }
            workspace: 沙箱工作目录
            
        Returns:
            {"success": bool, "data": {"question_id": str, "question": str, "type": str}, "error": str}
        """
        question = params.get("question", "")
        q_type = params.get("type", "input")
        choices = params.get("choices", [])
        default = params.get("default", "")
        hint = params.get("hint", "")
        
        if not question.strip():
            return {"success": False, "data": None, "error": "Empty question"}
        
        if q_type == "choice" and not choices:
            return {"success": False, "data": None, "error": "choice 类型需要提供 choices"}
        
        # 生成唯一 ID
        import time
        import uuid
        question_id = f"ask_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        
        return {
            "success": True,
            "data": {
                "question_id": question_id,
                "question": question,
                "type": q_type,
                "choices": choices,
                "default": default,
                "hint": hint,
                "pending": True,  # 标记为待响应
                "response": None  # 用户响应将填充在这里
            },
            "error": ""
        }


# 用于模拟用户响应的辅助函数
def simulate_user_response(question_data: Dict[str, Any], response: str) -> Dict[str, Any]:
    """
    模拟用户响应（用于测试或自动化场景）
    
    Args:
        question_data: ask_user 返回的 data
        response: 用户输入的响应
        
    Returns:
        带有用户响应的完整数据
    """
    result = question_data.copy()
    result["response"] = response
    result["pending"] = False
    return result
